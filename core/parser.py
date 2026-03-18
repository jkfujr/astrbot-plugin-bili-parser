"""
B站链接提取与解析
"""

import re
import asyncio
from typing import List, Dict, Any

from ..utils import normalize_video_id


class Link:
    def __init__(self, type_: str, id_: str):
        self.type = type_
        self.id = id_
        self.data: Dict[str, Any] = {}

    def __repr__(self):
        return f"Link(type={self.type}, id={self.id})"

    def __eq__(self, other):
        if isinstance(other, Link):
            return self.type == other.type and self.id == other.id
        return False

    def __hash__(self):
        return hash((self.type, self.id))


class BiliLinkParser:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._compile_regex()

    def _compile_regex(self):
        """预编译正则表达式"""
        self.patterns = []

        if self.config.get("video", {}).get("enable", True):
            pattern1 = r'bilibili\.com\/video\/((?<![a-zA-Z0-9])[aA][vV][0-9]+)' if self.config.get("video", {}).get("full_url", True) else r'((?<![a-zA-Z0-9])[aA][vV][0-9]+)'
            pattern2 = r'bilibili\.com\/video\/((?<![a-zA-Z0-9])[bB][vV][0-9a-zA-Z]+)' if self.config.get("video", {}).get("full_url", True) else r'((?<![a-zA-Z0-9])[bB][vV][0-9a-zA-Z]+)'
            self.patterns.append({"pattern": re.compile(pattern1, re.I), "type": "Video"})
            self.patterns.append({"pattern": re.compile(pattern2, re.I), "type": "Video"})

        if self.config.get("live", {}).get("enable", True):
            self.patterns.append({"pattern": re.compile(r'live\.bilibili\.com(?:\/h5)?\/(\d+)', re.I), "type": "Live"})

        if self.config.get("bangumi", {}).get("enable", True):
            p1 = r'bilibili\.com\/bangumi\/play\/(ep\d+)' if self.config.get("bangumi", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])(ep\d+)'
            p2 = r'bilibili\.com\/bangumi\/play\/(ss\d+)' if self.config.get("bangumi", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])(ss\d+)'
            p3 = r'bilibili\.com\/bangumi\/media\/(md\d+)' if self.config.get("bangumi", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])(md\d+)'
            self.patterns.append({"pattern": re.compile(p1, re.I), "type": "BangumiEp"})
            self.patterns.append({"pattern": re.compile(p2, re.I), "type": "BangumiSs"})
            self.patterns.append({"pattern": re.compile(p3, re.I), "type": "BangumiMd"})

        if self.config.get("space", {}).get("enable", True):
            self.patterns.append({"pattern": re.compile(r'space\.bilibili\.com\/(\d+)', re.I), "type": "Space"})
            self.patterns.append({"pattern": re.compile(r'bilibili\.com\/space\/(\d+)', re.I), "type": "Space"})

        if self.config.get("opus", {}).get("enable", True):
            self.patterns.append({"pattern": re.compile(r'bilibili\.com\/opus\/(\d+)', re.I), "type": "Opus"})

        if self.config.get("article", {}).get("enable", True):
            pattern = r'bilibili\.com\/read\/cv(\d+)' if self.config.get("article", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])cv(\d+)'
            self.patterns.append({"pattern": re.compile(pattern, re.I), "type": "Article"})
            self.patterns.append({"pattern": re.compile(r'bilibili\.com\/read\/mobile(?:\?id=|\/)(\d+)', re.I), "type": "Article"})

        if self.config.get("audio", {}).get("enable", True):
            pattern = r'bilibili\.com\/audio\/au(\d+)' if self.config.get("audio", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])au(\d+)'
            self.patterns.append({"pattern": re.compile(pattern, re.I), "type": "Audio"})

            pattern = r'bilibili\.com\/audio\/am(\d+)' if self.config.get("audio", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])am(\d+)'
            self.patterns.append({"pattern": re.compile(pattern, re.I), "type": "AudioMenu"})

        if self.config.get("short_link", {}).get("enable", True):
            self.patterns.append({"pattern": re.compile(r'b23\.tv(?:\\)?\/([0-9a-zA-Z]+)', re.I), "type": "Short"})
            self.patterns.append({"pattern": re.compile(r'bili(?:22|23|33)\.cn\/([0-9a-zA-Z]+)', re.I), "type": "Short"})

    def _deduplicate_links(self, links: List[Link]) -> List[Link]:
        """对提取出的链接列表去重，视频类型按 AV 号归一化后去重，其他类型按 type+id 去重"""
        seen = set()
        results = []
        for link in links:
            if link.type == "Video":
                normalized = normalize_video_id(link.id)
            else:
                normalized = f"{link.type}:{link.id}"
            if normalized not in seen:
                seen.add(normalized)
                results.append(link)
        return results

    def extract_links(self, content: str) -> List[Link]:
        """从纯文本中提取出所有 B站 链接"""
        results = []
        sanitized_content = re.sub(r'<[^>]+>', '', content)

        for item in self.patterns:
            for match in item["pattern"].finditer(sanitized_content):
                results.append(Link(item["type"], match.group(1)))

        return self._deduplicate_links(results)

    def extract_from_json(self, json_data: dict) -> List[Link]:
        """从 QQ 小程序等 JSON 卡片中提取 B站 链接"""
        extracted_urls = []

        def find_urls(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, str):
                        if k in ("qqdocurl", "url", "jumpUrl") or re.match(r'^https?://(b23\.tv|www\.bilibili\.com|bili22\.cn)', v):
                            extracted_urls.append(v)
                        else:
                            # Napcat/OneBot 给到的 JSON 内可能嵌套被直接 Stringify 的 JSON 文本（如 data="{\"ver...}"）
                            import json
                            v_stripped = v.strip()
                            if v_stripped.startswith('{') or v_stripped.startswith('['):
                                try:
                                    parsed_v = json.loads(v_stripped)
                                    find_urls(parsed_v)
                                except Exception:
                                    pass
                    else:
                        find_urls(v)
            elif isinstance(obj, list):
                for item in obj:
                    find_urls(item)

        find_urls(json_data)
        
        # 将提取到的 url 用普通的字符串提取合并
        links = []
        for url in extracted_urls:
            
            # 使用现有正则从 url 中提取出 Link 对象，注意这里我们跳过了自身内部的去重
            sanitized_content = re.sub(r'<[^>]+>', '', url)
            for item in self.patterns:
                for match in item["pattern"].finditer(sanitized_content):
                    links.append(Link(item["type"], match.group(1)))
                    
        return links

    async def resolve_short_links(self, links: List[Link], api_client) -> List[Link]:
        """将提取出来的短链接转换为真实链接并递归提取 (并发解析)"""

        async def process_link(link: Link, depth=0) -> List[Link]:
            if depth > 3:
                return [link]

            if link.type == "Short":
                redir_url = await api_client.get_short_redir_url(link.id)
                if redir_url:
                    resolved = self.extract_links(redir_url)
                    final_resolved = []
                    for r_link in resolved:
                        if r_link.type == "Short":
                            sub_resolved = await process_link(r_link, depth + 1)
                            final_resolved.extend(sub_resolved)
                        else:
                            final_resolved.append(r_link)
                    if final_resolved:
                        return final_resolved
            return [link]

        tasks = [process_link(link) for link in links]
        results_nested = await asyncio.gather(*tasks)

        flat_results = []
        for sub_list in results_nested:
            flat_results.extend(sub_list)

        return self._deduplicate_links(flat_results)
