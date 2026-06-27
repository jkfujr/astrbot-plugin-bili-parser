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

WBI_DEFAULT_WEB_LOCATION = 1550101
WBI_DM_RANDOM_CHARS = "ABCDEFGHIJK"
WBI_DM_IMG_LIST = "[]"
WBI_DM_IMG_INTER = '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'

OPUS_TIMEZONE_OFFSET = -480
OPUS_PLATFORM = "web"
OPUS_GAIA_SOURCE = "main_web"
OPUS_FEATURES = "itemOpusStyle,opusBigCover,onlyfansVote,endFooterHidden,decorationCard,onlyfansAssetsV2,ugcDelete"
OPUS_DEVICE_REQ_JSON = '{"platform":"web","device":"pc"}'
OPUS_WEB_REQ_JSON = '{"spm_id":"333.1368"}'

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
        params["web_location"] = WBI_DEFAULT_WEB_LOCATION
    # 按 key 排序后 url 编码，拼接 mixin_key，取 MD5
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return params


def _add_dm_params(params: dict) -> dict:
    """附加反爬虫鼠标指纹参数（dm_img_*）"""
    params.update({
        "dm_img_list": WBI_DM_IMG_LIST,
        "dm_img_str": "".join(random.sample(WBI_DM_RANDOM_CHARS, 2)),
        "dm_cover_img_str": "".join(random.sample(WBI_DM_RANDOM_CHARS, 2)),
        "dm_img_inter": WBI_DM_IMG_INTER,
    })
    return params


# ==================== API 客户端 ====================


