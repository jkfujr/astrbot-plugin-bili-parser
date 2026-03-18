from astrbot.api import logger
import aiohttp
import re
import asyncio
import random
from typing import Dict, Any, Optional, List

class BiliAPIClient:
    def __init__(self, config: Dict[str, Any]):
        # 从 basic 配置中读取 user_agent
        basic_config = config.get("basic", {})
        self.user_agent = basic_config.get("user_agent", "Mozilla/5.0")
        
        self.cookie_config = config.get("cookie", {})
        self.cookie_mode = self.cookie_config.get("mode", "none")
        
        # Cookie 管理器相关
        self.cookie_pool: List[str] = []
        self._refresh_task = None
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        """启动 Cookie 管理任务"""
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
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
        
        if self._session:
            await self._session.close()
            self._session = None

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
            # 使用共享 session
            if not self._session:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
                
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
                if not isinstance(interval, (int, float)) or interval <= 0:
                    interval = 30
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
            # "Host": host # aiohttp will set Host automatically
        }
        
        # 添加 Cookie
        cookie = self._get_random_cookie()
        if cookie:
            headers["Cookie"] = cookie

        try:
            # 使用共享 session
            if not self._session:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
                
            async with self._session.get(url, headers=headers) as resp:
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
        # 优先直接请求网页获取 __INITIAL_STATE__，因为新版动态 API v1/detail 会漏掉未登录状态下的纯文字等详情数据
        logger.info(f"[BiliParser] 尝试解析网页数据: {id_str}")
        web_url = f"https://www.bilibili.com/opus/{id_str}"
        try:
            headers = {"User-Agent": self.user_agent}
            cookie = self._get_random_cookie()
            if cookie:
                headers["Cookie"] = cookie
                
            if not self._session:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
            
            async with self._session.get(web_url, headers=headers) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    import re, json
                    # 使用更为宽泛的正则表达式匹配 __INITIAL_STATE__，避开大括号与 script 特性干扰
                    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*});\s*(?:</script>|\(function)', html)
                    # 备用宽松匹配
                    if not match:
                        match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html)
                        
                    if match:
                        state_data = json.loads(match.group(1))
                        detail = state_data.get('detail', {})
                        
                        # 构建一个兼容 API `data.item` 的模拟结构
                        mock_item = {"id_str": id_str, "modules": {
                            "module_author": {}, "module_dynamic": {"major": {"type": "MAJOR_TYPE_DRAW", "draw": {"items": []}}, "desc": {"text": ""}}, "module_stat": {"comment": {"count": 0}, "forward": {"count": 0}, "like": {"count": 0}}
                        }}
                        
                        modules = detail.get('modules', [])
                        text_parts = []
                        for mod in modules:
                            mtype = mod.get('module_type')
                            if mtype == 'MODULE_TYPE_AUTHOR':
                                mock_item["modules"]["module_author"]["name"] = mod.get("module_author", {}).get("name", "")
                                # 尝试从复杂的 layers 中提取头像 URL
                                face_url = ""
                                try:
                                    avatar = mod.get("module_author", {}).get("avatar", {})
                                    layers = avatar.get("fallback_layers", {}).get("layers", [])
                                    if layers:
                                        res_image = layers[0].get("resource", {}).get("res_image", {})
                                        face_url = res_image.get("image_src", {}).get("remote", {}).get("url", "")
                                        if not face_url:
                                            # 有些在 avatar 下直接有面部 url，为了保险兜底
                                            face_url = mod.get("module_author", {}).get("face", "")
                                except Exception:
                                    pass
                                mock_item["modules"]["module_author"]["face"] = face_url or mod.get("module_author", {}).get("face", "")
                            elif mtype == 'MODULE_TYPE_CONTENT':
                                paras = mod.get('module_content', {}).get('paragraphs', [])
                                for p in paras:
                                    if 'text' in p:
                                        for n in p['text'].get('nodes', []):
                                            if 'word' in n and 'words' in n['word']:
                                                text_parts.append(n['word']['words'])
                                    if 'pic' in p:
                                        pics = p['pic'].get('pics', [])
                                        for pic in pics:
                                            mock_item["modules"]["module_dynamic"]["major"]["draw"]["items"].append({"src": pic.get('url')})
                            elif mtype == 'MODULE_TYPE_STAT':
                                stat = mod.get('module_stat', {})
                                mock_item["modules"]["module_stat"]["like"]["count"] = stat.get("like", {}).get("count", 0)
                                mock_item["modules"]["module_stat"]["comment"]["count"] = stat.get("comment", {}).get("count", 0)
                                mock_item["modules"]["module_stat"]["forward"]["count"] = stat.get("forward", {}).get("count", 0)
                        
                        mock_item["modules"]["module_dynamic"]["desc"]["text"] = "\n".join(text_parts)
                        
                        # 兼容无图情况，避免 major.draw.items[0] 数组越界
                        if not mock_item["modules"]["module_dynamic"]["major"]["draw"]["items"]:
                            mock_item["modules"]["module_dynamic"]["major"]["type"] = "MAJOR_TYPE_NONE"
                        
                        # 兼容原模板：如果从 INITIAL_STATE 获取到了数据，伪装成 code=0 的合法返回
                        return {"code": 0, "message": "0", "data": {"item": mock_item}}
                    else:
                        logger.warning(f"[BiliParser] 网页解析未能提取到 INITIAL_STATE 数据！页面前2000字符如下:\n{html[:2000]}")
                else:
                    logger.warning(f"[BiliParser] 网页获取异常，状态码: {resp.status}")
        except Exception as e:
            logger.error(f"[BiliParser] 网页解析异常，准备降级尝试 API 请求: {e}")
            
        # 网页解析失败或未匹配，降级回退请求官方 API
        logger.info(f"[BiliParser] 降级请求官方 API: {id_str}")
        url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={id_str}"
        resp_data = await self._get(url, "api.bilibili.com")
        
        return resp_data

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
            # 使用共享 session
            if not self._session:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
                
            async with self._session.head(url, allow_redirects=True) as resp:
                return str(resp.url)
        except Exception:
            return ""
