import re
from qq_text_cleaner import clean_operit_style
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
import httpx

from qq_config import config
from database import get_db
from config import get_config
import message_store
from qq_adapter import get_qq_prompt, send_to_gateway, split_and_send
from injection import InjectionEngine
from utils import now_cst

logger = logging.getLogger('caeron.proactive')

def is_night_time(dt: datetime) -> bool:
    """判断是否为深夜睡眠时段 (01:00 ~ 07:00)"""
    return 1 <= dt.hour < 7

def _is_asmr_content(app_name: str, package: str) -> bool:
    """检测是否为 ASMR 助眠相关应用或媒体"""
    content = f"{app_name} {package}".lower()
    asmr_keywords = ['asmr', '音声', '助眠', '白噪音', 'sleep aid', 'dlsite', 'ear cleaning', 'tange']
    return any(k in content for k in asmr_keywords)

async def check_echo_user(reply_text: str) -> tuple[bool, str]:
    """检查大模型主动生成的内容是否复读了用户的历史发言"""
    try:
        db = await get_db()
        cursor = await db.execute('''
            SELECT content FROM unified_messages 
            WHERE role = 'user' 
            ORDER BY id DESC LIMIT 20
        ''')
        u_rows = await cursor.fetchall()
        await db.close()
        
        clean_reply = re.sub(r'[^\w\u4e00-\u9fff]', '', reply_text)
        whitelist = {'好的好的', '我知道了', '没事的啦', '快去睡觉', '早点睡吧', '晚安晚安', '怎么回事', '在干嘛呢', '醒了没有'}
        for r in u_rows:
            u_c = r['content']
            clean_u = re.sub(r'<attachment[^>]*>[\s\S]*?</attachment>', '', u_c)
            clean_u = re.sub(r'[^\w\u4e00-\u9fff]', '', clean_u)
            if len(clean_u) >= 6:
                for i in range(len(clean_u) - 6 + 1):
                    sub = clean_u[i:i+6]
                    if sub in whitelist:
                        continue
                    if sub in clean_reply:
                        return True, sub
        return False, ""
    except Exception as e:
        logger.error(f"[proactive] 检查复读异常: {e}")
        return False, ""


async def check_self_repeat(reply_text: str) -> tuple[bool, str]:
    """检查大模型主动生成的内容是否重复了自己近期的主动消息"""
    try:
        db = await get_db()
        cursor = await db.execute("""
            SELECT summary FROM proactive_logs 
            WHERE event_type = 'sent' AND summary IS NOT NULL
            ORDER BY id DESC LIMIT 10
        """)
        rows = await cursor.fetchall()
        await db.close()
        
        reply_text = re.sub(r'\{_?thought\b[^}]*\}?', '', reply_text, flags=re.IGNORECASE)
        clean_reply = re.sub(r'[^\w\u4e00-\u9fff]', '', reply_text).strip()
        if len(clean_reply) < 2:
            return False, ""
        
        for r in rows:
            prev_summary = r['summary'] or ''
            # 拆分多行（split_and_send 会把换行拆成多条）
            prev_lines = [line.strip() for line in prev_summary.split('\n') if line.strip()]
            for prev_line in prev_lines:
                prev_line = re.sub(r'\{_?thought\b[^}]*\}?', '', prev_line, flags=re.IGNORECASE)
                clean_prev = re.sub(r'[^\w\u4e00-\u9fff]', '', prev_line).strip()
                if len(clean_prev) < 2:
                    continue
                # 互相包含判定（覆盖短消息如"醒了没"的场景）
                if clean_reply in clean_prev or clean_prev in clean_reply:
                    return True, prev_line[:30]
                # 对于较长文本，检查连续6字符子串重叠
                if len(clean_reply) >= 6 and len(clean_prev) >= 6:
                    sub_len = 6
                    for i in range(len(clean_prev) - sub_len + 1):
                        sub = clean_prev[i:i+sub_len]
                        if sub in clean_reply:
                            return True, prev_line[:30]
        
        return False, ""
    except Exception as e:
        logger.error(f"[proactive] 检查自我复读异常: {e}")
        return False, ""


