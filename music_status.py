import os
import time
import json
import asyncio
import httpx
import logging

logger = logging.getLogger('music_status')
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

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
        
    async def fetch_listen_duration(self) -> int:
        """从网易云API获取真实听歌累计时长（秒）"""
        try:
            # 网易云API: /user/record?uid=xxx&type=0 (type=0为所有时间)
            # 返回的数据中包含听歌时长统计
            # 如果API不直接提供总时长，这里返回0，保持原有逻辑
            # TODO: 需要确认网易云API是否提供此接口
            return 0
        except Exception as e:
            logger.error(f"[Music] 获取听歌时长失败: {e}")
            return 0

    def _load_cookie(self):
        if os.path.exists(COOKIE_FILE):
            try:
                with open(COOKIE_FILE, "r") as f:
                    return f.read().strip()
            except Exception as e:
                logger.error(f"[Music] 读取 Cookie 失败: {e}")
        return None
    
    def _load_duration(self) -> int:
        """从文件加载累计听歌时长（秒）"""
        if os.path.exists(DURATION_FILE):
            try:
                with open(DURATION_FILE, "r") as f:
                    data = json.load(f)
                    return data.get("total_together_seconds", 0)
            except Exception as e:
                logger.error(f"[Music] 读取听歌时长失败: {e}")
        return 0
    
    def _save_duration(self):
        """保存累计听歌时长到文件"""
        try:
            with open(DURATION_FILE, "w") as f:
                json.dump({"total_together_seconds": self.total_together_seconds}, f)
        except Exception as e:
            logger.error(f"[Music] 保存听歌时长失败: {e}")
        
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
        server_seq = play_command.get("serverSeq", 0)  # 指令下发时的服务器时间戳(ms)
        
        # playCommand.progress 是指令下发瞬间的快照，不会实时更新！
        # 需要用 serverSeq 推算实时进度: estimated = progress + (now - serverSeq)
        return True, (song_id, play_status, progress, server_seq)

    async def fetch_song_detail(self, song_id) -> dict:
        try:
            data = await self._api_get("/song/detail", {"ids": song_id})
            songs = data.get("songs", [])
            if not songs:
                return {}
            
            song = songs[0]
            
            # 尝试提取风格标签（可能在多个地方）
            genres = []
            # 1. 从歌曲自身的tag字段
            if song.get("tags"):
                genres.extend(song.get("tags", []))
            # 2. 从专辑的subType字段（如"流行"、"摇滚"等）
            album_info = song.get("al", {})
            if album_info.get("subType"):
                genres.append(album_info.get("subType"))
            # 3. 网易云有些歌曲在song对象里有style字段
            if song.get("style"):
                genres.extend(song.get("style", []))
            
            result = {
                "id": song_id,
                "name": song.get("name", "Unknown"),
                "artists": [ar.get("name", "Unknown") for ar in song.get("ar", [])],
                "album": song.get("al", {}).get("name", "Unknown"),
                "albumPic": song.get("al", {}).get("picUrl", ""),
                "duration": song.get("dt", 0),
                "genres": genres[:3] if genres else [],  # 最多保留3个标签
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

    async def fetch_hot_comments(self, song_id) -> list:
        try:
            data = await self._api_get("/comment/music", {"id": song_id, "limit": 3})
            comments = []
            if data.get("hotComments") and len(data["hotComments"]) > 0:
                comments = [c.get("content", "") for c in data["hotComments"][:3]]
            elif data.get("comments") and len(data["comments"]) > 0:
                comments = [c.get("content", "") for c in data["comments"][:3]]
            return comments
        except Exception as e:
            logger.error(f"[Music] 获取评论失败: {e}")
            return []


    async def control_music(self, action: str, progress: int = None):
        """盲测：尝试发送控制指令"""
        if not self.cookie or not self.current_room_id:
            logger.warning("[Music] 无法控制：未登录或未在房间内")
            return False
            
        try:
            command_type = action.upper()
            
            # 使用传入的 progress，或者动态推算当前的真实进度
            current_prog = getattr(self, 'current_progress', 0)
            if progress is not None:
                current_prog = progress
            else:
                if getattr(self, 'current_play_status', '') == "PLAY" and hasattr(self, 'last_update_time'):
                    current_prog += int((time.time() - self.last_update_time) * 1000)
                
            params = {
                "roomId": self.current_room_id,
                "commandType": command_type,
                "playStatus": "PLAY" if command_type == "SEEK" else (command_type if command_type in ("PLAY", "PAUSE") else "PLAY"),
                "progress": current_prog
            }
            if command_type in ("PLAY", "PAUSE", "SEEK"):
                params["targetSongId"] = self.current_song_id

            res = await self._api_get("/listentogether/play/command", params)
            logger.info(f"[Music] 控制播放 {action} 返回: {res}")
            
            if res.get("code") == 200:
                # 乐观更新本地状态，防止前端立刻轮询时读到旧数据导致跳回
                self.last_update_time = time.time()
                if command_type == "SEEK" and progress is not None:
                    self.current_progress = progress
                    self.last_recorded_progress = progress
                elif command_type in ("PLAY", "PAUSE"):
                    self.current_play_status = command_type
                    self.last_recorded_status = command_type
                
                if os.path.exists(STATUS_FILE):
                    try:
                        with open(STATUS_FILE, "r", encoding="utf-8") as f:
                            output = json.load(f)
                        if command_type == "SEEK" and progress is not None:
                            output["progress"] = progress
                        elif command_type in ("PLAY", "PAUSE"):
                            output["status"] = "播放中" if command_type == "PLAY" else "已暂停"
                        with open(STATUS_FILE, "w", encoding="utf-8") as f:
                            json.dump(output, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        logger.error(f"[Music] 乐观更新状态文件失败: {e}")

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
                if self.current_song_id is not None or os.path.exists(STATUS_FILE):
                    logger.info("[Music] 退出一起听房间，清除状态")
                    self.current_song_id = None
                    self.current_play_status = None
                    self.current_progress = 0
                    self.stagnant_polls = 0
                    if os.path.exists(STATUS_FILE):
                        try:
                            os.remove(STATUS_FILE)
                        except Exception:
                            pass
                return
                
            if not current_info:
                return
                
            song_id, raw_play_status, cmd_progress, server_seq = current_info
            if not song_id:
                return
            
            # 用 serverSeq 推算实时进度（playCommand.progress 只是指令下发时的快照）
            now_ms = int(time.time() * 1000)
            if raw_play_status == "PLAY" and server_seq > 0:
                progress = cmd_progress + (now_ms - server_seq)
            else:
                progress = cmd_progress

            now_ts = time.time()
            last_poll_time = getattr(self, 'last_poll_time', now_ts)
            time_elapsed_ms = int((now_ts - last_poll_time) * 1000)
            last_progress = getattr(self, 'last_recorded_progress', progress)
            last_status = getattr(self, 'last_recorded_status', raw_play_status)
            is_new_song = (song_id != self.current_song_id)

            delta_progress = progress - last_progress

            # 检测进度是否停滞（仅用于时长累计保护，不再影响播放状态判定）
            # 使用估算进度后，正常播放时 delta_progress 会正确反映时间流逝
            is_stagnant = (time_elapsed_ms >= 5000 and abs(delta_progress) < 1500)

            if is_new_song:
                self.stagnant_polls = 0
            elif is_stagnant:
                self.stagnant_polls = getattr(self, 'stagnant_polls', 0) + 1
            else:
                self.stagnant_polls = 0

            # 直接信任 API 的 playStatus：
            # playCommand.progress 是指令快照而非实时值，用它做停滞检测会误判。
            # 已通过 serverSeq 推算出实时进度，无需再用停滞次数覆盖播放状态。
            effective_play_status = raw_play_status

            # 判断是否发生进度跳变（Seek）：
            # 只有在非停滞且进度真正改变时，偏离预期播放时间超过8秒才算 Seek
            if not is_new_song and not is_stagnant and abs(delta_progress) >= 1500:
                if last_status == "PLAY":
                    is_seek = abs(delta_progress - time_elapsed_ms) > 8000
                else:
                    is_seek = True
            else:
                is_seek = False

            # 累加一起听时长：只有在真实播放中且进度正常推进时才累加（房间固定2人，时长×2）
            if effective_play_status == "PLAY" and not is_stagnant and time_elapsed_ms > 0:
                actual_elapsed_sec = min(int(time_elapsed_ms / 1000), POLL_INTERVAL * 2)
                if actual_elapsed_sec > 0:
                    self.total_together_seconds += actual_elapsed_sec * 2
                    self._save_duration()

            self.last_poll_time = now_ts
            self.last_recorded_progress = progress
            self.last_recorded_status = raw_play_status
            self.current_progress = progress

            # 检查是否有状态改变（切歌、有效播放/暂停状态改变、或进度跳变）
            state_changed = (is_new_song or effective_play_status != self.current_play_status or is_seek)

            if state_changed:
                self.current_song_id = song_id
                self.current_play_status = effective_play_status
                self.last_update_time = now_ts

                status_zh = "播放中" if effective_play_status == "PLAY" else "已暂停"
                logger.info(f"[Music] 状态更新: 歌曲ID={song_id}, 状态={status_zh}, 进度={progress} (raw={raw_play_status}, seek={is_seek}, stagnant={self.stagnant_polls})")

                # 如果是切歌，拉取详情
                if is_new_song:
                    logger.info(f"[Music] 检测到切歌: 拉取 {song_id} 详情...")
                    detail = await self.fetch_song_detail(song_id)
                    lyric = await self.fetch_lyric(song_id)
                    hot_comments = await self.fetch_hot_comments(song_id)

                    # 写入文件供 injection.py 提取
                    output = {
                        "song_id": song_id,
                        "name": detail.get("name"),
                        "artists": ", ".join(detail.get("artists", [])),
                        "album": detail.get("album"),
                        "albumPic": detail.get("albumPic"),
                        "duration": detail.get("duration"),
                        "genres": detail.get("genres", []),
                        "progress": progress,
                        "status": status_zh,
                        "lyric": lyric,
                        "hot_comments": hot_comments,
                        "update_time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

                    with open(STATUS_FILE, "w", encoding="utf-8") as f:
                        json.dump(output, f, ensure_ascii=False, indent=2)
                    logger.info(f"[Music] 状态文件已更新: {detail.get('name')}")
                else:
                    # 只是播放/暂停状态改变或进度跳变，更新文件中的状态字段
                    if os.path.exists(STATUS_FILE):
                        with open(STATUS_FILE, "r", encoding="utf-8") as f:
                            output = json.load(f)
                        output["status"] = status_zh
                        output["progress"] = progress
                        output["update_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        with open(STATUS_FILE, "w", encoding="utf-8") as f:
                            json.dump(output, f, ensure_ascii=False, indent=2)
                        logger.info(f"[Music] 状态文件已更新(播放状态/进度): {status_zh}")
        except Exception as e:
            logger.error(f"[Music] poll 异常: {e}")

    def get_current_song_info(self):
        """为API提供实时估算的歌曲状态"""
        if not self.current_song_id or not os.path.exists(STATUS_FILE):
            return None
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                output = json.load(f)
            
            prog = output.get("progress", 0)
            if output.get("status") == "播放中" and hasattr(self, 'last_update_time'):
                prog += int((time.time() - self.last_update_time) * 1000)
            
            output["progress"] = prog
            return output
        except Exception:
            return None

    async def loop(self):
        self.is_running = True
        logger.info(f"[Music] 一起听实时监控启动 (间隔={POLL_INTERVAL}s)")
        from config import get_config
        while self.is_running:
            try:
                music_switch = await get_config('feature_music', '1')
                master_switch = await get_config('gateway_master_switch', '1')
                if master_switch == '1' and music_switch == '1':
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
    
    # 不再显示累计时长，因为需要从网易云API实时获取
    # 如果需要显示时长，应该异步调用 fetch_listen_duration()
    together_str = ""  # 暂时留空，等实现API获取后再填充

    if not os.path.exists(STATUS_FILE):
        return f"【网易云 一起听】\n状态: 离线\n{together_str}，现在不在听歌。"

    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        name = data.get("name", "Unknown")
        artists = data.get("artists", "Unknown")
        album = data.get("album", "Unknown")
        status = data.get("status", "播放中")
        
        if status == "已暂停":
            return f"【网易云 一起听】\n状态: 已暂停\n{together_str}，现在不在听歌。"
            
        lyric_full = data.get("lyric", "")
        
        lyric_lines = [line for line in lyric_full.split('\n') if line.strip() and ']' in line]
        lyric_preview = "\n".join(lyric_lines[:20]) # 取前20行
        if len(lyric_lines) > 20:
            lyric_preview += "\n..."
            
        text = (
            f"【网易云 ��起听】\n"
            f"状态: {status}\n"
            f"{together_str}\n"
            f"正在播放: {name}\n"
            f"歌手: {artists}\n"
            f"���辑: {album}\n"
            f"歌词预览:\n{lyric_preview}"
        )
        
        hot_comments = data.get("hot_comments", [])
        if hot_comments:
            comments_text = "\n".join([f"- {c}" for c in hot_comments])
            text += f"\n【网易云热评 (用于感受歌���氛围和情感)】\n{comments_text}"
            
        return text
    except Exception as e:
        logger.error(f"[Music] 读取状态失败: {e}")
        return f"【网易云 一起听】\n{together_str}，现在不在听歌。"

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