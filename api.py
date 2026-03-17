import aiohttp
import logging
import re
import asyncio
import random
from typing import Dict, Any, Optional, List

logger = logging.getLogger("astrbot_plugin_bili_parser")

class BiliAPIClient:
    def __init__(self, config: Dict[str, Any]):
        # 从 basic 配置中获取 user_agent，兼容旧配置
        basic_config = config.get("basic", {})
        self.user_agent = basic_config.get("user_agent", "Mozilla/5.0")
        
        self.cookie_config = config.get("cookie", {})
        self.cookie_mode = self.cookie_config.get("mode", "none")
        
        # Cookie 管理器相关
        self.cookie_pool: List[str] = []
        self._refresh_task = None
        self._running = False

    async def start(self):
        """启动 Cookie 管理任务"""
        if self.cookie_mode == "manager":
            self._running = True
            await self._refresh_cookies()
            self._refresh_task = asyncio.create_task(self._auto_refresh_loop())

    async def stop(self):
        """停止 Cookie 管理任务"""
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    def _get_random_cookie(self) -> str:
        """获取一个可用的 Cookie"""
        if self.cookie_mode == "manual":
            return self.cookie_config.get("manual_cookie", "")
        elif self.cookie_mode == "manager":
            if self.cookie_pool:
                return random.choice(self.cookie_pool)
        return ""

    async def _update_cookies_from_manager(self):
        """从 Cookie 管理器更新 Cookie 池"""
        manager_url = self.cookie_config.get("manager_url")
        if not manager_url:
            logger.warning("Cookie manager URL not configured")
            return

        token = self.cookie_config.get("manager_token")
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            # 确保 URL 格式正确
            api_url = f"{manager_url.rstrip('/')}/cookies/"
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, headers=headers) as resp:
                    if resp.status == 200:
                        cookies_data = await resp.json()
                        valid_cookies = []
                        for cookie_obj in cookies_data:
                            managed = cookie_obj.get("managed", {})
                            if managed.get("is_enabled") and managed.get("status") == "valid":
                                header_string = managed.get("header_string")
                                if header_string:
                                    valid_cookies.append(header_string)
                        
                        self.cookie_pool = valid_cookies
                        logger.info(f"Updated {len(valid_cookies)} cookies from manager")
                    else:
                        logger.error(f"Failed to fetch cookies from manager: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"Error updating cookies from manager: {e}")

    async def _refresh_cookies(self):
        """执行刷新逻辑"""
        await self._update_cookies_from_manager()

    async def _auto_refresh_loop(self):
        """自动刷新循环"""
        while self._running:
            try:
                interval = self.cookie_config.get("update_interval", 30)
                await asyncio.sleep(interval * 60)
                await self._refresh_cookies()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cookie refresh loop: {e}")
                await asyncio.sleep(60)  # 出错后等待1分钟重试

    async def _get(self, url: str, host: str) -> Dict[str, Any]:
        headers = {
            "User-Agent": self.user_agent,
            "Host": host
        }
        
        # 添加 Cookie
        cookie = self._get_random_cookie()
        if cookie:
            headers["Cookie"] = cookie

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except Exception as e:
            logger.error(f"BiliAPIClient Failed to fetch {url}: {e}")
            raise

    async def fetch_video(self, id_str: str) -> Dict[str, Any]:
        """获取视频信息"""
        av_match = re.match(r'av([0-9]+)', id_str, re.IGNORECASE)
        bv_match = re.match(r'bv([0-9a-zA-Z]+)', id_str, re.IGNORECASE)
        
        if av_match:
            url = f"https://api.bilibili.com/x/web-interface/view?aid={av_match.group(1)}"
        elif bv_match:
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv_match.group(1)}"
        else:
            # 默认视为 BV 号
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={id_str}"
            
        return await self._get(url, "api.bilibili.com")

    async def fetch_live(self, id_str: str) -> Dict[str, Any]:
        """获取直播间信息"""
        url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={id_str}"
        return await self._get(url, "api.live.bilibili.com")

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

        ret = await self._get(url, "api.bilibili.com")
        if 'result' in ret:
            ret['data'] = ret['result']
        return ret

    async def fetch_bangumi_md(self, id_str: str) -> Dict[str, Any]:
        """获取番剧 MD 信息"""
        media_id = re.sub(r'^md', '', id_str, flags=re.IGNORECASE)
        md_url = f"https://api.bilibili.com/pgc/review/user?media_id={media_id}"
        
        md_info = await self._get(md_url, "api.bilibili.com")
        if not md_info.get('result'):
            raise ValueError("Fetch bangumi information via mdid failed!")
            
        season_id = md_info['result']['media']['season_id']
        url = f"https://api.bilibili.com/pgc/view/web/season?season_id={season_id}"
        
        ret = await self._get(url, "api.bilibili.com")
        if 'result' in ret:
            ret['data'] = ret['result']
        return ret

    async def fetch_article(self, id_str: str) -> Dict[str, Any]:
        """获取专栏信息"""
        url = f"https://api.bilibili.com/x/article/viewinfo?id={id_str}"
        return await self._get(url, "api.bilibili.com")

    async def fetch_opus(self, id_str: str) -> Dict[str, Any]:
        """获取动态信息"""
        url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={id_str}"
        return await self._get(url, "api.bilibili.com")

    async def fetch_space(self, id_str: str) -> Dict[str, Any]:
        """获取空间信息"""
        url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid={id_str}"
        return await self._get(url, "api.bilibili.com")

    async def fetch_audio(self, id_str: str) -> Dict[str, Any]:
        """获取音频信息"""
        url = f"https://www.bilibili.com/audio/music-service-c/web/song/info?sid={id_str}"
        return await self._get(url, "www.bilibili.com")

    async def fetch_audio_menu(self, id_str: str) -> Dict[str, Any]:
        """获取歌单信息"""
        url = f"https://www.bilibili.com/audio/music-service-c/web/menu/info?sid={id_str}"
        return await self._get(url, "www.bilibili.com")

    async def get_short_redir_url(self, short_id: str) -> str:
        """获取短链接跳转真实地址"""
        url = f"https://b23.tv/{short_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(url, allow_redirects=True) as resp:
                    return str(resp.url)
        except Exception:
            return ""