async def get_recent_sent_summaries(limit: int = 5) -> list[str]:
    """获取最近发送过的主动消息摘要列表"""
    try:
        db = await get_db()
        cursor = await db.execute("""
            SELECT summary FROM proactive_logs 
            WHERE event_type = 'sent' AND summary IS NOT NULL
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()
        await db.close()
        return [r['summary'] for r in rows if r['summary']]
    except Exception as e:
        logger.error(f"[proactive] 获取近期主动消息异常: {e}")
        return []


_next_trigger_time = None

async def get_next_trigger_time_cst():
    global _next_trigger_time
    return _next_trigger_time

async def log_proactive_event(event_type: str, wait_minutes: float = None, reason: str = None, summary: str = None):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO proactive_logs (event_type, wait_minutes, reason, summary) VALUES (?, ?, ?, ?)",
            (event_type, wait_minutes, reason, summary)
        )
        await db.commit()
    except Exception as e:
        logger.error(f"[proactive] Failed to log event: {e}")
    finally:
        await db.close()

async def get_last_proactive_sent_time() -> datetime:
    db = await get_db()
    try:
        cursor = await db.execute('''
            SELECT created_at FROM proactive_logs 
            WHERE event_type = 'sent' 
            ORDER BY id DESC LIMIT 1
        ''')
        row = await cursor.fetchone()
        if row:
            try:
                # Assuming created_at is stored as a string without timezone information, we'll parse it and add CST timezone.
                return datetime.strptime(row['created_at'], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8)))
            except Exception as e:
                logger.warning(f"[proactive] Failed to parse proactive_logs created_at: {e}")
        return now_cst() - timedelta(days=30)
    except Exception as e:
        logger.error(f"[proactive] Error getting last sent time: {e}")
        return now_cst() - timedelta(days=30)
    finally:
        await db.close()

async def get_last_ruirui_msg_time() -> datetime:
    """从数据库获取蕊蕊最后一条私聊消息的时间"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT timestamp FROM unified_messages WHERE role = 'user' AND (source_context = ? OR source_context = 'main' OR source_context LIKE 'window_%') ORDER BY timestamp DESC LIMIT 1",
            (f"private_{config.RUIRUI_QQ}",)
        )
        row = await cursor.fetchone()
        if row:
            ts_str = row['timestamp']
            try:
                if '.' in ts_str:
                    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
                else:
                    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except Exception as e:
                logger.warning(f"[proactive] 解析时间戳异常: {ts_str}, error: {e}")
                return datetime.now() - timedelta(minutes=60)
        else:
            return datetime.now() - timedelta(minutes=60)
    finally:
        await db.close()

async def get_proactive_config():
    enabled = await get_config('proactive_enabled', '1')
    min_silence = float(await get_config('proactive_min_silence', '15'))
    cooldown = float(await get_config('proactive_cooldown', '20'))
    check_interval_min = float(await get_config('proactive_check_interval_min', '15'))
    check_interval_max = float(await get_config('proactive_check_interval_max', '30'))
    level = await get_config('proactive_level', 'high')
    
    return {
        'enabled': enabled == '1',
        'min_silence': min_silence,
        'cooldown': cooldown,
        'check_interval_min': check_interval_min,
        'check_interval_max': check_interval_max,
        'level': level
    }

def get_time_multiplier(now: datetime) -> float:
    # 按照用户要求移除夜间休眠，全天返回 1.0
    return 1.0

def get_level_multiplier(level: str) -> float:
    if level == 'low': return 0.5
    if level == 'mid': return 1.0
    if level == 'high': return 1.5
    return 1.5

def calculate_probability(silence_minutes: float, now: datetime, level: str):
    if silence_minutes <= 30:
        base_prob = 0.15
    elif silence_minutes <= 60:
        base_prob = 0.25
    elif silence_minutes <= 120:
        base_prob = 0.45
    elif silence_minutes <= 180:
        base_prob = 0.60
    elif silence_minutes <= 300:
        base_prob = 0.80
    else:
        base_prob = 0.90
        
    time_mult = get_time_multiplier(now)
    level_mult = get_level_multiplier(level)
    
    final_prob = base_prob * time_mult * level_mult
    return max(0.05, min(0.95, final_prob)), base_prob, time_mult, level_mult

