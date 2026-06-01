import os
import time
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from mi_fitness.auth import XiaomiAuth
from mi_fitness.client import MiHealthClient
from mi_fitness.client import data as _data

logger = logging.getLogger('health_status')

HEALTH_STATUS_FILE = "/home/ubuntu/caeron-gateway/health_status.json"
TOKEN_FILE = "/home/ubuntu/caeron-gateway/xiaomi_token.json"
POLL_INTERVAL_FAST = 5 * 60   # 5分钟轮询一次（心率/步数/血氧）
POLL_INTERVAL_SLOW = 30 * 60  # 30分钟轮询一次（睡眠）

class XiaomiHealthWatcher:
    def __init__(self):
        self.is_running = False
        self.last_fast_poll = 0
        self.last_slow_poll = 0
        self.data = {
            "heart_rate": None,
            "steps": None,
            "spo2": None,
            "calories": None,
            "sleep": None,
            "last_update": None,
            "status": "offline",
            "last_notification": None # Preserve MacroDroid notification text
        }
        self._load_cache()

    def _load_cache(self):
        if os.path.exists(HEALTH_STATUS_FILE):
            try:
                with open(HEALTH_STATUS_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                    # Update local memory
                    for k, v in cached.items():
                        if v is not None:
                            self.data[k] = v
            except Exception as e:
                logger.error(f"[Health] 读取缓存失败: {e}")

    def _save_cache(self):
        try:
            # First, reload from disk to not overwrite MacroDroid's real-time heart rate!
            disk_data = {}
            if os.path.exists(HEALTH_STATUS_FILE):
                with open(HEALTH_STATUS_FILE, "r", encoding="utf-8") as f:
                    disk_data = json.load(f)
            
            # Keep MacroDroid's heart_rate and last_notification if they are newer
            if "heart_rate" in disk_data and disk_data["heart_rate"]:
                self.data["heart_rate"] = disk_data["heart_rate"]
            if "last_notification" in disk_data:
                self.data["last_notification"] = disk_data["last_notification"]
                
            with open(HEALTH_STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Health] 保存缓存失败: {e}")

    async def _fetch_band_data(self, fetch_slow=False):
        if not os.path.exists(TOKEN_FILE):
            self.data["status"] = "auth_expired"
            return False
            
        try:
            async with XiaomiAuth.from_token(TOKEN_FILE) as auth:
                async with MiHealthClient(auth, base_url="https://hlth.io.mi.com") as client:
                    relatives = await client.get_relatives()
                    if not relatives:
                        logger.error("[Health] 亲友列表为空！请确保已经添加主号为亲友并共享数据。")
                        return False
                    user_id = relatives[0].relative_uid
                    
                    from datetime import date
                    from mi_fitness.client.data import _date_to_timestamps
                    start_time, end_time = _date_to_timestamps(date.today())
                    
                    # Fetch daily summary
                    steps_resp = await client.get_aggregated_data(user_id, "steps", start_time, end_time, limit=2)
                    cals_resp = await client.get_aggregated_data(user_id, "calories", start_time, end_time, limit=2)
                    hr_resp = await client.get_aggregated_data(user_id, "heart_rate", start_time, end_time, limit=2)
                    spo2_resp = await client.get_aggregated_data(user_id, "spo2", start_time, end_time, limit=2)
                    
                    if steps_resp.data_items:
                        latest_steps = steps_resp.data_items[-1].value
                        if isinstance(latest_steps, str):
                            latest_steps = json.loads(latest_steps)
                        if isinstance(latest_steps, dict) and 'steps' in latest_steps:
                            self.data['steps'] = latest_steps['steps']
                            
                    if cals_resp.data_items:
                        latest_cals = cals_resp.data_items[-1].value
                        if isinstance(latest_cals, str):
                            latest_cals = json.loads(latest_cals)
                        if isinstance(latest_cals, dict) and 'calories' in latest_cals:
                            self.data['calories'] = latest_cals['calories']
                            
                    if hr_resp.data_items:
                        latest_hr = hr_resp.data_items[-1].value
                        if isinstance(latest_hr, str):
                            latest_hr = json.loads(latest_hr)
                        if isinstance(latest_hr, dict) and 'heart_rate' in latest_hr:
                            # Try to get the average or latest HR from the aggregated data
                            # Depending on API response, it might be 'heart_rate' or 'avg_hr'
                            self.data['heart_rate'] = latest_hr.get('heart_rate', latest_hr.get('avg_hr', self.data['heart_rate']))
                            
                    if spo2_resp.data_items:
                        latest_spo2 = spo2_resp.data_items[-1].value
                        if isinstance(latest_spo2, str):
                            latest_spo2 = json.loads(latest_spo2)
                        if isinstance(latest_spo2, dict):
                            self.data['spo2'] = latest_spo2.get('spo2', latest_spo2.get('avg_spo2', self.data['spo2']))
                            
                    if fetch_slow:
                        sleep_resp = await client.get_aggregated_data(user_id, "sleep", start_time, end_time, limit=2)
                        if sleep_resp.data_items:
                            latest_sleep = sleep_resp.data_items[-1].value
                            if isinstance(latest_sleep, str):
                                latest_sleep = json.loads(latest_sleep)
                            if isinstance(latest_sleep, dict):
                                dp_min = latest_sleep.get('deepSleepTime', 0)
                                lt_min = latest_sleep.get('shallowSleepTime', 0)
                                rem_min = latest_sleep.get('remSleepTime', 0)
                                total_min = dp_min + lt_min + rem_min
                                
                                def minutes_as_time(minutes):
                                    return f"{minutes//60}h{minutes%60}m"
                                    
                                self.data['sleep'] = {
                                    "total": minutes_as_time(total_min),
                                    "deep": minutes_as_time(dp_min),
                                    "light": minutes_as_time(lt_min),
                                    "rem": minutes_as_time(rem_min)
                                }
                                
                    self.data['last_update'] = datetime.now().strftime("%H:%M")
                    self.data['status'] = "online"
                    return True
        except Exception as e:
            logger.error(f"[Health] 获取健康数据失败: {e}")
            return False

    async def _fetch_fast_data(self):
        logger.debug("[Health] 正在请求日常健康数据...")
        return await self._fetch_band_data(fetch_slow=False)

    async def _fetch_slow_data(self):
        logger.debug("[Health] 正在请求睡眠数据...")
        return await self._fetch_band_data(fetch_slow=True)

    async def loop(self):
        self.is_running = True
        logger.info("[Health] 小米健康数据轮询服务已启动")
        
        while self.is_running:
            try:
                now = time.time()
                
                if not os.path.exists(TOKEN_FILE):
                    self.data["status"] = "auth_expired"
                    self._save_cache()
                    await asyncio.sleep(60)
                    continue

                updated = False
                # 快轮询 (日常数据)
                if now - self.last_fast_poll >= POLL_INTERVAL_FAST:
                    if await self._fetch_fast_data():
                        self.last_fast_poll = now
                        updated = True
                    else:
                        updated = True
                
                # 慢轮询 (睡眠数据)
                if now - self.last_slow_poll >= POLL_INTERVAL_SLOW:
                    if await self._fetch_slow_data():
                        self.last_slow_poll = now
                        updated = True
                        
                if updated:
                    self._save_cache()
                
            except Exception as e:
                logger.error(f"[Health] 轮询异常: {e}")
                
            await asyncio.sleep(60) # 每分钟检查一次是否需要执行轮询

    def stop(self):
        self.is_running = False

def get_health_status() -> str:
    """返回用于注入的文本"""
    if not os.path.exists(HEALTH_STATUS_FILE):
        logger.info("[Health-Debug] health_status.json 文件不存在！")
        return ""
        
    try:
        with open(HEALTH_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        status = data.get("status", "offline")
        if status == "auth_expired":
            suffix = " (认证过期)"
        elif status == "not_worn":
            suffix = " (未佩戴)"
        elif status == "offline":
            suffix = " (离线)"
        else:
            suffix = ""
            
        lines = ["【健康数据 · 小米手环9 Pro】"]
        
        # 心率, 步数, 血氧
        hr = data.get("heart_rate") or "--"
        steps = data.get("steps") or "--"
        spo2 = data.get("spo2") or "--"
        lines.append(f"心率: {hr} bpm | 步数: {steps} | 血氧: {spo2}%")
        
        # 睡眠
        sleep = data.get("sleep")
        if sleep:
            lines.append(f"睡眠: {sleep.get('total', '--')} (深睡{sleep.get('deep', '--')} / 浅睡{sleep.get('light', '--')} / REM {sleep.get('rem', '--')})")
            
        # 消耗
        cals = data.get("calories") or "--"
        lines.append(f"活动消耗: {cals} kcal")
        
        # 更新时间
        updated = data.get("last_update") or "--"
        lines.append(f"更新: {updated}{suffix}")
        
        res = "\n".join(lines)
        logger.info(f"[Health-Debug] 注入文本组装成功，长度: {len(res)}")
        return res
    except Exception as e:
        logger.error(f"[Health] 组装状态注入文本失败: {e}")
        return ""

_watcher = None

def get_watcher() -> XiaomiHealthWatcher:
    global _watcher
    if _watcher is None:
        _watcher = XiaomiHealthWatcher()
    return _watcher

async def start_health_watcher():
    watcher = get_watcher()
    if not watcher.is_running:
        asyncio.create_task(watcher.loop())