class BiliNetworkError(Exception):
    """B站网络请求失败，包含面向用户的简短错误信息。"""

    def __init__(
        self,
        message: str,
        *,
        url: str,
        proxy_url: str = "",
        cause: Optional[BaseException] = None,
    ):
        super().__init__(message)
        self.url = url
        self.proxy_url = proxy_url
        self.cause = cause


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
        self._total_timeout = self._get_positive_number(network_config, "total_timeout", 10)
        self._connect_timeout = self._get_positive_number(network_config, "connect_timeout", 5)
        self._sock_connect_timeout = self._get_positive_number(network_config, "sock_connect_timeout", 5)
        self._retry_count = self._get_non_negative_int(network_config, "retry_count", 1)
        self._retry_delay = self._get_non_negative_number(network_config, "retry_delay", 0.3)
        self._fallback_direct = bool(network_config.get("fallback_direct", False))
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        # Wbi mixin key 缓存
        self._wbi_mixin_key: str = ""
        self._wbi_key_expire: float = 0
        self._wbi_retry_after: float = 0
        self._wbi_lock = asyncio.Lock()

    def _get_positive_number(self, config: Dict[str, Any], key: str, default: float) -> float:
        value = config.get(key, default)
        if not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0:
            return float(value)
        return float(default)

    def _get_non_negative_number(self, config: Dict[str, Any], key: str, default: float) -> float:
        value = config.get(key, default)
        if not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 0:
            return float(value)
        return float(default)

    def _get_non_negative_int(self, config: Dict[str, Any], key: str, default: int) -> int:
        value = config.get(key, default)
        if not isinstance(value, bool) and isinstance(value, int) and value >= 0:
            return value
        return default

    def _build_timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(
            total=self._total_timeout,
            connect=self._connect_timeout,
            sock_connect=self._sock_connect_timeout,
            sock_read=self._total_timeout,
        )

    def _create_session(self, *, use_proxy: bool = True) -> aiohttp.ClientSession:
        """创建 HTTP 会话，按配置统一接入代理"""
        kwargs: Dict[str, Any] = {"timeout": self._build_timeout()}
        if use_proxy and self._proxy_url:
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

    async def _require_session(self) -> aiohttp.ClientSession:
        """返回已初始化的 session，供静态类型检查收窄 Optional。"""
        await self._ensure_session()
        if self._session is None:
            raise RuntimeError("Bili API session not initialized")
        return self._session

    def _build_headers(self, referer: Optional[str] = None, use_cookie: bool = True) -> dict:
        """构建通用请求头"""
        headers = {
            "User-Agent": self._user_agent,
            "Referer": referer or "https://www.bilibili.com",
        }
        cookie = self._cookie.get_cookie() if use_cookie else ""
        if cookie:
            headers["Cookie"] = cookie
        return headers

    # ---- Wbi 签名 ----

    async def _fetch_wbi_nav(self, nav_url: str) -> Dict[str, Any]:
        return await self._request_json(
            "GET",
            nav_url,
            headers=self._build_headers(),
        )

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

            nav_url = "https://api.bilibili.com/x/web-interface/nav"
            try:
                data = await self._fetch_wbi_nav(nav_url)
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
            except BiliNetworkError as e:
                logger.warning(f"[BiliParser] 获取 Wbi mixin key 失败: {e}")
                self._wbi_mixin_key = ""
                self._wbi_key_expire = 0
                self._wbi_retry_after = now + 60
            except Exception as e:
                logger.error(f"[BiliParser] 获取 Wbi mixin key 失败: {e}")
                self._wbi_mixin_key = ""
                self._wbi_key_expire = 0
                self._wbi_retry_after = now + 60

        return self._wbi_mixin_key

    # ---- HTTP 请求方法 ----

    def _network_error_text(self, error: BaseException) -> str:
        error_type = type(error).__name__
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return "B站请求超时，请检查插件代理配置或网络连通性。"
        if isinstance(error, aiohttp.ClientProxyConnectionError):
            return "B站代理连接失败，请检查插件代理地址是否可用。"
        if isinstance(error, aiohttp.ClientConnectorError):
            return "B站连接失败，请检查插件代理配置或网络连通性。"
        if isinstance(error, aiohttp.ClientError):
            return f"B站网络请求失败（{error_type}），请检查插件代理配置。"
        if isinstance(error, OSError):
            return f"B站网络连接失败（{error_type}），请检查网络连通性。"
        return f"B站请求失败（{error_type}）。"

    def _log_network_error(self, url: str, error: BaseException, attempt: int, max_attempts: int, use_proxy: bool):
        proxy_text = self._proxy_url if use_proxy and self._proxy_url else "直连"
        logger.warning(
            f"[BiliParser] 网络请求失败 {url}，方式={proxy_text}，"
            f"尝试={attempt}/{max_attempts}，异常={type(error).__name__}: {error}"
        )

    async def _request_json_once(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        allow_redirects: bool = True,
        use_proxy: bool = True,
    ) -> Dict[str, Any]:
        session = await self._require_session() if use_proxy else self._create_session(use_proxy=False)
        try:
            async with session.request(
                method,
                url,
                params=params,
                headers=headers,
                allow_redirects=allow_redirects,
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        finally:
            if not use_proxy:
                await session.close()

    async def _request_url_once(
        self,
        method: str,
        url: str,
        *,
        allow_redirects: bool = True,
        use_proxy: bool = True,
    ) -> str:
        session = await self._require_session() if use_proxy else self._create_session(use_proxy=False)
        try:
            async with session.request(method, url, allow_redirects=allow_redirects) as resp:
                return str(resp.url)
        finally:
            if not use_proxy:
                await session.close()

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        allow_redirects: bool = True,
    ) -> Dict[str, Any]:
        max_attempts = self._retry_count + 1
        last_error: Optional[BaseException] = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._request_json_once(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    allow_redirects=allow_redirects,
                    use_proxy=True,
                )
            except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientError, OSError) as e:
                last_error = e
                self._log_network_error(url, e, attempt, max_attempts, True)
                if attempt < max_attempts and self._retry_delay > 0:
                    await asyncio.sleep(self._retry_delay)

        if self._fallback_direct and self._proxy_url:
            logger.warning(f"[BiliParser] 代理请求失败，尝试直连回退: {url}")
            try:
                return await self._request_json_once(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    allow_redirects=allow_redirects,
                    use_proxy=False,
                )
            except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientError, OSError) as e:
                last_error = e
                self._log_network_error(url, e, 1, 1, False)

        message = self._network_error_text(last_error) if last_error else "B站网络请求失败。"
        raise BiliNetworkError(message, url=url, proxy_url=self._proxy_url, cause=last_error)

    async def _request_url(self, method: str, url: str, *, allow_redirects: bool = True) -> str:
        max_attempts = self._retry_count + 1
        last_error: Optional[BaseException] = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._request_url_once(
                    method,
                    url,
                    allow_redirects=allow_redirects,
                    use_proxy=True,
                )
            except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientError, OSError) as e:
                last_error = e
                self._log_network_error(url, e, attempt, max_attempts, True)
                if attempt < max_attempts and self._retry_delay > 0:
                    await asyncio.sleep(self._retry_delay)

        if self._fallback_direct and self._proxy_url:
            logger.warning(f"[BiliParser] 代理请求失败，尝试直连回退: {url}")
            try:
                return await self._request_url_once(
                    method,
                    url,
                    allow_redirects=allow_redirects,
                    use_proxy=False,
                )
            except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientError, OSError) as e:
                last_error = e
                self._log_network_error(url, e, 1, 1, False)

        message = self._network_error_text(last_error) if last_error else "B站网络请求失败。"
        raise BiliNetworkError(message, url=url, proxy_url=self._proxy_url, cause=last_error)

    async def _get(
        self,
        url: str,
        params: Optional[dict] = None,
        referer: Optional[str] = None,
        use_cookie: bool = True,
    ) -> Dict[str, Any]:
        """普通 GET 请求"""
        return await self._request_json(
            "GET",
            url,
            params=params,
            headers=self._build_headers(referer, use_cookie),
        )

    async def _get_with_wbi(self, url: str, params: dict) -> Dict[str, Any]:
        """带 Wbi 签名 + 设备指纹的 GET 请求"""
        mixin_key = await self._get_wbi_mixin_key()
        if mixin_key:
            params = _add_dm_params(params)
            params = _sign_wbi_params(params, mixin_key)

        return await self._request_json(
            "GET",
            url,
            params=params,
            headers=self._build_headers(),
        )

    # ---- 各类型资源 API ----

    async def fetch_video(self, id_str: str, use_cookie: bool = True) -> Dict[str, Any]:
        """获取视频信息"""
        av_match = re.match(r'av([0-9]+)', id_str, re.IGNORECASE)
        bv_match = re.match(r'bv([0-9a-zA-Z]+)', id_str, re.IGNORECASE)

        if av_match:
            url = f"https://api.bilibili.com/x/web-interface/view?aid={av_match.group(1)}"
        elif bv_match:
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv_match.group(1)}"
        else:
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={id_str}"

        return await self._get(url, use_cookie=use_cookie)

    async def fetch_video_comment(
        self,
        video_id: str,
        root_id: str,
        use_cookie: bool = False,
    ) -> Dict[str, Any]:
        """获取视频评论详情"""
        video_data = await self.fetch_video(video_id, use_cookie=use_cookie)
        if video_data.get("code") != 0:
            return video_data

        video = video_data.get("data") or {}
        aid = video.get("aid")
        if not aid:
            raise ValueError("视频 aid 缺失")

        params = {
            "type": 1,
            "oid": aid,
            "root": root_id,
        }
        comment_url = f"https://www.bilibili.com/video/{video.get('bvid', video_id)}?comment_root_id={root_id}"
        comment_data = await self._get(
            "https://api.bilibili.com/x/v2/reply/detail",
            params=params,
            referer=comment_url,
            use_cookie=use_cookie,
        )
        if comment_data.get("code") == 0:
            data = comment_data.setdefault("data", {})
            data["video"] = video
            data["root_id"] = root_id
            data["comment_url"] = comment_url
        return comment_data

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
            "timezone_offset": OPUS_TIMEZONE_OFFSET,
            "platform": OPUS_PLATFORM,
            "gaia_source": OPUS_GAIA_SOURCE,
            "features": OPUS_FEATURES,
            "x-bili-device-req-json": OPUS_DEVICE_REQ_JSON,
            "x-bili-web-req-json": OPUS_WEB_REQ_JSON,
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
        try:
            return await self._request_url("HEAD", url, allow_redirects=True)
        except BiliNetworkError as e:
            logger.warning(f"[BiliParser] 短链接解析失败 {url}: {e}")
            return ""
