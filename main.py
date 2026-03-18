from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

import re
import jinja2
from typing import Dict, Any

from .parser import BiliLinkParser
from .api import BiliAPIClient
from .utils import format_number, format_live_status

@register("astrbot_plugin_bili_parser", "BiliParser", "Bilibili Link Parser Plugin", "0.0.2", "https://github.com/jkfujr/astrbot_plugin_bili_parser")
class BiliParser(Star):
    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        self.config = config
        
        # 初始化 API Client
        self.api_client = BiliAPIClient(config)
        
        # 启动 Cookie 管理任务
        if self.config.get("cookie", {}).get("mode") == "manager":
            import asyncio
            # 保存任务引用，以便管理生命周期
            self._cookie_task = asyncio.create_task(self.api_client.start())
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
            "Video": self.api_client.fetch_video,
            "Live": self.api_client.fetch_live,
            "BangumiEp": self.api_client.fetch_bangumi_ep_ss,
            "BangumiSs": self.api_client.fetch_bangumi_ep_ss,
            "BangumiMd": self.api_client.fetch_bangumi_md,
            "Article": self.api_client.fetch_article,
            "Opus": self.api_client.fetch_opus,
            "Space": self.api_client.fetch_space,
            "Audio": self.api_client.fetch_audio,
            "AudioMenu": self.api_client.fetch_audio_menu,
        }

        # 建立类型与模板配置路径的映射，格式为 (配置节, 配置键)
        self.template_keys = {
            "Video":      ("video",   "ret_preset"),
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
        await self.api_client.stop()
        if self._cookie_task:
            try:
                self._cookie_task.cancel()
                await self._cookie_task
            except Exception:
                pass

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        message_str = event.message_str
        if not message_str:
            return

        debug = self.config.get("basic", {}).get("debug_mode", False)
        if debug:
            logger.info(f"[BiliParser][DEBUG] 收到消息: {repr(message_str[:200])}")

        # 提取链接
        try:
            links = self.parser.extract_links(message_str)
        except Exception as e:
            logger.error(f"[BiliParser] extract_links 异常: {e}")
            return

        if debug:
            logger.info(f"[BiliParser][DEBUG] 提取到链接: {links}")

        if not links:
            return
            
        # 解析限制
        limit = self.config.get("basic", {}).get("parse_limit", 3)
        if len(links) > limit:
            links = links[:limit]
            
        # 解析短链接
        if self.config.get("short_link", {}).get("enable", True):
            links = await self.parser.resolve_short_links(links, self.api_client)
            if debug:
                logger.info(f"[BiliParser][DEBUG] 短链解析后: {links}")
            
        results = []
        for link in links:
            fetch_func = self.fetch_methods.get(link.type)
            if not fetch_func:
                continue
                
            try:
                if debug:
                    logger.info(f"[BiliParser][DEBUG] 请求 {link.type} id={link.id}")
                # 获取数据
                data = await fetch_func(link.id)
                if debug:
                    logger.info(f"[BiliParser][DEBUG] {link.type} {link.id} 响应 code={data.get('code') if data else None}")
                if not data or data.get('code') != 0:
                    code = data.get('code') if data else None
                    msg = data.get('message', '未知错误') if data else '请求失败'
                    logger.warning(f"[BiliParser] fetch {link.type} {link.id} 失败: code={code}, msg={msg}")
                    # 未登录时给出明确提示
                    if code == -101:
                        results.append(f"[解析失败] {link.type} 需要登录 Cookie 才能访问，请在插件配置中设置 Cookie。")
                    else:
                        results.append(f"[解析失败] {link.type} {link.id}：{msg}（错误码 {code}）")
                    continue
                
                # 获取对应模板配置路径
                template_path = self.template_keys.get(link.type)
                if not template_path:
                    logger.warning(f"[BiliParser] 未找到 {link.type} 的模板路径映射")
                    continue
                
                section, key = template_path
                template_str = self.config.get(section, {}).get(key)
                if not template_str:
                    logger.warning(f"[BiliParser] 配置中未找到 {section}.{key} 模板，请检查插件配置")
                    continue

                # 使用缓存的模板或重新编译（以路径元组为缓存键）
                cache_key = template_path
                if cache_key not in self.template_cache or self.template_cache[cache_key].source != template_str:
                    self.template_cache[cache_key] = self.env.from_string(template_str)
                
                template = self.template_cache[cache_key]

                # 准备上下文
                context = data.get('data', {})
                if 'result' in data and not context: 
                     context = data['result']
                
                # 定义模板内联辅助函数
                def get_current_episode(key):
                    if link.type == 'BangumiEp':
                        try:
                            ep_id_str = re.sub(r'^ep', '', link.id, flags=re.IGNORECASE)
                            ep_id = int(ep_id_str)
                            episodes = context.get('episodes', [])
                            for ep in episodes:
                                if ep.get('ep_id') == ep_id:
                                    return ep.get(key)
                        except Exception:
                            pass
                    return ""

                def get_article_id():
                    return re.sub(r'^cv', '', link.id, flags=re.IGNORECASE)

                # 渲染并收集结果
                rendered = template.render(
                    **context,
                    get_current_episode=get_current_episode,
                    get_article_id=get_article_id
                )
                results.append(rendered)
                
            except Exception as e:
                import traceback
                logger.error(f"[BiliParser] 处理 {link.type} {link.id} 时异常: {e}\n{traceback.format_exc()}")
                
        # 组合最终回复并发送
        if results:
            delimiter = self.config.get("basic", {}).get("custom_delimiter", "\n------\n")
            reply_text = delimiter.join(results)
            
            # 解析 <img> 标签并构建 MessageChain
            chain = []
            # 更健壮的正则匹配：支持可选的自闭合斜杠，支持属性间空格
            parts = re.split(r'(<img\s+src="[^"]+"\s*/?>)', reply_text)
            for part in parts:
                if not part:
                    continue
                # 匹配 URL
                img_match = re.match(r'<img\s+src="([^"]+)"\s*/?>', part)
                if img_match:
                    img_url = img_match.group(1)
                    # 确保图片 URL 包含协议，优先使用 https
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    elif img_url.startswith('http:'):
                        img_url = 'https' + img_url[4:]
                        
                    chain.append(Comp.Image.fromURL(img_url))
                else:
                    # 清理多余的换行符，如果段落为空则不添加
                    text_part = part.strip('\n')
                    if text_part:
                        chain.append(Comp.Plain(text_part + '\n'))

            if chain:
                yield event.chain_result(chain)
