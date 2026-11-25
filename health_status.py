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
            disk_data = {}
            if os.path.exists(HEALTH_STATUS_FILE):
                with open(HEALTH_STATUS_FILE, "r", encoding="utf-8") as f:
                    disk_data = json.load(f)

            # 仅保留 MacroDroid 推送的通知文本（不覆盖最新拉取的心率）
            if not self.data.get("last_notification") and disk_data.get("last_notification"):
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
                    rel_resp = await client._request("GET", "/app/v1/relatives/get_relative_list")
                    relatives = (rel_resp.get("result") or {}).get("relative_list") or []
                    if not relatives:
                        logger.error("[Health] 亲友列表为空！请确保已经添加主号为亲友并共享数据。")
                        return False
                    rel0 = relatives[0]
                    user_id = rel0["relative_uid"]

                    # 提取亲友列表中的最近异常心率时间
                    cst_tz = timezone(timedelta(hours=8))
                    for ab in (rel0.get("latest_abnormal_record_time") or []):
                        if isinstance(ab, dict) and ab.get("key") == "heart_rate" and ab.get("timestamp"):
                            try:
                                ab_dt = datetime.fromtimestamp(int(ab["timestamp"]), tz=cst_tz)
                                self.data["last_abnormal_hr_time"] = ab_dt.strftime("%m-%d %H:%M")
                            except Exception:
                                pass

                    latest_resp = await client._request(
                        "GET",
                        "/app/v1/relatives/get_latest_data",
                        params={"relative_uid": user_id},
                    )
                    data_list = (latest_resp.get("result") or {}).get("data_list") or []
                    has_new_data = False

                    for item in data_list:
                        key = item.get("key")
                        raw_val = item.get("value")
                        if not key or raw_val is None:
                            continue
                        try:
                            val = json.loads(raw_val) if isinstance(raw_val, str) else raw_val
                        except Exception:
                            continue
                        if not isinstance(val, dict):
                            continue

                        if key == "heart_rate" and val.get("bpm") is not None:
                            self.data["heart_rate"] = val["bpm"]
                            has_new_data = True
                        elif key == "spo2" and val.get("spo2") is not None:
                            self.data["spo2"] = val["spo2"]
                            has_new_data = True
                        elif key == "steps" and val.get("steps") is not None:
                            self.data["steps"] = val["steps"]
                            has_new_data = True
                        elif key == "calories" and val.get("calories") is not None:
                            self.data["calories"] = val["calories"]
                            has_new_data = True
                        elif key == "valid_stand" and val.get("count") is not None:
                            self.data["valid_stand"] = val["count"]
                            has_new_data = True
                        elif key == "sleep" and val.get("total_duration") is not None:
                            total_min = int(val.get("total_duration") or 0)
                            score = val.get("sleep_score")
                            sleep_info = dict(self.data.get("sleep") or {})
                            sleep_info["total"] = f"{total_min // 60}h{total_min % 60}m"
                            if score is not None:
                                sleep_info["score"] = score
                            self.data["sleep"] = sleep_info
                            has_new_data = True

                    # 拉取当日聚合明细（含深睡/浅睡/REM比例、入睡醒来时间、清醒次数、静息/最高心率、异常心搏数、血氧区间）
                    from datetime import date
                    from mi_fitness.client.data import _date_to_timestamps
                    day_start, day_end = _date_to_timestamps(date.today())
                    win_start = day_start - 86400  # 包含昨日以防凌晨/跨日报告延迟

                    for agg_key in ("heart_rate", "spo2", "sleep"):
                        try:
                            agg_resp = await client._request(
                                "GET",
                                "/app/v1/relatives/get_aggregated_data",
                                params={
                                    "relative_uid": user_id,
                                    "key": agg_key,
                                    "tag": "daily_report",
                                    "start_time": win_start,
                                    "end_time": day_end,
                                    "limit": 3,
                                },
                            )
                            agg_items = (agg_resp.get("result") or {}).get("data_list") or []
                            if not agg_items:
                                continue
                            latest_agg = agg_items[-1]
                            raw_v = latest_agg.get("value")
                            aval = json.loads(raw_v) if isinstance(raw_v, str) else raw_v
                            if not isinstance(aval, dict):
                                continue

                            if agg_key == "heart_rate":
                                hr_stats = {}
                                if aval.get("avg_rhr") is not None:
                                    hr_stats["rhr"] = aval["avg_rhr"]
                                if aval.get("avg_hr") is not None:
                                    hr_stats["avg"] = aval["avg_hr"]
                                if aval.get("min_hr") is not None:
                                    hr_stats["min"] = aval["min_hr"]
                                if aval.get("max_hr") is not None:
                                    hr_stats["max"] = aval["max_hr"]
                                if aval.get("abnormal_hr_count") is not None:
                                    hr_stats["abnormal_count"] = int(aval["abnormal_hr_count"])
                                if hr_stats:
                                    self.data["hr_stats"] = hr_stats
                                lhr = aval.get("latest_hr")
                                if isinstance(lhr, dict) and lhr.get("bpm") is not None:
                                    self.data["heart_rate"] = lhr["bpm"]
                                has_new_data = True

                            elif agg_key == "spo2":
                                spo2_stats = {}
                                if aval.get("min_spo2") is not None:
                                    spo2_stats["min"] = aval["min_spo2"]
                                if aval.get("max_spo2") is not None:
                                    spo2_stats["max"] = aval["max_spo2"]
                                if aval.get("lack_spo2_count") is not None:
                                    spo2_stats["lack_count"] = int(aval["lack_spo2_count"])
                                if spo2_stats:
                                    self.data["spo2_stats"] = spo2_stats
                                has_new_data = True

                            elif agg_key == "sleep":
                                total_min = int(aval.get("total_duration") or 0)
                                deep_min = int(aval.get("sleep_deep_duration") or 0)
                                light_min = int(aval.get("sleep_light_duration") or 0)
                                rem_min = int(aval.get("sleep_rem_duration") or 0)
                                awake_min = int(aval.get("sleep_awake_duration") or 0)
                                segs = aval.get("segment_details") or []
                                seg = segs[-1] if segs and isinstance(segs[-1], dict) else {}

                                sleep_info = {
                                    "total": f"{total_min // 60}h{total_min % 60}m",
                                    "score": aval.get("sleep_score"),
                                    "deep_min": deep_min,
                                    "light_min": light_min,
                                    "rem_min": rem_min,
                                    "awake_min": awake_min,
                                    "awake_count": seg.get("awake_count"),
                                }
                                if total_min > 0:
                                    sleep_info["deep_pct"] = round(deep_min * 100 / total_min)
                                    sleep_info["light_pct"] = round(light_min * 100 / total_min)
                                    sleep_info["rem_pct"] = max(0, 100 - sleep_info["deep_pct"] - sleep_info["light_pct"])
                                if seg.get("bedtime"):
                                    sleep_info["bedtime"] = datetime.fromtimestamp(int(seg["bedtime"]), tz=cst_tz).strftime("%H:%M")
                                if seg.get("wake_up_time"):
                                    sleep_info["wake_up_time"] = datetime.fromtimestamp(int(seg["wake_up_time"]), tz=cst_tz).strftime("%H:%M")
                                if aval.get("avg_hr") is not None:
                                    sleep_info["avg_hr"] = aval["avg_hr"]
                                if aval.get("avg_spo2") is not None:
                                    sleep_info["avg_spo2"] = aval["avg_spo2"]
                                self.data["sleep"] = sleep_info
                                has_new_data = True
                        except Exception as agg_e:
                            logger.warning(f"[Health] 拉取聚合 {agg_key} 失败: {agg_e}")

                    if has_new_data:
                        self.data["last_update"] = datetime.now().strftime("%H:%M")
                    self.data["status"] = "online"
                    logger.info(
                        f"[Health] 拉取成功: hr={self.data.get('heart_rate')}, "
                        f"spo2={self.data.get('spo2')}, steps={self.data.get('steps')}, "
                        f"cal={self.data.get('calories')}, sleep={self.data.get('sleep', {}).get('total')}"
                    )
                    return True
        except Exception as e:
            logger.error(f"[Health] 获取健康数据失败: {e}")
            return False

    async def _fetch_fast_data(self):
        logger.debug("[Health] 正在请求日常健康数据...")
        return await self._fetch_band_data(fetch_slow=True)

    async def _fetch_slow_data(self):
        logger.debug("[Health] 正在请求睡眠数据...")
        return await self._fetch_band_data(fetch_slow=True)

    async def loop(self):
        self.is_running = True
        logger.info("[Health] 小米健康数据轮询服务已启动")
        
        from config import get_config
        while self.is_running:
            try:
                master_switch = await get_config('gateway_master_switch', '1')
                health_switch = await get_config('feature_health', '1')
                if master_switch == '0' or health_switch == '0':
                    await asyncio.sleep(60)
                    continue
                    
                now = time.time()
                
                if not os.path.exists(TOKEN_FILE):
                    self.data["status"] = "auth_expired"
                    self._save_cache()
                    await asyncio.sleep(60)
                    continue

                # get_latest_data 一次返回心率/血氧/步数/卡路里/站立/睡眠，每 5 分钟拉一次
                if now - self.last_fast_poll >= POLL_INTERVAL_FAST:
                    await self._fetch_fast_data()
                    self.last_fast_poll = now
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
        has_any_metric = any(
            data.get(k) is not None for k in ("heart_rate", "steps", "spo2", "calories", "sleep")
        )
        if status == "offline" and not has_any_metric:
            return ""

        if status == "auth_expired":
            suffix = " (认证过期)"
        elif status == "not_worn":
            suffix = " (未佩戴)"
        elif status == "offline":
            suffix = " (离线)"
        else:
            suffix = ""
            
        lines = ["【健康数据 · 小米手环9 Pro】"]
        
        hr = data.get("heart_rate")
        hr_stats = data.get("hr_stats") or {}
        spo2 = data.get("spo2")
        spo2_stats = data.get("spo2_stats") or {}
        steps = data.get("steps")
        cals = data.get("calories")
        stand = data.get("valid_stand")

        hr_str = f"{hr}bpm" if hr is not None else "--bpm"
        hr_extras = []
        if hr_stats.get("rhr") is not None and hr_stats.get("max") is not None:
            hr_extras.append(f"静息{hr_stats['rhr']}/最高{hr_stats['max']}")
        if hr_stats.get("abnormal_count") is not None:
            ab_cnt = hr_stats["abnormal_count"]
            ab_part = f"异常心搏{ab_cnt}次"
            if ab_cnt > 0 and data.get("last_abnormal_hr_time"):
                ab_part += f"(最近{data['last_abnormal_hr_time']})"
            hr_extras.append(ab_part)
        if hr_extras:
            hr_str += f"({', '.join(hr_extras)})"

        spo2_str = f"{spo2}%" if spo2 is not None else "--%"
        if spo2_stats.get("min") is not None and spo2_stats.get("max") is not None:
            spo2_str += f"({spo2_stats['min']}~{spo2_stats['max']}%)"

        parts_line1 = [
            f"心率: {hr_str}",
            f"血氧: {spo2_str}",
            f"步数: {steps if steps is not None else '--'}",
            f"消耗: {cals if cals is not None else '--'}kcal",
        ]
        if stand is not None:
            parts_line1.append(f"站立: {stand}次")
        lines.append(" | ".join(parts_line1))
        
        sleep = data.get("sleep")
        updated = data.get("last_update") or "--"
        if isinstance(sleep, dict) and sleep.get("total"):
            time_span = ""
            if sleep.get("bedtime") and sleep.get("wake_up_time"):
                time_span = f"{sleep['bedtime']}~{sleep['wake_up_time']} "
            score_part = f"(评分{sleep['score']})" if sleep.get("score") is not None else ""
            sleep_parts = [f"睡眠: {time_span}共{sleep.get('total')}{score_part}"]

            if sleep.get("deep_pct") is not None:
                stage_str = f"深睡{sleep['deep_pct']}% 浅睡{sleep['light_pct']}% REM{sleep['rem_pct']}%"
                if sleep.get("awake_count") is not None:
                    aw_min = sleep.get("awake_min")
                    aw_min_str = f"({aw_min}m)" if aw_min else ""
                    stage_str += f" 清醒{sleep['awake_count']}次{aw_min_str}"
                sleep_parts.append(stage_str)
            elif sleep.get("deep") and sleep.get("deep") != "0h0m":
                sleep_parts.append(f"深睡{sleep.get('deep')}/浅睡{sleep.get('light')}/REM {sleep.get('rem')}")

            sleep_parts.append(f"更新: {updated}{suffix}")
            lines.append(" | ".join(sleep_parts))
        else:
            lines.append(f"更新: {updated}{suffix}")
        
        res = "\n".join(lines)
        logger.debug(f"[Health-Debug] 注入文本组装成功，长度: {len(res)}")
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
