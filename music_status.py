import os
import time
import json
import asyncio
import httpx
import logging

logger = logging.getLogger('music_status')

NETEASE_API_BASE = "http://127.0.0.1:3002"
POLL_INTERVAL = 15  # 一起听模式可以更频繁轮询（15秒）
STATUS_FILE = "/home/ubuntu/caeron-gateway/music_status.json"
COOKIE_FILE = "/home/ubuntu/caeron-gateway/netease_cookie.txt"

DURATION_FILE = "/home/ubuntu/caeron-gateway/music_duration.json"

class ListenTogetherWatcher:
    def __init__(self):
        self.cookie = self._load_cookie()
        self.current_song_id = None
        self.current_play_status = None
        self.current_room_id = None
        self.last_update_time = 0
        self.total_together_seconds = self._load_duration()
        self.is_running = False
        
    def _load_duration(self):
        base_seconds = 6139 * 3600 + 20 * 60 # 6139小时20分钟 = 22101600秒
        if os.path.exists(DURATION_FILE):
            try:
                with open(DURATION_FILE, "r") as f:
                    data = json.load(f)
                    stored_seconds = data.get("total_seconds", 0)
                    if stored_seconds < base_seconds:
                        return base_seconds + stored_seconds
                    return stored_seconds
            except Exception as e:
                logger.error(f"[Music] 读取时长失败: {e}")
        return base_seconds

    def _save_duration(self):
        try:
            with open(DURATION_FILE, "w") as f:
                json.dump({"total_seconds": self.total_together_seconds}, f)
        except Exception as e:
            logger.error(f"[Music] 保存时长失败: {e}")

    def _load_cookie(self):
        if os.path.exists(COOKIE_FILE):
            try:
                with open(COOKIE_FILE, "r") as f:
                    return f.read().strip()
            except Exception as e:
                logger.error(f"[Music] 读取 Cookie 失败: {e}")
        return None
        
    def _save_cookie(self, cookie_str: str):
        self.cookie = cookie_str
        try:
            with open(COOKIE_FILE, "w") as f:
                f.write(cookie_str)
            logger.info("[Music] Cookie 已保存")
        except Exception as e:
            logger.error(f"[Music] 保存 Cookie 失败: {e}")

    async def _api_get(self, path: str, params: dict = None) -> dict:
        if not self.cookie:
            return {}
        if params is None:
            params = {}
        params["timestamp"] = int(time.time() * 1000)
        params["cookie"] = self.cookie
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{NETEASE_API_BASE}{path}", params=params, timeout=10.0)
                if resp.status_code == 200:
                    return resp.json()
                else:
                    logger.warning(f"[Music] API {path} 请求失败，状态码: {resp.status_code}")
                    return {}
        except Exception as e:
            logger.error(f"[Music] API {path} 请求异常: {e}")
            return {}

    async def get_listen_together_status(self):
        """获取一起听房间状态和当前播放歌曲"""
        status_data = await self._api_get("/listentogether/status")
        if not status_data or status_data.get("code") != 200:
            return None, None
            
        data = status_data.get("data", {})
        if not data.get("inRoom"):
            return False, None
            
        room_id = data.get("roomInfo", {}).get("roomId")
        if not room_id:
            return False, None
            
        self.current_room_id = room_id
        
        # 获取房间同步播放列表状态
        sync_data = await self._api_get("/listentogether/sync/playlist/get", {"roomId": room_id})
        if not sync_data or sync_data.get("code") != 200:
            return True, None
            
        play_command = sync_data.get("data", {}).get("playCommand", {})
        song_id = play_command.get("targetSongId")
        play_status = play_command.get("playStatus") # "PLAY" or "PAUSE"
        progress = play_command.get("progress", 0)
        
        return True, (song_id, play_status, progress)

    async def fetch_song_detail(self, song_id) -> dict:
        try:
            data = await self._api_get("/song/detail", {"ids": song_id})
            songs = data.get("songs", [])
            if not songs:
                return {}
            
            song = songs[0]
            result = {
                "id": song_id,
                "name": song.get("name", "Unknown"),
                "artists": [ar.get("name", "Unknown") for ar in song.get("ar", [])],
                "album": song.get("al", {}).get("name", "Unknown"),
                "albumPic": song.get("al", {}).get("picUrl", ""),
                "duration": song.get("dt", 0),
            }
            return result
        except Exception as e:
            logger.error(f"[Music] 获取歌曲详情失败: {e}")
            return {}

    async def fetch_lyric(self, song_id) -> str:
        try:
            data = await self._api_get("/lyric", {"id": song_id})
            lrc = data.get("lrc", {}).get("lyric", "")
            return lrc
        except Exception:
            return ""

    async def control_music(self, action: str):
        """盲测：尝试发送控制指令"""
        if not self.cookie or not self.current_room_id:
            logger.warning("[Music] 无法控制：未登录或未在房间内")
            return False
            
        try:
            command_type = action.upper()
            
            params = {
                "roomId": self.current_room_id,
                "commandType": command_type,
                "playStatus": command_type if command_type in ("PLAY", "PAUSE") else "PLAY",
                "progress": getattr(self, 'current_progress', 0)
            }
            if command_type in ("PLAY", "PAUSE"):
                params["targetSongId"] = self.current_song_id

            res = await self._api_get("/listentogether/play/command", params)
            logger.info(f"[Music] 控制播放 {action} 返回: {res}")
            
            return res.get("code") == 200
        except Exception as e:
            logger.error(f"[Music] 控制异常: {e}")
            return False

    async def poll(self):
        """执行一次轮询，检测一起听状态变化"""
        if not self.cookie:
            return
            
        try:
            in_room, current_info = await self.get_listen_together_status()
            
            if in_room is False:
                # 没在一起听，清空状态
                if self.current_song_id is not None:
                    logger.info("[Music] 退出一起听房间，清除状态")
                    self.current_song_id = None
                    if os.path.exists(STATUS_FILE):
                        os.remove(STATUS_FILE)
                return
                
            if not current_info:
                return
                
            song_id, play_status, progress = current_info
            self.current_progress = progress
            
            if play_status == "PLAY":
                self.total_together_seconds += POLL_INTERVAL
                self._save_duration()
            
            # 检查是否有变化（切歌或播放/暂停状态改变）
            if song_id != self.current_song_id or play_status != self.current_play_status:
                is_new_song = (song_id != self.current_song_id)
                self.current_song_id = song_id
                self.current_play_status = play_status
                self.last_update_time = time.time()
                
                status_zh = "播放中" if play_status == "PLAY" else "已暂停"
                logger.info(f"[Music] 状态更新: 歌曲ID={song_id}, 状态={status_zh}")
                
                # 如果是切歌，拉取详情
                if is_new_song and song_id:
                    logger.info(f"[Music] 检测到切歌: 拉取 {song_id} 详情...")
                    detail = await self.fetch_song_detail(song_id)
                    lyric = await self.fetch_lyric(song_id)
                    
                    # 写入文件供 injection.py 提取
                    output = {
                        "song_id": song_id,
                        "name": detail.get("name"),
                        "artists": ", ".join(detail.get("artists", [])),
                        "album": detail.get("album"),
                        "albumPic": detail.get("albumPic"),
                        "duration": detail.get("duration"),
                        "progress": progress,
                        "status": status_zh,
                        "lyric": lyric,
                        "update_time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    
                    with open(STATUS_FILE, "w", encoding="utf-8") as f:
                        json.dump(output, f, ensure_ascii=False, indent=2)
                    logger.info(f"[Music] 状态文件已更新: {detail.get('name')}")
                else:
                    # 只是播放/暂停状态改变，更新文件中的状态字段
                    if os.path.exists(STATUS_FILE):
                        with open(STATUS_FILE, "r", encoding="utf-8") as f:
                            output = json.load(f)
                        output["status"] = status_zh
                        output["progress"] = progress
                        output["update_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        with open(STATUS_FILE, "w", encoding="utf-8") as f:
                            json.dump(output, f, ensure_ascii=False, indent=2)
                        logger.info(f"[Music] 状态文件已更新(播放状态): {status_zh}")
        except Exception as e:
            logger.error(f"[Music] poll 异常: {e}")

    async def loop(self):
        self.is_running = True
        logger.info(f"[Music] 一起听实时监控启动 (间隔={POLL_INTERVAL}s)")
        while self.is_running:
            try:
                await self.poll()
            except Exception as e:
                logger.error(f"[Music] 轮询异常: {e}")
            await asyncio.sleep(POLL_INTERVAL)

_watcher = None

def get_watcher():
    global _watcher
    if _watcher is None:
        _watcher = ListenTogetherWatcher()
    return _watcher

async def start_music_watcher():
    watcher = get_watcher()
    asyncio.create_task(watcher.loop())

def get_music_status() -> str:
    """供 injection.py 调用的接口，格式化输出当前听歌状态"""
    if not os.path.exists(STATUS_FILE):
        return ""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        # 如果距离上次更新超过 30 分钟，可能是不准了，忽略
        # （注：一起听如果暂停很久可能不会有更新，但这里我们相信状态）
        
        name = data.get("name", "Unknown")
        artists = data.get("artists", "Unknown")
        album = data.get("album", "Unknown")
        status = data.get("status", "播放中")
        
        # 暂停时停止向大模型注入，省token
        if status == "已暂停":
            return ""
            
        lyric_full = data.get("lyric", "")
        
        # 截取歌词防止太长爆 token
        lyric_lines = [line for line in lyric_full.split('\n') if line.strip() and ']' in line]
        lyric_preview = "\n".join(lyric_lines[:20]) # 取前20行
        if len(lyric_lines) > 20:
            lyric_preview += "\n..."
            
        text = (
            f"【网易云 一起听】\n"
            f"状态: {status}\n"
            f"歌曲: {name}\n"
            f"歌手: {artists}\n"
            f"专辑: {album}\n"
            f"歌词预览:\n{lyric_preview}"
        )
        return text
    except Exception as e:
        logger.error(f"[Music] 读取状态失败: {e}")
        return ""

# ========== 以下是扫码登录功能 ==========

async def qr_login_step1():
    w = get_watcher()
    res = await w._api_get("/login/qr/key")
    if res and res.get("code") == 200:
        return res.get("data", {}).get("unikey")
    return None

async def qr_login_step2(key):
    w = get_watcher()
    res = await w._api_get("/login/qr/create", {"key": key, "qrimg": 1})
    if res and res.get("code") == 200:
        return res.get("data", {}).get("qrimg")
    return None

async def qr_login_step3(key):
    w = get_watcher()
    res = await w._api_get("/login/qr/check", {"key": key})
    if res:
        code = res.get("code")
        if code == 803:
            return code, res.get("cookie")
        return code, res.get("message", "未知状态")
    return 500, "API请求失败"
