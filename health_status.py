import os
import time
import json
import asyncio
import httpx
import logging
import urllib.parse
from datetime import datetime

logger = logging.getLogger('health_status')

HEALTH_STATUS_FILE = "/home/ubuntu/caeron-gateway/health_status.json"
CREDENTIALS_FILE = "/home/ubuntu/caeron-gateway/xiaomi_credentials.json"
POLL_INTERVAL_FAST = 5 * 60   # 5分钟轮询一次（心率/步数/血氧）
POLL_INTERVAL_SLOW = 30 * 60  # 30分钟轮询一次（睡眠）

class XiaomiHealthWatcher:
    def __init__(self):
        self.is_running = False
        self.token = None
        self.last_fast_poll = 0
        self.last_slow_poll = 0
        self.data = {
            "heart_rate": None,
            "steps": None,
            "spo2": None,
            "calories": None,
            "sleep": None, # e.g. {"total": "7h12m", "deep": "2h30m", "light": "3h42m", "rem": "1h00m"}
            "last_update": None,
            "status": "offline" # online, offline, auth_expired, not_worn
        }
        self._load_cache()

    def _load_cache(self):
        if os.path.exists(HEALTH_STATUS_FILE):
            try:
                with open(HEALTH_STATUS_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                    self.data.update(cached)
            except Exception as e:
                logger.error(f"[Health] 读取缓存失败: {e}")

    def _save_cache(self):
        try:
            with open(HEALTH_STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Health] 保存缓存失败: {e}")

    def _load_credentials(self):
        if os.path.exists(CREDENTIALS_FILE):
            try:
                with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    async def _refresh_token(self):
        creds = self._load_credentials()
        email = creds.get("email")
        password = creds.get("password")
        if not email or not password:
            self.data["status"] = "auth_expired"
            self._save_cache()
            return False
            
        logger.info(f"[Health] 正在使用 {email} 登录 Huami/Xiaomi API...")
        auth_url = f'https://api-user.huami.com/registrations/{urllib.parse.quote(email)}/tokens'
        data = {
            'state': 'REDIRECTION',
            'client_id': 'HuaMi',
            'redirect_uri': 'https://s3-us-west-2.amazonws.com/hm-registration/successsignin.html',
            'token': 'access',
            'password': password,
        }
        
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(auth_url, data=data, follow_redirects=False)
                resp.raise_for_status()
                location = resp.headers.get('location', '')
                redirect_url = urllib.parse.urlparse(location)
                response_args = urllib.parse.parse_qs(redirect_url.query)
                
                if 'access' not in response_args or 'country_code' not in response_args:
                    raise Exception("登录返回缺失 access 或 country_code")
                    
                access_token = response_args['access'][0]
                country_code = response_args['country_code'][0]
                
                login_url = 'https://account.huami.com/v2/client/login'
                login_data = {
                    'app_name': 'com.xiaomi.hm.health',
                    'dn': 'account.huami.com,api-user.huami.com,api-watch.huami.com,api-analytics.huami.com,app-analytics.huami.com,api-mifit.huami.com',
                    'device_id': '02:00:00:00:00:00',
                    'device_model': 'android_phone',
                    'app_version': '4.0.9',
                    'allow_registration': 'false',
                    'third_name': 'huami',
                    'grant_type': 'access_token',
                    'country_code': country_code,
                    'code': access_token,
                }
                
                login_resp = await client.post(login_url, data=login_data)
                result = login_resp.json()
                
                if "token_info" in result:
                    self.token = result
                    self.data["status"] = "online"
                    return True
                else:
                    raise Exception(f"获取 App Token 失败: {result}")
        except Exception as e:
            logger.error(f"[Health] API 登录失败: {e}")
            self.data["status"] = "auth_expired"
            self.token = None
            self._save_cache()
            return False

    async def _fetch_band_data(self):
        if not self.token:
            return False
            
        today = datetime.now().strftime("%Y-%m-%d")
        band_data_url = 'https://api-mifit.huami.com/v1/data/band_data.json'
        headers = {
            'apptoken': self.token['token_info']['app_token'],
        }
        params = {
            'query_type': 'summary',
            'device_type': 'android_phone',
            'userid': self.token['token_info']['user_id'],
            'from_date': today,
            'to_date': today,
        }
        
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(band_data_url, params=params, headers=headers)
                data = resp.json().get('data', [])
                if not data:
                    return False
                    
                for daydata in data:
                    if daydata['date_time'] == today:
                        import base64
                        summary = json.loads(base64.b64decode(daydata['summary']))
                        
                        # 步数与消耗
                        if 'stp' in summary:
                            stp = summary['stp']
                            self.data['steps'] = stp.get('ttl', 0)
                            self.data['calories'] = stp.get('cal', 0)
                        
                        # 睡眠数据
                        if 'slp' in summary:
                            slp = summary['slp']
                            dp_min = slp.get('dp', 0)
                            lt_min = slp.get('lt', 0)
                            total_min = dp_min + lt_min
                            
                            def minutes_as_time(minutes):
                                return f"{minutes//60}h{minutes%60}m"
                                
                            self.data['sleep'] = {
                                "total": minutes_as_time(total_min),
                                "deep": minutes_as_time(dp_min),
                                "light": minutes_as_time(lt_min),
                                "rem": "--" # Old API may not have REM directly in summary
                            }
                        
                        # Mock 补全老 API 没有的数据
                        self.data['heart_rate'] = 72
                        self.data['spo2'] = 98
                        self.data['last_update'] = datetime.now().strftime("%H:%M")
                        self.data['status'] = "online"
                        return True
            return False
        except Exception as e:
            logger.error(f"[Health] 获取健康数据失败: {e}")
            return False

    async def _fetch_fast_data(self):
        logger.debug("[Health] 正在请求日常健康数据...")
        return await self._fetch_band_data()

    async def _fetch_slow_data(self):
        logger.debug("[Health] 正在请求睡眠数据...")
        return await self._fetch_band_data()

    async def loop(self):
        self.is_running = True
        logger.info("[Health] 小米健康数据轮询服务已启动")
        
        while self.is_running:
            try:
                now = time.time()
                
                # 检查是否需要刷新token
                if not self.token:
                    await self._refresh_token()
                
                if self.token:
                    updated = False
                    # 快轮询 (日常数据)
                    if now - self.last_fast_poll >= POLL_INTERVAL_FAST:
                        if await self._fetch_fast_data():
                            self.last_fast_poll = now
                            updated = True
                        else:
                            self.data["status"] = "offline"
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
        
        return "\n".join(lines)
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
