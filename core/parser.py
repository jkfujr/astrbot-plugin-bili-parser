"""
B站链接提取与解析
"""

import asyncio
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

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
        self.video_url_pattern = re.compile(
            r'(?:(?:https?:)?//)?(?:www\.)?bilibili\.com/video/[^\s<>"\']+',
            re.I
        )
        self.card_video_url_pattern = re.compile(
            r'(?:(?:https?:)?//)(?:www\.)?bilibili\.com/video/[^\s<>"\']+',
            re.I
        )

        self.text_patterns = self._build_patterns(force_full_url=False)
        self.card_patterns = self._build_patterns(force_full_url=True)

    def _build_patterns(self, force_full_url: bool):
        patterns = []
        bili_prefix = (
            r'(?:(?:https?:)?//)(?:www\.)?bilibili\.com'
            if force_full_url else r'bilibili\.com'
        )
        live_prefix = (
            r'(?:(?:https?:)?//)live\.bilibili\.com'
            if force_full_url else r'live\.bilibili\.com'
        )
        space_prefix = (
            r'(?:(?:https?:)?//)space\.bilibili\.com'
            if force_full_url else r'space\.bilibili\.com'
        )
        short_prefix = r'(?:(?:https?:)?//)' if force_full_url else ''

        def use_full_url(section: str) -> bool:
            return force_full_url or self.config.get(section, {}).get(
                "full_url",
                True,
            )

        if self.config.get("video", {}).get("enable", True):
            if use_full_url("video"):
                pattern1 = (
                    bili_prefix
                    + r'\/video\/((?<![a-zA-Z0-9])[aA][vV][0-9]+(?![a-zA-Z0-9]))'
                )
                pattern2 = (
                    bili_prefix
                    + r'\/video\/((?<![a-zA-Z0-9])[bB][vV]1[0-9a-zA-Z]{9}(?![a-zA-Z0-9]))'
                )
            else:
                pattern1 = r'((?<![a-zA-Z0-9])[aA][vV][0-9]+(?![a-zA-Z0-9]))'
                pattern2 = r'((?<![a-zA-Z0-9])[bB][vV]1[0-9a-zA-Z]{9}(?![a-zA-Z0-9]))'
            patterns.append({"pattern": re.compile(pattern1, re.I), "type": "Video"})
            patterns.append({"pattern": re.compile(pattern2, re.I), "type": "Video"})

        if self.config.get("live", {}).get("enable", True):
            patterns.append({
                "pattern": re.compile(live_prefix + r'(?:\/h5)?\/(\d+)', re.I),
                "type": "Live",
            })

        if self.config.get("bangumi", {}).get("enable", True):
            if use_full_url("bangumi"):
                p1 = bili_prefix + r'\/bangumi\/play\/(ep\d+)(?![a-zA-Z0-9])'
                p2 = bili_prefix + r'\/bangumi\/play\/(ss\d+)(?![a-zA-Z0-9])'
                p3 = bili_prefix + r'\/bangumi\/media\/(md\d+)(?![a-zA-Z0-9])'
            else:
                p1 = r'(?<![a-zA-Z0-9])(ep\d+)(?![a-zA-Z0-9])'
                p2 = r'(?<![a-zA-Z0-9])(ss\d+)(?![a-zA-Z0-9])'
                p3 = r'(?<![a-zA-Z0-9])(md\d+)(?![a-zA-Z0-9])'
            patterns.append({"pattern": re.compile(p1, re.I), "type": "BangumiEp"})
            patterns.append({"pattern": re.compile(p2, re.I), "type": "BangumiSs"})
            patterns.append({"pattern": re.compile(p3, re.I), "type": "BangumiMd"})

        if self.config.get("space", {}).get("enable", True):
            patterns.append({
                "pattern": re.compile(space_prefix + r'\/(\d+)', re.I),
                "type": "Space",
            })
            patterns.append({
                "pattern": re.compile(bili_prefix + r'\/space\/(\d+)', re.I),
                "type": "Space",
            })

        if self.config.get("opus", {}).get("enable", True):
            patterns.append({
                "pattern": re.compile(bili_prefix + r'\/opus\/(\d+)', re.I),
                "type": "Opus",
            })

        if self.config.get("article", {}).get("enable", True):
            pattern = (
                bili_prefix + r'\/read\/cv(\d+)(?![a-zA-Z0-9])'
                if use_full_url("article")
                else r'(?<![a-zA-Z0-9])cv(\d+)(?![a-zA-Z0-9])'
            )
            patterns.append({"pattern": re.compile(pattern, re.I), "type": "Article"})
            patterns.append({
                "pattern": re.compile(
                    bili_prefix
                    + r'\/read\/mobile(?:\?id=|\/)(\d+)(?![a-zA-Z0-9])',
                    re.I,
                ),
                "type": "Article",
            })

        if self.config.get("audio", {}).get("enable", True):
            pattern = (
                bili_prefix + r'\/audio\/au(\d+)(?![a-zA-Z0-9])'
                if use_full_url("audio")
                else r'(?<![a-zA-Z0-9])au(\d+)(?![a-zA-Z0-9])'
            )
            patterns.append({"pattern": re.compile(pattern, re.I), "type": "Audio"})

            pattern = (
                bili_prefix + r'\/audio\/am(\d+)(?![a-zA-Z0-9])'
                if use_full_url("audio")
                else r'(?<![a-zA-Z0-9])am(\d+)(?![a-zA-Z0-9])'
            )
            patterns.append({"pattern": re.compile(pattern, re.I), "type": "AudioMenu"})

        if self.config.get("short_link", {}).get("enable", True):
            patterns.append({
                "pattern": re.compile(
                    short_prefix
                    + r'b23\.tv(?:\\)?\/([0-9a-zA-Z]+)(?![a-zA-Z0-9])',
                    re.I,
                ),
                "type": "Short",
            })
            patterns.append({
                "pattern": re.compile(
                    short_prefix
                    + r'bili(?:22|23|33)\.cn\/([0-9a-zA-Z]+)(?![a-zA-Z0-9])',
                    re.I,
                ),
                "type": "Short",
            })

        return patterns

    def _deduplicate_links(self, links: List[Link]) -> List[Link]:
        """对提取出的链接列表去重；同一视频有评论链接时，丢弃普通视频链接。"""
        comment_video_ids = {
            self._video_identity(link)
            for link in links
            if link.type == "VideoComment"
        }
        seen = set()
        results = []
        for link in links:
            if link.type == "Video" and self._video_identity(link) in comment_video_ids:
                continue

            normalized = self._link_identity(link)
            if normalized not in seen:
                seen.add(normalized)
                results.append(link)
        return results

    def _video_identity(self, link: Link) -> str:
        return normalize_video_id(link.id)

    def _link_identity(self, link: Link):
        params = tuple(sorted(link.params.items()))
        if link.type in ("Video", "VideoComment"):
            return (link.type, self._video_identity(link), params)
        return (link.type, link.id, params)

    def extract_links(self, content: str) -> List[Link]:
        """从纯文本中提取出所有 B站 链接"""
        return self._extract_content_links(
            content,
            self.text_patterns,
            self.video_url_pattern,
        )

    def _extract_content_links(
        self,
        content: str,
        patterns,
        video_url_pattern,
    ) -> List[Link]:
        results = []
        sanitized_content = self._strip_html_tags(content)
        scan_content = sanitized_content

        if self.config.get("video_comment", {}).get("enable", True):
            comment_links, scan_content = self._extract_video_comment_links(
                sanitized_content,
                video_url_pattern,
            )
            results.extend(comment_links)

        for item in patterns:
            for match in item["pattern"].finditer(scan_content):
                results.append(Link(item["type"], match.group(1)))

        return self._deduplicate_links(results)

    def _extract_video_comment_links(self, content: str, video_url_pattern):
        links = []
        chars = list(content)

        for match in video_url_pattern.finditer(content):
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

    def extract_card_links(self, payload: Any) -> List[Link]:
        """从规范化的 JSON 卡片数据中提取完整 B 站链接。"""
        links = []
        for value in self._iter_card_values(payload):
            links.extend(self._extract_content_links(
                value,
                self.card_patterns,
                self.card_video_url_pattern,
            ))
        return self._deduplicate_links(links)

    def _iter_card_values(self, payload: Any):
        stack = [(payload, 0)]
        while stack:
            value, depth = stack.pop()
            if depth > self.max_json_depth:
                continue
            if isinstance(value, dict):
                stack.extend((item, depth + 1) for item in value.values())
                continue
            if isinstance(value, list):
                stack.extend((item, depth + 1) for item in value)
                continue
            if not isinstance(value, str):
                continue

            if depth >= self.max_json_depth:
                yield value
                continue

            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    nested = json.loads(stripped)
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(nested, (dict, list)):
                        stack.append((nested, depth + 1))
                        continue

            if value:
                yield value

    async def resolve_short_links(self, links: List[Link], api_client) -> List[Link]:
        """将提取出来的短链接转换为真实链接并递归提取 (并发解析)"""

        async def process_link(link: Link, depth=0) -> List[Link]:
            if depth > 3:
                return [link]

            if link.type == "Short":
                redir_url = await api_client.get_short_redir_url(link.id)
                if redir_url:
                    resolved = self._extract_content_links(
                        redir_url,
                        self.card_patterns,
                        self.card_video_url_pattern,
                    )
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
