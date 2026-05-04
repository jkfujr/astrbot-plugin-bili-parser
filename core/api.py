"""
B站 API 客户端

包含 Wbi 签名基础设施和各类型资源的 API 调用。
"""

from astrbot.api import logger
import aiohttp
from aiohttp_socks import ProxyConnector
import asyncio
import hashlib
import re
import random
import time
import urllib.parse
from functools import reduce
from typing import Dict, Any, Optional

from .cookie import CookieManager

# ==================== Wbi 签名基础设施 ====================

# Wbi mixin key 混淆索引表（源自 bilibili-api-collect 逆向）
_WBI_SHUFFLE_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def _calc_mixin_key(img_key: str, sub_key: str) -> str:
    """通过 img_key + sub_key 混淆生成 32 字符的 mixin key"""
    raw = img_key + sub_key
    return reduce(lambda s, i: s + (raw[i] if i < len(raw) else ""), _WBI_SHUFFLE_TABLE, "")[:32]


def _sign_wbi_params(params: dict, mixin_key: str) -> dict:
    """对请求参数附加 Wbi 签名（wts + w_rid）"""
    params["wts"] = int(time.time())
    if not params.get("web_location"):
        params["web_location"] = 1550101
    # 按 key 排序后 url 编码，拼接 mixin_key，取 MD5
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return params


def _add_dm_params(params: dict) -> dict:
    """附加反爬虫鼠标指纹参数（dm_img_*）"""
    dm_rand = "ABCDEFGHIJK"
    params.update({
        "dm_img_list": "[]",
        "dm_img_str": "".join(random.sample(dm_rand, 2)),
        "dm_cover_img_str": "".join(random.sample(dm_rand, 2)),
        "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
    })
    return params


# ==================== API 客户端 ====================