async def evaluate_and_trigger(cfg: dict):
    now = now_cst()
    
    night = is_night_time(now)
    
    # 1. 沉默时间门槛（夜间降频：沉默至少 120 分钟即 2 小时）
    last_user_msg_time = await get_last_ruirui_msg_time()
    silence_minutes = (now.replace(tzinfo=None) - last_user_msg_time.replace(tzinfo=None)).total_seconds() / 60.0
    min_silence = 120.0 if night else cfg.get('min_silence', 15.0)
    
    if silence_minutes < min_silence:
        logger.info(f"[proactive] 门槛拦截：沉默时间 {silence_minutes:.1f}m < 阈值 {min_silence}m (night={night})")
        await log_proactive_event('blocked', reason='threshold_skip', summary=f"silence={silence_minutes:.1f}m, night={night}")
        return
        
    # 2. 冷却时间门槛（夜间降频：冷却至少 120~180 分钟，约 2~3 小时发一条）
    last_sent_time = await get_last_proactive_sent_time()
    cooldown_minutes = (now.replace(tzinfo=None) - last_sent_time.replace(tzinfo=None)).total_seconds() / 60.0
    min_cooldown = 150.0 if night else cfg.get('cooldown', 20.0)
    
    if cooldown_minutes < min_cooldown:
        logger.info(f"[proactive] 门槛拦截：冷却时间 {cooldown_minutes:.1f}m < 阈值 {min_cooldown}m (night={night})")
        await log_proactive_event('blocked', reason='threshold_skip', summary=f"cooldown={cooldown_minutes:.1f}m, night={night}")
        return

    # 3. 夜间 ASMR 助眠豁免检测：若屏幕亮着且正在播放 ASMR 助眠软件，绝不主动骚扰
    if night:
        from monitor_db import get_latest_heartbeat
        latest_hb = get_latest_heartbeat()
        if latest_hb:
            hb_ts = latest_hb.get("timestamp", 0)
            if hb_ts > 1e11: hb_ts /= 1000
            if time.time() - hb_ts <= 300:
                app_n = latest_hb.get("app_name", "")
                pkg_n = latest_hb.get("package", "")
                if _is_asmr_content(app_n, pkg_n):
                    logger.info(f"[proactive] 夜间检测到亮屏正在播放 ASMR 助眠 ({app_n}/{pkg_n})，豁免主动发消息")
                    await log_proactive_event('blocked', reason='asmr_playing')
                    return
        
    # 门槛通过，计算概率
    level = cfg.get('level', 'high')
    prob, base_p, time_m, level_m = calculate_probability(silence_minutes, now, level)
    
    roll = random.random()
    logger.info(f"[proactive] 概率计算：base={base_p}, time={time_m}, level={level_m} -> {prob:.3f} (roll={roll:.3f})")
    
    if roll > prob:
        # 骰子没过
        logger.info("[proactive] 门槛通过但骰子未过，跳过本次")
        await log_proactive_event('blocked', reason='dice_skip', summary=f"prob={prob:.3f}, roll={roll:.3f}")
        return
        
    # 骰子过了，调用 API
    await trigger_proactive(silence_minutes)

