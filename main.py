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

@register("astrbot_plugin_bili_parser", "BiliParser", "Bilibili Link Parser Plugin", "1.0.0", "https://github.com/jkfujr/astrbot-plugin-bili-parser")
class BiliParser(Star):
    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        self.config = config
        
        # 初始化 API Client
        self.api_client = BiliAPIClient(config.get("user_agent", "Mozilla/5.0"))
        
        # 初始化解析器
        self.parser = BiliLinkParser(config)
        
        # 初始化 Jinja2 环境
        self.env = jinja2.Environment()
        self.env.filters['format_number'] = format_number
        self.env.filters['format_live_status'] = format_live_status
        
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

        # 建立类型与模板配置键的映射
        self.template_keys = {
            "Video": "b_video_ret_preset",
            "Live": "b_live_ret_preset",
            "BangumiEp": "b_episode_ret_preset",
            "BangumiSs": "b_bangumi_ret_preset",
            "BangumiMd": "b_bangumi_ret_preset",
            "Article": "b_article_ret_preset",
            "Opus": "b_opus_ret_preset",
            "Space": "b_space_ret_preset",
            "Audio": "b_audio_ret_preset",
            "AudioMenu": "b_audio_menu_ret_preset",
        }

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        message_str = event.message_str
        if not message_str:
            return

        # 提取链接
        links = self.parser.extract_links(message_str)
        if not links:
            return
            
        # 解析限制
        limit = self.config.get("parse_limit", 3)
        if len(links) > limit:
            links = links[:limit]
            
        # 解析短链接
        if self.config.get("b_short_enable", True):
            links = await self.parser.resolve_short_links(links, self.api_client)
            
        results = []
        for link in links:
            fetch_func = self.fetch_methods.get(link.type)
            if not fetch_func:
                continue
                
            try:
                # 获取数据
                data = await fetch_func(link.id)
                if not data or data.get('code') != 0:
                    logger.warning(f"Failed to fetch {link.type} {link.id}: {data}")
                    continue
                
                # 获取对应模板
                template_key = self.template_keys.get(link.type)
                if not template_key:
                    continue
                    
                template_str = self.config.get(template_key)
                if not template_str:
                    continue

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
                rendered = self.env.from_string(template_str).render(
                    **context,
                    get_current_episode=get_current_episode,
                    get_article_id=get_article_id
                )
                results.append(rendered)
                
            except Exception as e:
                logger.error(f"Error processing {link.type} {link.id}: {e}")
                
        # 组合最终回复并发送
        if results:
            delimiter = self.config.get("custom_delimiter", "\n------\n")
            reply_text = delimiter.join(results)
            
            # 解析 <img> 标签并构建 MessageChain
            chain = []
            parts = re.split(r'(<img src="[^"]+" />)', reply_text)
            for part in parts:
                if not part:
                    continue
                img_match = re.match(r'<img src="([^"]+)" />', part)
                if img_match:
                    img_url = img_match.group(1)
                    # 确保图片 URL 包含协议
                    if img_url.startswith('//'):
                        img_url = 'http:' + img_url
                    chain.append(Comp.Image.fromURL(img_url))
                else:
                    # 清理多余的换行符，如果段落为空则不添加
                    text_part = part.strip('\n')
                    if text_part:
                        chain.append(Comp.Plain(text_part + '\n'))

            if chain:
                yield event.chain_result(chain)
