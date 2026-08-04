import asyncio
import json
import os
import re
import traceback
from functools import partial
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import jinja2

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

from .core import BiliAPIClient, BiliNetworkError, CookieManager, BiliLinkParser
from .utils import format_number, format_live_status
from .utils.remote_image import enable_remote_image_delivery

TEMPLATE_RENDER_ERROR_TEXT = "[模板解析失败] 请检查插件回复模板配置。"


@register("astrbot_plugin_bili_parser", "BiliParser", "Bilibili Link Parser Plugin", "0.0.3", "https://github.com/jkfujr/astrbot_plugin_bili_parser")
class BiliParser(Star):
    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        self.config = config
        
        # 初始化 Cookie 管理器
        cookie_config = config.get("cookie", {})
        self.cookie_manager = CookieManager(cookie_config)
        
        # 初始化 API Client（注入 Cookie 管理器）
        basic_config = config.get("basic", {})
        network_config = config.get("network", {})
        user_agent = basic_config.get("user_agent", "Mozilla/5.0")
        self.api_client = BiliAPIClient(user_agent, self.cookie_manager, network_config)
        
        # 启动后台任务
        if cookie_config.get("mode") == "manager":
            self._cookie_task = asyncio.create_task(self.cookie_manager.start())
        else:
            self._cookie_task = None
        
        # 初始化解析器
        self.parser = BiliLinkParser(config)
        
        # 初始化 Jinja2 环境
        self.env = jinja2.Environment()
        self.env.filters['format_number'] = format_number
        self.env.filters['format_live_status'] = format_live_status
        
        # 预编译模板 (简单缓存)
        self.template_cache = {}
        
        # 建立类型与抓取方法的映射
        self.fetch_methods = {
            "Video": lambda link: self.api_client.fetch_video(link.id),
            "VideoComment": lambda link: self.api_client.fetch_video_comment(
                link.id,
                link.params["root_id"],
                self.config.get("video_comment", {}).get("use_cookie", False),
            ),
            "Live": lambda link: self.api_client.fetch_live(link.id),
            "BangumiEp": lambda link: self.api_client.fetch_bangumi_ep_ss(link.id),
            "BangumiSs": lambda link: self.api_client.fetch_bangumi_ep_ss(link.id),
            "BangumiMd": lambda link: self.api_client.fetch_bangumi_md(link.id),
            "Article": lambda link: self.api_client.fetch_article(link.id),
            "Opus": lambda link: self.api_client.fetch_opus(link.id),
            "Space": lambda link: self.api_client.fetch_space(link.id),
            "Audio": lambda link: self.api_client.fetch_audio(link.id),
            "AudioMenu": lambda link: self.api_client.fetch_audio_menu(link.id),
        }

        # 建立类型与模板配置路径的映射，格式为 (配置节, 配置键)
        self.template_keys = {
            "Video":      ("video",   "ret_preset"),
            "VideoComment": ("video_comment", "ret_preset"),
            "Live":       ("live",    "ret_preset"),
            "BangumiEp": ("bangumi", "episode_ret_preset"),
            "BangumiSs": ("bangumi", "ret_preset"),
            "BangumiMd": ("bangumi", "ret_preset"),
            "Article":   ("article", "ret_preset"),
            "Opus":      ("opus",    "ret_preset"),
            "Space":     ("space",   "ret_preset"),
            "Audio":     ("audio",   "ret_preset"),
            "AudioMenu": ("audio",   "menu_ret_preset"),
        }

    async def terminate(self):
        """插件卸载时调用"""
        if self._cookie_task:
            try:
                self._cookie_task.cancel()
                await self._cookie_task
            except asyncio.CancelledError:
                pass
            finally:
                self._cookie_task = None
        await self.cookie_manager.stop()
        await self.api_client.stop()

    async def _extract_event_links(self, event: AstrMessageEvent, debug: bool):
        message_str = event.message_str
        extra_links = []

        if self.config.get("json_card", {}).get("enable", True):
            for component in event.get_messages():
                if not isinstance(component, Comp.Json):
                    continue
                try:
                    extra_links.extend(self.parser.extract_card_links(component.data))
                except Exception as e:
                    logger.error(f"[BiliParser] extract_card_links 异常: {e}")

        if not message_str and not extra_links:
            return []

        if debug:
            logger.info(f"[BiliParser][DEBUG] 收到消息: {repr(message_str[:200]) if message_str else '<包含 JSON 卡片>'}")

        try:
            links = []
            if message_str:
                links.extend(self.parser.extract_links(message_str))
            if extra_links:
                links.extend(extra_links)
            
            # 统一去重
            if links:
                links = self.parser._deduplicate_links(links)
        except Exception as e:
            logger.error(f"[BiliParser] extract_links 异常: {e}")
            return []

        if debug:
            logger.info(f"[BiliParser][DEBUG] 提取到链接: {links}")

        if not links:
            return []

        limit = self.config.get("basic", {}).get("parse_limit", 3)
        if len(links) > limit:
            links = links[:limit]

        if self.config.get("short_link", {}).get("enable", True):
            links = await self.parser.resolve_short_links(links, self.api_client)
            if debug:
                logger.info(f"[BiliParser][DEBUG] 短链解析后: {links}")

        return links

    async def _fetch_link_data(self, link, debug: bool):
        fetch_func = self.fetch_methods.get(link.type)
        if not fetch_func:
            return None, None

        if debug:
            logger.info(f"[BiliParser][DEBUG] 请求 {link.type} id={link.id}")

        try:
            data = await fetch_func(link)
        except BiliNetworkError as e:
            if debug:
                logger.error(f"[BiliParser] fetch {link.type} {link.id} 网络失败: {e}\n{traceback.format_exc()}")
            else:
                logger.warning(f"[BiliParser] fetch {link.type} {link.id} 网络失败: {e}")
            return None, f"[解析失败] {link.type} {link.id}：{e}"
        except ValueError as e:
            logger.warning(f"[BiliParser] fetch {link.type} {link.id} 解析失败: {e}")
            return None, f"[解析失败] {link.type} {link.id}：{e}"
        except KeyError as e:
            logger.warning(f"[BiliParser] fetch {link.type} {link.id} 参数缺失: {e}")
            return None, f"[解析失败] {link.type} {link.id}：参数缺失"

        if debug:
            logger.info(f"[BiliParser][DEBUG] {link.type} {link.id} 响应 code={data.get('code') if data else None}")

        if not data or data.get('code') != 0:
            code = data.get('code') if data else None
            msg = data.get('message', '未知错误') if data else '请求失败'
            logger.warning(f"[BiliParser] fetch {link.type} {link.id} 失败: code={code}, msg={msg}")
            if code == -101:
                return None, f"[解析失败] {link.type} 需要登录 Cookie 才能访问，请在插件配置中设置 Cookie。"
            return None, f"[解析失败] {link.type} {link.id}：{msg}（错误码 {code}）"

        return data, None

    def _render_link(self, link, data):
        template_path = self.template_keys.get(link.type)
        if not template_path:
            logger.warning(f"[BiliParser] 未找到 {link.type} 的模板路径映射")
            return None

        section, key = template_path
        template_str = self.config.get(section, {}).get(key)
        if not template_str:
            logger.warning(f"[BiliParser] 配置中未找到 {section}.{key} 模板，请检查插件配置")
            return None

        context = data.get('data', {})
        if 'result' in data and not context:
            context = data['result']

        cache_key = template_path
        try:
            cached_template = self.template_cache.get(cache_key)
            if not cached_template or cached_template[0] != template_str:
                template = self.env.from_string(template_str)
                self.template_cache[cache_key] = (template_str, template)
            else:
                template = cached_template[1]
            return self._render_template(template, context, link)
        except jinja2.exceptions.TemplateError as te:
            return self._render_with_default_template(section, key, cache_key, context, link, te)

    def _render_template(self, template, context, link):
        return template.render(
            **context,
            get_current_episode=partial(self._get_current_episode, context, link),
            get_article_id=partial(self._get_article_id, link)
        )

    def _render_with_default_template(self, section: str, key: str, cache_key, context, link, error):
        logger.warning(f"[BiliParser] 模板渲染失败: {error}。将尝试恢复出厂默认配置...")
        schema_path = os.path.join(os.path.dirname(__file__), "_conf_schema.json")
        default_tmpl = ""
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema_data = json.load(f)
                default_tmpl = schema_data.get(section, {}).get("items", {}).get(key, {}).get("default", "")
        except Exception as schema_err:
            logger.error(f"[BiliParser] 读取默认 schema 失败: {schema_err}")

        if not default_tmpl:
            return TEMPLATE_RENDER_ERROR_TEXT

        logger.info(f"[BiliParser] 正在使用默认模板重试渲染: {section}.{key}")
        self.config.setdefault(section, {})[key] = default_tmpl
        logger.info("[BiliParser] 内存中已重置当前的出错配置。如果需要永久生效，请前往客户端/网页控制台重新保存一次插件配置。")

        try:
            template = self.env.from_string(default_tmpl)
            self.template_cache[cache_key] = (default_tmpl, template)
            return self._render_template(template, context, link)
        except jinja2.exceptions.TemplateError as retry_error:
            logger.error(f"[BiliParser] 默认模板重试渲染仍然失败: {retry_error}")
            return TEMPLATE_RENDER_ERROR_TEXT

    def _get_current_episode(self, context, link, key):
        if link.type == 'BangumiEp':
            ep_id_str = re.sub(r'^ep', '', link.id, flags=re.IGNORECASE)
            if not ep_id_str.isdigit():
                return ""

            ep_id = int(ep_id_str)
            episodes = context.get('episodes', [])
            for ep in episodes:
                if isinstance(ep, dict) and ep.get('ep_id') == ep_id:
                    return ep.get(key)
        return ""

    def _get_article_id(self, link):
        return re.sub(r'^cv', '', link.id, flags=re.IGNORECASE)

    def _is_img_tag_start(self, reply_text: str, pos: int) -> bool:
        if reply_text[pos] != '<':
            return False
        if reply_text[pos + 1:pos + 4].lower() != 'img':
            return False
        boundary = pos + 4
        return boundary >= len(reply_text) or reply_text[boundary].isspace() or reply_text[boundary] in '/>'

    def _find_tag_end(self, reply_text: str, start: int) -> int:
        quote = ''
        for pos in range(start + 1, len(reply_text)):
            char = reply_text[pos]
            if quote:
                if char == quote:
                    quote = ''
            elif char in ('"', "'"):
                quote = char
            elif char == '>':
                return pos + 1
        return -1

    def _extract_img_src(self, tag: str) -> Optional[str]:
        pos = 4
        end = len(tag) - 1 if tag.endswith('>') else len(tag)

        while pos < end:
            while pos < end and (tag[pos].isspace() or tag[pos] == '/'):
                pos += 1
            if pos >= end:
                break

            name_start = pos
            while pos < end and not tag[pos].isspace() and tag[pos] not in '=/>':
                pos += 1
            attr_name = tag[name_start:pos].lower()

            while pos < end and tag[pos].isspace():
                pos += 1

            attr_value = ''
            if pos < end and tag[pos] == '=':
                pos += 1
                while pos < end and tag[pos].isspace():
                    pos += 1

                if pos < end and tag[pos] in ('"', "'"):
                    quote = tag[pos]
                    pos += 1
                    value_start = pos
                    while pos < end and tag[pos] != quote:
                        pos += 1
                    attr_value = tag[value_start:pos]
                    if pos < end and tag[pos] == quote:
                        pos += 1
                else:
                    value_start = pos
                    while pos < end and not tag[pos].isspace() and tag[pos] != '>':
                        pos += 1
                    attr_value = tag[value_start:pos]

            if attr_name == 'src':
                return attr_value

        return None

    def _append_plain_text(self, chain, text: str):
        text_part = text.strip('\n')
        if text_part:
            chain.append(Comp.Plain(text_part + '\n'))

    def _normalize_image_url(self, img_url: str) -> str:
        img_url = img_url.strip()
        img_url_lower = img_url.lower()
        if img_url.startswith('//'):
            normalized = 'https:' + img_url
        elif img_url_lower.startswith('http://'):
            normalized = 'https://' + img_url[7:]
        elif img_url_lower.startswith('https://'):
            normalized = 'https://' + img_url[8:]
        else:
            return ""

        if re.search(r'\s', normalized):
            return ""
        parsed = urlsplit(normalized)
        if parsed.scheme != 'https' or not parsed.netloc:
            return ""
        return normalized

    def _build_message_chain(self, event: AstrMessageEvent, reply_text: str):
        chain = []
        pos = 0
        last_end = 0
        while pos < len(reply_text):
            if not self._is_img_tag_start(reply_text, pos):
                pos += 1
                continue

            tag_end = self._find_tag_end(reply_text, pos)
            if tag_end == -1:
                pos += 1
                continue

            img_src = self._extract_img_src(reply_text[pos:tag_end])
            if img_src is not None:
                self._append_plain_text(chain, reply_text[last_end:pos])
                img_url = self._normalize_image_url(img_src)
                if img_url:
                    chain.append(Comp.Image.fromURL(img_url))
                last_end = tag_end
            pos = tag_end

        self._append_plain_text(chain, reply_text[last_end:])

        if chain and self.config.get("basic", {}).get("show_quote", True):
            message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
            if message_id:
                chain.insert(0, Comp.Reply(id=message_id))

        return chain

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        debug = self.config.get("basic", {}).get("debug_mode", False)
        links = await self._extract_event_links(event, debug)
        if not links:
            return

        results = []
        for link in links:
            try:
                data, error_text = await self._fetch_link_data(link, debug)
                if error_text:
                    results.append(error_text)
                    continue
                if not data:
                    continue

                rendered = self._render_link(link, data)
                if rendered is not None:
                    results.append(rendered)
            except Exception as e:
                logger.error(f"[BiliParser] 处理 {link.type} {link.id} 时异常: {e}\n{traceback.format_exc()}")

        if results:
            delimiter = self.config.get("basic", {}).get("custom_delimiter", "\n------\n")
            reply_text = delimiter.join(results)
            chain = self._build_message_chain(event, reply_text)
            if chain:
                result = event.chain_result(chain)
                yield enable_remote_image_delivery(event, result)