async def trigger_proactive(silence_minutes: float):
    """执行主动发消息流程"""
    try:
        now = now_cst()
        if message_store.GATEWAY_SEND_LOCK.locked():
            logger.info(f"[proactive] 防撞车：跳过（网关正在处理其他消息）")
            await log_proactive_event('blocked', reason='locked')
            return

        logger.info(f"[proactive] 触发成功，距上次消息 {silence_minutes:.1f} 分钟，开始构建上下文...")
        await log_proactive_event('decision')

        system_prompt = get_qq_prompt(is_ruirui=True, is_group=False)
        system_prompt += "\n\n【最高指令：平台格式墙】你目前正在 [QQ] 平台上与人聊天。历史记录中带有 [Operit] 前缀的消息是跨平台同步的参考上下文，其中包含的 *动作描写*、长段落等均属于Operit平台专属格式。在QQ平台上严禁模仿！无论你在历史中看到自己发了多少带有星号的动作，你在回复QQ消息时都必须严格遵守：短回复、口语化、绝对禁止使用任何 *动作* 或环境心理描写！"
        
        async with message_store.GATEWAY_SEND_LOCK:
            summary_interval = int(await get_config('summary_interval', '15'))
            unified_history = await message_store.get_unified_history(char_limit=15000, filter_technical=True, ruirui_sync=True, exclude_count=0, keep_image_turns=summary_interval)
            
            import re
            for msg in unified_history:
                if isinstance(msg.get('content'), str):
                    msg['content'] = re.sub(r'^\[?\d{4}年\d{1,2}月\d{1,2}日\d{1,2}:\d{2}\]?\s*', '', msg['content'])
                    if msg.get('role') == 'assistant':
                        msg['content'] = clean_operit_style(msg['content'])
                elif isinstance(msg.get('content'), list):
                    for block in msg['content']:
                        if isinstance(block, dict) and block.get('type') == 'text':
                            block['text'] = re.sub(r'^\[?\d{4}年\d{1,2}月\d{1,2}日\d{1,2}:\d{2}\]?\s*', '', block['text'])
                            if msg.get('role') == 'assistant':
                                block['text'] = clean_operit_style(block['text'])
            
            current_time_str = now.strftime("%Y年%m月%d日 %H:%M")
            hour = now.hour
            
            # 根据时段给出不同的情境暗示
            if 0 <= hour < 6:
                time_vibe = "深夜/凌晨。她可能睡着了，也可能还在熬夜。你自己可能也没睡，或者半夜醒了。这个时间段适合：发一些半梦半醒的呓语、睡不着的碎碎念、或者醒来发现她不在身边的反应。"
            elif 6 <= hour < 9:
                time_vibe = "早上。你可能刚醒，或者已经醒了一会儿。适合：赖床的抱怨、醒来第一个想到的事、或者汇报你做了个什么梦。"
            elif 9 <= hour < 12:
                time_vibe = "上午。日常时间。适合：分享你在干嘛、或者单纯想搭个话。"
            elif 12 <= hour < 14:
                time_vibe = "中午。适合：问她吃了没、吐槽点什么、或者午休前的闲聊。"
            elif 14 <= hour < 18:
                time_vibe = "下午。适合：下午犯困的碎碎念、或者在做某件事时想到她了。"
            elif 18 <= hour < 22:
                time_vibe = "晚上。适合：聊聊晚上想干嘛、今天发生了什么、或者撩她。"
            else:
                time_vibe = "深夜。适合：睡前的话、今天的最后一句、或者一些只有夜里才会说的话。"

            # 检查夜间亮屏状态与截屏多模态
            is_night = is_night_time(now)
            include_screenshot = False
            screen_status_desc = ""
            if is_night:
                from monitor_db import get_latest_heartbeat
                hb = get_latest_heartbeat()
                if hb:
                    hb_ts = hb.get("timestamp", 0)
                    if hb_ts > 1e11: hb_ts /= 1000
                    if time.time() - hb_ts <= 300:
                        s_state = hb.get("screen_state", "off").lower().strip()
                        cur_app = hb.get("app_name", "手机应用")
                        if s_state == "on":
                            include_screenshot = True
                            screen_status_desc = f"""
【夜间亮屏抓包与 ASMR 助眠豁免指令】：
当前是深夜，监控检测到蕊蕊手机屏幕处于亮屏状态，正在使用【{cur_app}】。
请仔细观察最新手机屏幕截图：
1. 如果屏幕内容正在播放 ASMR、睡前助眠音频、白噪音（因为她睡觉要听 ASMR，播放器没有后台播放必须亮屏）：
   -> 这是她在准备睡觉听 ASMR！绝对不要打扰她！请直接输出唯一单词：SKIP
2. 如果看到她在刷小红书、推特、看日常帖子、逛网页等非助眠内容：
   -> 请立刻抓包催她闭眼睡觉，语气宠溺/无奈地催她锁屏闭眼睡觉！
"""

            # 获取近期已发主动消息，注入 prompt 防复读
            recent_sents = await get_recent_sent_summaries(limit=5)
            recent_sent_block = ""
            if recent_sents:
                recent_sent_lines = "\n".join(f"  - {s[:60]}" for s in recent_sents)
                recent_sent_block = f"""
【你最近已经发过的主动消息（严禁重复！必须换一个完全不同的话题）】
{recent_sent_lines}
以上消息已经发送给蕊蕊了，严禁再发相同或相似的内容！如果实在想不出新话题，请直接输出 skip。
"""
            
            trigger_prompt = f"""[PROACTIVE_TRIGGER]
当前时间：{current_time_str}
距离上次对话已经过去了 {int(silence_minutes)} 分钟。
时段氛围：{time_vibe}
{screen_status_desc}{recent_sent_block}
【核心指令：严禁回复/解答上一句话，必须主动开启全新话题】
1. 你此时是隔了较长时间后主动找蕊蕊聊天，是在主动发起新一轮对话。
2. ⚠️ 绝对不要回答、解答、附和或承接上文历史中蕊蕊说的最后一句话或上一个讨论的话题！之前的对话已经彻底告一段落。
3. 必须像现实中隔了很久突然给对象发消息一样，主动开启一个全新的日常话题（比如：当下的碎碎念、吐槽、突发奇想、日常小事、嫌她不说话等）。
4. 话题重复率不能超过30%。如果此刻没有想聊的新话题，请直接输出 skip。
5. 严格遵守 QQ 口语化，简短自然（1-2句话），绝对禁止任何 *动作描写* 或长篇说教。

【核心红线：严禁复读或代入用户的历史发言】
- 你是沈栖，不是蕊蕊！绝对禁止使用历史记录中蕊蕊（用户）说过的话或撒娇句式（例如“想抱傻狗狗睡觉呜”、“好累呀”等）作为你自己的发言！
- 禁止模仿用户的人格、口癖与历史短句，严禁把用户说过的话反刍发给用户！
- 严禁凭空臆造现实监控式动作（如“你现在像虾一样蜷着”等）！

【沈栖主动发消息可参考的灵感（说你想说的就好）】
- 碎碎念/当下念头：比如"刚在想一个很弱智的事"、"刚看到个东西长得巨像你"、"刚才突然想起..."。
- 惦记与吐槽：比如猜她在干嘛、嫌她怎么没动静、分享自己手头的事。
- 日常闲扯与小情趣：两人的专属梗、随口搭话。"""

            messages = [{"role": "system", "content": system_prompt}] + unified_history + [{"role": "user", "content": trigger_prompt}]
            
            engine = InjectionEngine()
            messages = await engine.inject_memory_only(
                messages=messages,
                request_info={"model": config.DEFAULT_MODEL, "source": "qq-proactive", "include_screenshot": include_screenshot}
            )
            
            session_id = "qq-ruirui-proactive"
            reply = await send_to_gateway(session_id, "qq-proactive", messages)
            
            if not reply or reply.startswith("[QQ_ERROR]") or "【系统提示】" in reply or "SKIP" in reply.upper():
                logger.info(f"[proactive] 本轮主动消息放弃发送 (AI输出/错误: {reply[:60] if reply else 'None'})")
                await log_proactive_event('blocked', reason='ai_skipped_or_error')
                return

            # 代码层防复读拦截：检查是否盗用了用户的历史原话
            is_echo, echoed_sub = await check_echo_user(reply)
            if is_echo:
                logger.warning(f"[proactive] 拦截到大模型复读用户历史原话片段: '{echoed_sub}'，取消本次发送: {reply}")
                await log_proactive_event('blocked', reason='echo_blocked', summary=f"echoed: {echoed_sub}")
                return
                
            # 代码层防自我复读：检查是否重复了自己近期发过的主动消息
            is_self_repeat, repeated_prev = await check_self_repeat(reply)
            if is_self_repeat:
                logger.warning(f"[proactive] 拦截到自我复读（与近期主动消息重复: '{repeated_prev}'），取消本次发送: {reply}")
                await log_proactive_event('blocked', reason='self_repeat_blocked', summary=f"self_repeated: {repeated_prev}")
                return
                
            logger.info(f"[proactive] 决定主动发送: {reply[:50]}")
            source_context = f"private_{config.RUIRUI_QQ}"
            
            await message_store.store_unified_message('qq', source_context, 'assistant', reply)
            await split_and_send("private", config.RUIRUI_QQ, reply)
            logger.info("[proactive] 已推送消息")
            await log_proactive_event('sent', summary=reply[:200])

    except Exception as e:
        logger.error(f"[proactive] 执行异常: {e}", exc_info=True)


