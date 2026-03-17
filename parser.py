import re
from typing import List, Dict, Any
from .utils import normalize_video_id
from .api import BiliAPIClient

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

    def _deduplicate_links(self, links: List[Link]) -> List[Link]:
        seen = set()
        unique_links = []
        for link in links:
            if link.type == "Video":
                normalized_id = normalize_video_id(link.id)
                key = (link.type, normalized_id)
            else:
                key = (link.type, link.id)
                
            if key not in seen:
                seen.add(key)
                unique_links.append(link)
                
        return unique_links

    def extract_links(self, content: str) -> List[Link]:
        """从纯文本中提取出所有 B站 链接"""
        link_regex = []

        if self.config.get("video", {}).get("enable", True):
            pattern1 = r'bilibili\.com\/video\/((?<![a-zA-Z0-9])[aA][vV][0-9]+)' if self.config.get("video", {}).get("full_url", True) else r'((?<![a-zA-Z0-9])[aA][vV][0-9]+)'
            pattern2 = r'bilibili\.com\/video\/((?<![a-zA-Z0-9])[bB][vV][0-9a-zA-Z]+)' if self.config.get("video", {}).get("full_url", True) else r'((?<![a-zA-Z0-9])[bB][vV][0-9a-zA-Z]+)'
            link_regex.append({"pattern": re.compile(pattern1, re.I), "type": "Video"})
            link_regex.append({"pattern": re.compile(pattern2, re.I), "type": "Video"})

        if self.config.get("live", {}).get("enable", True):
            link_regex.append({"pattern": re.compile(r'live\.bilibili\.com(?:\/h5)?\/(\d+)', re.I), "type": "Live"})

        if self.config.get("bangumi", {}).get("enable", True):
            p1 = r'bilibili\.com\/bangumi\/play\/(ep\d+)' if self.config.get("bangumi", {}).get("full_url", True) else r'(ep\d+)'
            p2 = r'bilibili\.com\/bangumi\/play\/(ss\d+)' if self.config.get("bangumi", {}).get("full_url", True) else r'(ss\d+)'
            p3 = r'bilibili\.com\/bangumi\/media\/(md\d+)' if self.config.get("bangumi", {}).get("full_url", True) else r'(md\d+)'
            link_regex.append({"pattern": re.compile(p1, re.I), "type": "BangumiEp"})
            link_regex.append({"pattern": re.compile(p2, re.I), "type": "BangumiSs"})
            link_regex.append({"pattern": re.compile(p3, re.I), "type": "BangumiMd"})

        if self.config.get("space", {}).get("enable", True):
            link_regex.append({"pattern": re.compile(r'space\.bilibili\.com\/(\d+)', re.I), "type": "Space"})
            link_regex.append({"pattern": re.compile(r'bilibili\.com\/space\/(\d+)', re.I), "type": "Space"})

        if self.config.get("opus", {}).get("enable", True):
            link_regex.append({"pattern": re.compile(r'bilibili\.com\/opus\/(\d+)', re.I), "type": "Opus"})

        if self.config.get("article", {}).get("enable", True):
            pattern = r'bilibili\.com\/read\/cv(\d+)' if self.config.get("article", {}).get("full_url", True) else r'cv(\d+)'
            link_regex.append({"pattern": re.compile(pattern, re.I), "type": "Article"})
            link_regex.append({"pattern": re.compile(r'bilibili\.com\/read\/mobile(?:\?id=|\/)(\d+)', re.I), "type": "Article"})

        if self.config.get("audio", {}).get("enable", True):
            pattern = r'bilibili\.com\/audio\/au(\d+)' if self.config.get("audio", {}).get("full_url", True) else r'au(\d+)'
            link_regex.append({"pattern": re.compile(pattern, re.I), "type": "Audio"})
            
            pattern = r'bilibili\.com\/audio\/am(\d+)' if self.config.get("audio", {}).get("full_url", True) else r'am(\d+)'
            link_regex.append({"pattern": re.compile(pattern, re.I), "type": "AudioMenu"})

        if self.config.get("short_link", {}).get("enable", True):
            link_regex.append({"pattern": re.compile(r'b23\.tv(?:\\)?\/([0-9a-zA-Z]+)', re.I), "type": "Short"})
            link_regex.append({"pattern": re.compile(r'bili(?:22|23|33)\.cn\/([0-9a-zA-Z]+)', re.I), "type": "Short"})

        results = []
        sanitized_content = re.sub(r'<[^>]+>', '', content)
        
        for item in link_regex:
            for match in item["pattern"].finditer(sanitized_content):
                results.append(Link(item["type"], match.group(1)))

        return self._deduplicate_links(results)

    async def resolve_short_links(self, links: List[Link], api_client: BiliAPIClient) -> List[Link]:
        """将提取出来的短链接转换为真实链接并递归提取"""
        result = []
        for link in links:
            if link.type == "Short":
                redir_url = await api_client.get_short_redir_url(link.id)
                if redir_url:
                    resolved_links = self.extract_links(redir_url)
                    if resolved_links:
                        result.extend(resolved_links)
                        continue
            result.append(link)
        
        return self._deduplicate_links(result)