class BiliAPIClient:
    """B站 API 客户端"""

    def __init__(
        self,
        user_agent: str,
        cookie_manager: CookieManager,
        network_config: Dict[str, Any],
    ):
        self._user_agent = user_agent
        self._cookie = cookie_manager
        self._proxy_url = network_config.get("proxy_url", "").strip()
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        # Wbi mixin key 缓存
        self._wbi_mixin_key: str = ""
        self._wbi_key_expire: float = 0
        self._wbi_retry_after: float = 0
        self._wbi_lock = asyncio.Lock()

    def _create_session(self) -> aiohttp.ClientSession:
        """创建 HTTP 会话，按配置统一接入代理"""
        kwargs = {"timeout": aiohttp.ClientTimeout(total=10)}
        if self._proxy_url:
            proxy_url = self._proxy_url
            proxy_url_lower = proxy_url.lower()
            if proxy_url_lower.startswith("socks5h://"):
                proxy_url = "socks5://" + proxy_url[len("socks5h://"):]
                kwargs["connector"] = ProxyConnector.from_url(proxy_url, rdns=True)
            elif proxy_url_lower.startswith(("socks4://", "socks5://")):
                kwargs["connector"] = ProxyConnector.from_url(proxy_url)
            else:
                kwargs["proxy"] = proxy_url
        return aiohttp.ClientSession(**kwargs)

    async def start(self):
        """初始化 HTTP 会话"""
        await self._ensure_session()

    async def stop(self):
        """关闭 HTTP 会话"""
        async with self._session_lock:
            if self._session:
                await self._session.close()
                self._session = None

    async def _ensure_session(self):
        """确保 session 存在"""
        if self._session and not self._session.closed:
            return

        async with self._session_lock:
            if not self._session or self._session.closed:
                self._session = self._create_session()

    def _build_headers(self) -> dict:
        """构建通用请求头"""
        headers = {
            "User-Agent": self._user_agent,
            "Referer": "https://www.bilibili.com",
        }
        cookie = self._cookie.get_cookie()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    # ---- Wbi 签名 ----

    async def _get_wbi_mixin_key(self) -> str:
        """获取 Wbi mixin key（带缓存，每 30 分钟刷新一次）"""
        now = time.time()
        if self._wbi_mixin_key and now < self._wbi_key_expire:
            return self._wbi_mixin_key
        if now < self._wbi_retry_after:
            return ""

        async with self._wbi_lock:
            now = time.time()
            if self._wbi_mixin_key and now < self._wbi_key_expire:
                return self._wbi_mixin_key
            if now < self._wbi_retry_after:
                return ""

            # 请求导航接口获取 wbi_img
            await self._ensure_session()
            nav_url = "https://api.bilibili.com/x/web-interface/nav"
            try:
                async with self._session.get(nav_url, headers=self._build_headers()) as resp:
                    data = await resp.json()
                    data_dict = data.get("data") or {}
                    wbi_img = data_dict.get("wbi_img") or {}
                    img_url = wbi_img.get("img_url", "")
                    sub_url = wbi_img.get("sub_url", "")
                    # 从 URL 中提取文件名（不含扩展名）作为 key
                    img_key = img_url.rsplit("/", 1)[-1].split(".")[0] if img_url else ""
                    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0] if sub_url else ""
                    if not img_key or not sub_key:
                        self._wbi_mixin_key = ""
                        self._wbi_key_expire = 0
                        self._wbi_retry_after = now + 60
                        logger.warning("[BiliParser] 获取 Wbi mixin key 失败: nav 接口未返回有效 wbi_img")
                        return ""
                    self._wbi_mixin_key = _calc_mixin_key(img_key, sub_key)
                    self._wbi_key_expire = now + 1800  # 缓存 30 分钟
                    self._wbi_retry_after = 0
                    logger.info(f"[BiliParser] 已获取 Wbi mixin key: {self._wbi_mixin_key[:8]}...")
            except Exception as e:
                logger.error(f"[BiliParser] 获取 Wbi mixin key 失败: {e}")
                self._wbi_mixin_key = ""
                self._wbi_key_expire = 0
                self._wbi_retry_after = now + 60

        return self._wbi_mixin_key

    # ---- HTTP 请求方法 ----

    async def _get(self, url: str) -> Dict[str, Any]:
        """普通 GET 请求"""
        await self._ensure_session()
        try:
            async with self._session.get(url, headers=self._build_headers()) as resp:
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            logger.error(f"[BiliParser] 请求失败 {url}: {e}")
            raise

    async def _get_with_wbi(self, url: str, params: dict) -> Dict[str, Any]:
        """带 Wbi 签名 + 设备指纹的 GET 请求"""
        mixin_key = await self._get_wbi_mixin_key()
        if mixin_key:
            params = _add_dm_params(params)
            params = _sign_wbi_params(params, mixin_key)

        await self._ensure_session()
        try:
            async with self._session.get(url, params=params, headers=self._build_headers()) as resp:
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            logger.error(f"[BiliParser] Wbi 请求失败 {url}: {e}")
            raise

    # ---- 各类型资源 API ----

    async def fetch_video(self, id_str: str) -> Dict[str, Any]:
        """获取视频信息"""
        av_match = re.match(r'av([0-9]+)', id_str, re.IGNORECASE)
        bv_match = re.match(r'bv([0-9a-zA-Z]+)', id_str, re.IGNORECASE)

        if av_match:
            url = f"https://api.bilibili.com/x/web-interface/view?aid={av_match.group(1)}"
        elif bv_match:
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv_match.group(1)}"
        else:
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={id_str}"

        return await self._get(url)

    async def fetch_live(self, id_str: str) -> Dict[str, Any]:
        """获取直播间信息"""
        url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={id_str}"
        return await self._get(url)

    async def fetch_bangumi_ep_ss(self, id_str: str) -> Dict[str, Any]:
        """获取番剧 EP/SS 信息"""
        ep_match = re.match(r'ep([0-9]+)', id_str, re.IGNORECASE)
        ss_match = re.match(r'ss([0-9]+)', id_str, re.IGNORECASE)

        if ep_match:
            url = f"https://api.bilibili.com/pgc/view/web/season?ep_id={ep_match.group(1)}"
        elif ss_match:
            url = f"https://api.bilibili.com/pgc/view/web/season?season_id={ss_match.group(1)}"
        else:
            if id_str.isdigit():
                url = f"https://api.bilibili.com/pgc/view/web/season?season_id={id_str}"
            else:
                raise ValueError(f"Unknown bangumi type: {id_str}")

        ret = await self._get(url)
        if 'result' in ret:
            ret['data'] = ret['result']
        return ret

    async def fetch_bangumi_md(self, id_str: str) -> Dict[str, Any]:
        """获取番剧 MD 信息"""
        media_id = re.sub(r'^md', '', id_str, flags=re.IGNORECASE)
        md_url = f"https://api.bilibili.com/pgc/review/user?media_id={media_id}"

        md_info = await self._get(md_url)
        result = md_info.get('result') or {}
        if not result:
            raise ValueError("Fetch bangumi information via mdid failed!")

        media = result.get('media') or {}
        season_id = media.get('season_id')
        if not season_id:
            raise ValueError("Fetch bangumi season_id via mdid failed!")
        url = f"https://api.bilibili.com/pgc/view/web/season?season_id={season_id}"

        ret = await self._get(url)
        if 'result' in ret:
            ret['data'] = ret['result']
        return ret

    async def fetch_article(self, id_str: str) -> Dict[str, Any]:
        """获取专栏信息"""
        url = f"https://api.bilibili.com/x/article/viewinfo?id={id_str}"
        return await self._get(url)

    async def fetch_opus(self, id_str: str) -> Dict[str, Any]:
        """获取动态信息（通过 Polymer API + Wbi 签名）"""
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
        params = {
            "id": id_str,
            "timezone_offset": -480,
            "platform": "web",
            "gaia_source": "main_web",
            "features": "itemOpusStyle,opusBigCover,onlyfansVote,endFooterHidden,decorationCard,onlyfansAssetsV2,ugcDelete",
            "x-bili-device-req-json": '{"platform":"web","device":"pc"}',
            "x-bili-web-req-json": '{"spm_id":"333.1368"}',
        }
        resp_data = await self._get_with_wbi(url, params)

        # 对风控返回给出明确警告
        if resp_data.get("code") == -352:
            logger.warning("[BiliParser] Polymer API 返回 -352 风控，请配置有效的 Cookie。")
        elif resp_data.get("code") != 0:
            logger.warning(f"[BiliParser] Polymer API 返回异常: code={resp_data.get('code')}, msg={resp_data.get('message')}")

        return resp_data

    async def fetch_space(self, id_str: str) -> Dict[str, Any]:
        """获取空间信息"""
        url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid={id_str}"
        return await self._get(url)

    async def fetch_audio(self, id_str: str) -> Dict[str, Any]:
        """获取音频信息"""
        url = f"https://www.bilibili.com/audio/music-service-c/web/song/info?sid={id_str}"
        return await self._get(url)

    async def fetch_audio_menu(self, id_str: str) -> Dict[str, Any]:
        """获取歌单信息"""
        url = f"https://www.bilibili.com/audio/music-service-c/web/menu/info?sid={id_str}"
        return await self._get(url)

    async def get_short_redir_url(self, short_id: str) -> str:
        """获取短链接跳转真实地址"""
        url = f"https://b23.tv/{short_id}"
        await self._ensure_session()
        try:
            async with self._session.head(url, allow_redirects=True) as resp:
                return str(resp.url)
        except Exception:
            return ""
