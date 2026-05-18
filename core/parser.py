"""
B站链接提取与解析
"""

import asyncio
import json
import re
from typing import List, Dict, Any, Optional
from urllib.parse import parse_qs, urlsplit

from astrbot.api import logger

from ..utils import normalize_video_id


class Link:
    def __init__(self, type_: str, id_: str, params: Optional[Dict[str, str]] = None):
        self.type = type_
        self.id = id_
        self.params = params or {}

    def __repr__(self):
        if self.params:
            return f"Link(type={self.type}, id={self.id}, params={self.params})"
        return f"Link(type={self.type}, id={self.id})"

    def __eq__(self, other):
        if isinstance(other, Link):
            return self.type == other.type and self.id == other.id and self.params == other.params
        return False

    def __hash__(self):
        return hash((self.type, self.id, tuple(sorted(self.params.items()))))


class BiliLinkParser:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.max_json_depth = 10
        self._compile_regex()

    def _compile_regex(self):
        """预编译正则表达式"""
        self.patterns = []
        self.video_url_pattern = re.compile(
            r'(?:(?:https?:)?//)?(?:www\.)?bilibili\.com/video/[^\s<>"\']+',
            re.I
        )

        if self.config.get("video", {}).get("enable", True):
            pattern1 = r'bilibili\.com\/video\/((?<![a-zA-Z0-9])[aA][vV][0-9]+(?![a-zA-Z0-9]))' if self.config.get("video", {}).get("full_url", True) else r'((?<![a-zA-Z0-9])[aA][vV][0-9]+(?![a-zA-Z0-9]))'
            pattern2 = r'bilibili\.com\/video\/((?<![a-zA-Z0-9])[bB][vV]1[0-9a-zA-Z]{9}(?![a-zA-Z0-9]))' if self.config.get("video", {}).get("full_url", True) else r'((?<![a-zA-Z0-9])[bB][vV]1[0-9a-zA-Z]{9}(?![a-zA-Z0-9]))'
            self.patterns.append({"pattern": re.compile(pattern1, re.I), "type": "Video"})
            self.patterns.append({"pattern": re.compile(pattern2, re.I), "type": "Video"})

        if self.config.get("live", {}).get("enable", True):
            self.patterns.append({"pattern": re.compile(r'live\.bilibili\.com(?:\/h5)?\/(\d+)', re.I), "type": "Live"})

        if self.config.get("bangumi", {}).get("enable", True):
            p1 = r'bilibili\.com\/bangumi\/play\/(ep\d+)(?![a-zA-Z0-9])' if self.config.get("bangumi", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])(ep\d+)(?![a-zA-Z0-9])'
            p2 = r'bilibili\.com\/bangumi\/play\/(ss\d+)(?![a-zA-Z0-9])' if self.config.get("bangumi", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])(ss\d+)(?![a-zA-Z0-9])'
            p3 = r'bilibili\.com\/bangumi\/media\/(md\d+)(?![a-zA-Z0-9])' if self.config.get("bangumi", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])(md\d+)(?![a-zA-Z0-9])'
            self.patterns.append({"pattern": re.compile(p1, re.I), "type": "BangumiEp"})
            self.patterns.append({"pattern": re.compile(p2, re.I), "type": "BangumiSs"})
            self.patterns.append({"pattern": re.compile(p3, re.I), "type": "BangumiMd"})

        if self.config.get("space", {}).get("enable", True):
            self.patterns.append({"pattern": re.compile(r'space\.bilibili\.com\/(\d+)', re.I), "type": "Space"})
            self.patterns.append({"pattern": re.compile(r'bilibili\.com\/space\/(\d+)', re.I), "type": "Space"})

        if self.config.get("opus", {}).get("enable", True):
            self.patterns.append({"pattern": re.compile(r'bilibili\.com\/opus\/(\d+)', re.I), "type": "Opus"})

        if self.config.get("article", {}).get("enable", True):
            pattern = r'bilibili\.com\/read\/cv(\d+)(?![a-zA-Z0-9])' if self.config.get("article", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])cv(\d+)(?![a-zA-Z0-9])'
            self.patterns.append({"pattern": re.compile(pattern, re.I), "type": "Article"})
            self.patterns.append({"pattern": re.compile(r'bilibili\.com\/read\/mobile(?:\?id=|\/)(\d+)(?![a-zA-Z0-9])', re.I), "type": "Article"})

        if self.config.get("audio", {}).get("enable", True):
            pattern = r'bilibili\.com\/audio\/au(\d+)(?![a-zA-Z0-9])' if self.config.get("audio", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])au(\d+)(?![a-zA-Z0-9])'
            self.patterns.append({"pattern": re.compile(pattern, re.I), "type": "Audio"})

            pattern = r'bilibili\.com\/audio\/am(\d+)(?![a-zA-Z0-9])' if self.config.get("audio", {}).get("full_url", True) else r'(?<![a-zA-Z0-9])am(\d+)(?![a-zA-Z0-9])'
            self.patterns.append({"pattern": re.compile(pattern, re.I), "type": "AudioMenu"})

        if self.config.get("short_link", {}).get("enable", True):
            self.patterns.append({"pattern": re.compile(r'b23\.tv(?:\\)?\/([0-9a-zA-Z]+)(?![a-zA-Z0-9])', re.I), "type": "Short"})
            self.patterns.append({"pattern": re.compile(r'bili(?:22|23|33)\.cn\/([0-9a-zA-Z]+)(?![a-zA-Z0-9])', re.I), "type": "Short"})

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
        sanitized_content = self._strip_html_tags(content)
        scan_content = sanitized_content

        if self.config.get("video_comment", {}).get("enable", True):
            comment_links, scan_content = self._extract_video_comment_links(sanitized_content)
            results.extend(comment_links)

        for item in self.patterns:
            for match in item["pattern"].finditer(scan_content):
                results.append(Link(item["type"], match.group(1)))

        return self._deduplicate_links(results)

    def _extract_video_comment_links(self, content: str):
        links = []
        chars = list(content)

        for match in self.video_url_pattern.finditer(content):
            link = self._parse_video_comment_url(match.group(0))
            if not link:
                continue

            links.append(link)
            chars[match.start():match.end()] = " " * (match.end() - match.start())

        return links, "".join(chars)

    def _parse_video_comment_url(self, url: str) -> Optional[Link]:
        parsed_url = url
        if parsed_url.startswith("//"):
            parsed_url = "https:" + parsed_url
        elif not parsed_url.startswith(("http://", "https://")):
            parsed_url = "https://" + parsed_url

        parsed = urlsplit(parsed_url)
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 2 or path_parts[0].lower() != "video":
            return None

        video_id = path_parts[1]
        if not re.match(r'^(?:av\d+|bv1[0-9a-zA-Z]{9})$', video_id, re.I):
            return None

        root_ids = parse_qs(parsed.query).get("comment_root_id") or []
        root_id = root_ids[0] if root_ids else ""
        if not root_id.isdigit():
            return None

        return Link("VideoComment", video_id, {"root_id": root_id})

    def _strip_html_tags(self, content: str) -> str:
        """用线性扫描剥离 HTML 标签，避免正则处理异常长文本。"""
        if "<" not in content:
            return content

        result = []
        pos = 0
        while pos < len(content):
            if content[pos] != "<":
                result.append(content[pos])
                pos += 1
                continue

            tag_end = self._find_html_tag_end(content, pos)
            if tag_end == -1:
                result.append(content[pos])
                pos += 1
            else:
                pos = tag_end + 1

        return "".join(result)

    def _find_html_tag_end(self, content: str, start: int) -> int:
        quote = ""
        pos = start + 1
        while pos < len(content):
            ch = content[pos]
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in ("'", '"'):
                quote = ch
            elif ch == ">":
                return pos
            pos += 1
        return -1

    def extract_from_json(self, json_data: dict) -> List[Link]:
        """从 QQ 小程序等 JSON 卡片中提取 B站 链接"""
        extracted_urls = []
        self._find_urls_in_json(json_data, extracted_urls, 0)

        links = []
        for url in extracted_urls:
            links.extend(self.extract_links(url))

        return links

    def _find_urls_in_json(self, obj, extracted_urls: List[str], depth: int):
        if depth > self.max_json_depth:
            return

        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str):
                    if k in ("qqdocurl", "url", "jumpUrl") or re.match(r'^https?://(b23\.tv|www\.bilibili\.com|bili22\.cn)', v):
                        extracted_urls.append(v)
                    else:
                        # Napcat/OneBot 给到的 JSON 内可能嵌套被直接 Stringify 的 JSON 文本（如 data="{\"ver...}"）
                        v_stripped = v.strip()
                        if v_stripped.startswith('{') or v_stripped.startswith('['):
                            try:
                                parsed_v = json.loads(v_stripped)
                                self._find_urls_in_json(parsed_v, extracted_urls, depth + 1)
                            except json.JSONDecodeError as e:
                                if self.config.get("basic", {}).get("debug_mode", False):
                                    snippet = v_stripped[:120].replace("\n", "\\n")
                                    logger.warning(f"[BiliParser] JSON 卡片嵌套字符串解析失败: {e}; content={snippet}")
                else:
                    self._find_urls_in_json(v, extracted_urls, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                self._find_urls_in_json(item, extracted_urls, depth + 1)

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