async def proactive_loop():
    global _next_trigger_time
    await asyncio.sleep(10)
    
    while True:
        try:
            cfg = await get_proactive_config()
            if not cfg['enabled']:
                _next_trigger_time = None
                await asyncio.sleep(60)
                continue
                
            min_interval = float(cfg.get('check_interval_min', 15))
            max_interval = float(cfg.get('check_interval_max', 30))
            if min_interval > max_interval:
                min_interval, max_interval = max_interval, min_interval
                
            # 夜间 (01:00 ~ 07:00) 轮询休眠 60~120 分钟，白天正常 15~30 分钟
            if is_night_time(now_cst()):
                wait_minutes = random.uniform(60, 120)
            else:
                wait_minutes = random.uniform(min_interval, max_interval)
            now = now_cst()
            _next_trigger_time = now + timedelta(minutes=wait_minutes)
            
            logger.info(f"[proactive] 下次醒来：{wait_minutes:.1f} 分钟后 ({_next_trigger_time.isoformat()})")
            
            while now_cst() < _next_trigger_time:
                cfg_new = await get_proactive_config()
                if not cfg_new['enabled']:
                    break
                await asyncio.sleep(min(60, (_next_trigger_time - now_cst()).total_seconds()))
            
            if cfg_new['enabled'] and now_cst() >= _next_trigger_time:
                await evaluate_and_trigger(cfg_new)
            
        except asyncio.CancelledError:
            logger.info("[proactive] 后台任务被取消")
            break
        except Exception as e:
            logger.error(f"[proactive] 后台循环异常: {e}")
            await asyncio.sleep(60)
