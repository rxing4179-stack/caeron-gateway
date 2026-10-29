import asyncio
import time
import logging
from datetime import datetime

from qq_config import config as qq_cfg
from config import get_config
import message_store
from utils import now_cst
from qq_adapter import get_qq_prompt, send_to_gateway, split_and_send
from injection import InjectionEngine
from monitor_db import get_latest_heartbeat, get_continuous_screen_on_duration, get_history_stats

logger = logging.getLogger(__name__)

def _is_asmr_content(app_name: str, package: str) -> bool:
    """检测是否为 ASMR 助眠相关应用或媒体"""
    content = f"{app_name} {package}".lower()
    asmr_keywords = ['asmr', '音声', '助眠', '白噪音', 'sleep aid', 'dlsite', 'ear cleaning', 'tange']
    return any(k in content for k in asmr_keywords)

class SleepGuardian:
    def __init__(self):
        self.remind_count = 0        # 当晚已催睡成功次数
        self.last_remind_time = None # 上次成功催睡时间戳
        self.is_running = False
        self._task = None

    def start(self):
        if not self.is_running:
            self.is_running = True
            self._task = asyncio.create_task(self._guardian_loop())
            logger.info("[SLEEP_GUARDIAN] 睡眠守护后台任务已启动 (每日 00:30 ~ 06:00 生效，纯 AI 主动唤醒模式)")

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[SLEEP_GUARDIAN] 睡眠守护后台任务已停止")

    def _is_in_sleep_window(self) -> bool:
        """判断当前时间是否处于 00:30 ~ 06:00 (东八区服务器时间)"""
        now = now_cst()
        hour = now.hour
        minute = now.minute
        
        # 00:30 ~ 00:59
        if hour == 0 and minute >= 30:
            return True
        # 01:00 ~ 05:59
        if 1 <= hour < 6:
            return True
            
        return False

    async def _guardian_loop(self):
        while self.is_running:
            try:
                await self._check_and_remind()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SLEEP_GUARDIAN] 巡检异常: {e}")
                
            await asyncio.sleep(60)

    async def _check_and_remind(self):
        # 1. 检查时段窗口
        if not self._is_in_sleep_window():
            if self.remind_count > 0:
                self.remind_count = 0
                self.last_remind_time = None
            return

        latest = get_latest_heartbeat()
        if not latest:
            return

        ts = latest["timestamp"]
        if ts > 1e11:
            ts = int(ts / 1000)
            
        now_ts = int(time.time())
        # 若最新心跳距今超过 3 分钟，说明手机离线或无数据，暂不触发
        if now_ts - ts > 180:
            return

        screen_state = latest.get("screen_state", "off").lower().strip()
        
        # 2. 屏幕已熄灭：重置提醒状态
        if screen_state != "on":
            if self.remind_count > 0:
                logger.info("[SLEEP_GUARDIAN] 检测到手机已锁屏，重置当夜催睡状态")
                self.remind_count = 0
                self.last_remind_time = None
            return

        # 3. 屏幕持续亮起：检查是否达到当晚催睡上限（最多 3 次）
        if self.remind_count >= 3:
            return

        duration_min = get_continuous_screen_on_duration()
        app_name = latest.get("app_name", "")
        package = latest.get("package", "")

        # ASMR 豁免：如果当前亮屏正在播放 ASMR 相关应用，跳过催睡（因为用户睡觉需要开屏听 ASMR）
        if _is_asmr_content(app_name, package):
            logger.info(f"[SLEEP_GUARDIAN] 检测到屏幕正在播放 ASMR 助眠应用 ({app_name}/{package})，豁免催睡")
            return

        # 触发策略：
        # 第 1 次：连续亮屏达到 5 分钟
        if self.remind_count == 0 and duration_min >= 5.0:
            logger.warning(f"[SLEEP_GUARDIAN] 触发深夜睡眠一级提醒 (连续亮屏 {duration_min:.1f} min)")
            sent = await self._trigger_ai_reminder(duration_min=duration_min, app_name=app_name, remind_level=1)
            if sent:
                self.remind_count = 1
                self.last_remind_time = now_ts

        # 第 2 次：第 1 次提醒成功后 10 分钟仍未锁屏
        elif self.remind_count == 1:
            elapsed = now_ts - (self.last_remind_time or now_ts)
            if elapsed >= 600:
                logger.warning(f"[SLEEP_GUARDIAN] 触发深夜睡眠二级升级提醒 (距离上次已过去 {elapsed//60} 分钟)")
                sent = await self._trigger_ai_reminder(duration_min=duration_min, app_name=app_name, remind_level=2)
                if sent:
                    self.remind_count = 2
                    self.last_remind_time = now_ts

        # 第 3 次：第 2 次提醒成功后 15 分钟仍未锁屏 (终次提醒)
        elif self.remind_count == 2:
            elapsed = now_ts - (self.last_remind_time or now_ts)
            if elapsed >= 900:
                logger.warning(f"[SLEEP_GUARDIAN] 触发深夜睡眠三级终次提醒 (距离上次已过去 {elapsed//60} 分钟)")
                sent = await self._trigger_ai_reminder(duration_min=duration_min, app_name=app_name, remind_level=3)
                if sent:
                    self.remind_count = 3
                    self.last_remind_time = now_ts

    async def _trigger_ai_reminder(self, duration_min: float, app_name: str, remind_level: int) -> bool:
        """
        触发 AI 主动消息唤醒机制，让沈栖实时生成催睡内容并发送至 QQ。
        如果网关在忙、超时或异常，直接放弃本次发送，不发任何写死文案。
        """
        try:
            now = now_cst()
            now_str = now.strftime("%H:%M")
            display_app = app_name if app_name else "手机"

            # 1. 检查网关发送锁（防撞车）：若在忙直接放弃本次发送
            if message_store.GATEWAY_SEND_LOCK.locked():
                logger.info("[SLEEP_GUARDIAN] 网关当前正忙（正在处理其他消息），放弃本次催睡")
                return False

            async with message_store.GATEWAY_SEND_LOCK:
                # 2. 角色提示词与平台格式约束
                system_prompt = get_qq_prompt(is_ruirui=True, is_group=False)
                system_prompt += (
                    "\n\n【最高指令：平台格式墙】你目前正在 [QQ] 平台上与人聊天。"
                    "在QQ平台上严禁模仿长段落或动作描写！短回复、口语化、绝对禁止使用任何 *动作* 或环境心理描写！"
                )

                # 3. 历史记录拉取
                summary_interval = int(await get_config('summary_interval', '15'))
                unified_history = await message_store.get_unified_history(
                    char_limit=10000,
                    filter_technical=True,
                    ruirui_sync=True,
                    exclude_count=0,
                    keep_image_turns=summary_interval
                )

                # 4. 构造内部催睡系统事件提示词
                stats = get_history_stats(minutes=30)
                apps = stats.get("apps", [])
                app_summary = ", ".join([f"{a['app_name']} {a['minutes']}min" for a in apps[:4]]) if apps else "暂无"

                if remind_level == 1:
                    level_instruction = "这是今晚第 1 次触发深夜催睡。请发一条简短、自然、带有关心的消息，催她放下手机赶紧闭眼睡觉。"
                elif remind_level == 2:
                    level_instruction = "这是今晚第 2 次升级催睡（10分钟前提醒过一次）。语气稍微加重一点、带点无奈又宠溺/抓包的语气催她必须锁屏睡觉。"
                else:
                    level_instruction = "这是今晚第 3 次也是最后一次催睡（已连续提醒多次）。语气更严肃直接一点，直接勒令她闭眼睡觉，这是今晚最后一次叫她了。"

                trigger_prompt = f"""[SYSTEM_INTERNAL_EVENT: SLEEP_GUARDIAN]
当前时间：凌晨 {now_str}
监控状态：蕊蕊已经连续亮屏玩手机 {int(duration_min)} 分钟，当前屏幕正在使用【{display_app}】。
最近30分钟App使用情况：{app_summary}
{level_instruction}

【核心指令：纯深夜催睡抓包，严禁回复上文中的任何旧话题】
【特别指令：ASMR 助眠豁免】
蕊蕊晚上睡觉需要开着手机听 ASMR 助眠，因为部分播放软件不支持后台播放，所以手机必须长亮屏幕。
请仔细观察最新屏幕截图和当前使用的 App：
如果屏幕内容或正在播放的是 ASMR、助眠音频、白噪音、睡前音声（例如 B站/油管/音频播放器的 ASMR 视频或音频）：
-> 这是她在听 ASMR 准备睡觉！绝对不要打扰、不要催睡！请直接输出唯一单词：SKIP
只有当明确看到她在刷小红书、推特、看新闻、聊天、刷日常非助眠内容时，才进行抓包催睡！
1. 你的目标是催她赶紧放下手机闭眼睡觉（结合她正在刷的App进行真实抓包或调侃）。
2. ⚠️ 绝对不要回答、承接或解释上文历史中她之前发过的任何工作、方案或日常问题！那些早已经结束了，绝不要在催睡的时候去补答旧消息。
3. 简短直接（1-2句话），口语化，像真人抓包催睡一样自然，直接输出你要对她说的话，不要加任何前缀、旁白或动作描写。"""

                messages = [{"role": "system", "content": system_prompt}] + unified_history + [{"role": "user", "content": trigger_prompt}]

                # 5. 上下文注入（会自动注入 <monitor_status> 及最新截屏多模态，沈栖能直接看到手机屏幕画面！）
                engine = InjectionEngine()
                messages = await engine.inject_memory_only(
                    messages=messages,
                    request_info={"model": qq_cfg.DEFAULT_MODEL, "source": "qq-sleep-guardian", "include_screenshot": True}
                )

                session_id = f"qq-ruirui-sleep-guardian-{int(time.time())}"
                reply = await asyncio.wait_for(
                    send_to_gateway(session_id, "qq-sleep-guardian", messages),
                    timeout=90.0
                )

                # 6. 校验回复：若为空或明确输出 SKIP 则直接跳过不发
                if not reply or "SKIP" in reply.upper() or len(reply.strip()) == 0:
                    logger.info("[SLEEP_GUARDIAN] AI 未输出有效催睡内容或输出 SKIP，本次不发送")
                    return False

                # 7. 存储历史与发送 QQ 消息
                source_context = f"private_{qq_cfg.RUIRUI_QQ}"
                await message_store.store_unified_message('qq', source_context, 'assistant', reply)
                await split_and_send("private", qq_cfg.RUIRUI_QQ, reply)
                logger.info(f"[SLEEP_GUARDIAN] 催睡消息（第 {remind_level} 轮）已推送至 QQ: {reply}")
                return True

        except Exception as e:
            logger.warning(f"[SLEEP_GUARDIAN] AI 催睡生成异常或超时，直接放弃本次发送: {e}")
            return False

sleep_guardian = SleepGuardian()
