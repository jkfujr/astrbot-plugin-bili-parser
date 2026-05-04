"""
Cookie 池管理

负责 Cookie 的获取、池化和定时刷新，支持手动配置和外部管理器两种模式。
"""

from astrbot.api import logger
import aiohttp
import asyncio
import random
from typing import List, Optional, Dict, Any


class CookieManager:
    """Cookie 池管理器"""

    def __init__(self, cookie_config: Dict[str, Any]):
        self._config = cookie_config
        self._mode = cookie_config.get("mode", "none")
        self._pool: List[str] = []
        self._refresh_task: Optional[asyncio.Task] = None
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._lifecycle_lock = asyncio.Lock()

    async def start(self):
        """启动管理器（仅 manager 模式需要）"""
        async with self._lifecycle_lock:
            if self._mode != "manager" or self._running:
                return

            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10)
                )
            self._running = True
            await self._refresh()
            self._refresh_task = asyncio.create_task(self._auto_refresh_loop())

    async def stop(self):
        """停止管理器"""
        async with self._lifecycle_lock:
            self._running = False
            if self._refresh_task:
                self._refresh_task.cancel()
                try:
                    await self._refresh_task
                except asyncio.CancelledError:
                    pass
                finally:
                    self._refresh_task = None
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    def get_cookie(self) -> str:
        """获取一个可用的 Cookie 字符串"""
        if self._mode == "manual":
            return self._config.get("manual_cookie", "")
        elif self._mode == "manager":
            if self._pool:
                return random.choice(self._pool)
        return ""

    async def _refresh(self):
        """从外部管理器拉取 Cookie 池"""
        if not self._session or self._session.closed:
            logger.error("[CookieManager] Cookie session not initialized")
            return

        manager_url = self._config.get("manager_url")
        if not manager_url:
            logger.warning("[CookieManager] Cookie manager URL not configured")
            return

        token = self._config.get("manager_token")
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            api_url = f"{manager_url.rstrip('/')}/cookies/"
            async with self._session.get(api_url, headers=headers) as resp:
                if resp.status == 200:
                    cookies_data = await resp.json()
                    valid_cookies = []
                    for cookie_obj in cookies_data:
                        managed = cookie_obj.get("managed", {})
                        if managed.get("is_enabled") and managed.get("status") == "valid":
                            header_string = managed.get("header_string")
                            if header_string:
                                valid_cookies.append(header_string)
                    self._pool = valid_cookies
                    logger.info(f"[CookieManager] 已更新 {len(valid_cookies)} 个 Cookie")
                else:
                    logger.error(f"[CookieManager] 拉取 Cookie 失败: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"[CookieManager] 更新 Cookie 异常: {e}")

    async def _auto_refresh_loop(self):
        """定时自动刷新循环"""
        while self._running:
            try:
                interval = self._config.get("update_interval", 30)
                if not isinstance(interval, (int, float)) or interval <= 0:
                    interval = 30
                await asyncio.sleep(interval * 60)
                await self._refresh()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CookieManager] 刷新循环异常: {e}")
                await asyncio.sleep(60)
