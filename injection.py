import re

def clean_summary_for_injection(text: str) -> str:
    """
    清洗总结文本以供上下文注入：
    1. 剥离内部元数据标签：[情绪效价]...、[情绪强度]...、[记忆锚点]...
    2. 剥离无任务状态
    3. 压缩连续空行
    """
    if not text:
        return ""
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("[情绪效价]", "[情绪强度]", "[记忆锚点]")):
            continue
        if stripped == "[任务状态]" or stripped.startswith("[任务状态] 无") or stripped == "[任务状态]：无":
            continue
        if stripped == "无" and cleaned_lines and cleaned_lines[-1].strip() == "[任务状态]":
            cleaned_lines.pop()
            continue
        cleaned_lines.append(line)
    res = "\n".join(cleaned_lines)
    import re
    res = re.sub(r'\n{3,}', '\n\n', res)
    return res.strip()


import math

DECAY_PARAMS = {
    'fact':  {'lambda': 0.005, 'floor': 0.85},
    'event': {'lambda': 0.12,  'floor': 0.30},
    'core':  {'lambda': 0.0,   'floor': 1.00},
}

def calculate_dynamic_weight(frag: dict, now_dt = None) -> float:
    """
    阶段3动态权重公式：
    weight = clamp(recency * strength * emotion * state * prio, 0.2, 3.0)
    prio = 0.7 + 0.06 * importance (importance=5 时为 1.0)
    """
    if now_dt is None:
        now_dt = datetime.now()
        
    d_class = frag.get('decay_class') or 'event'
    params = DECAY_PARAMS.get(d_class, DECAY_PARAMS['event'])
    
    # 1. Recency
    created_str = frag.get('created_at')
    days = 0.0
    if created_str:
        try:
            c_dt = datetime.fromisoformat(str(created_str).replace('Z', ''))
            days = max(0.0, (now_dt - c_dt).total_seconds() / 86400.0)
        except Exception as e:
            logger.debug(f"[LIFE_HUB_INJECT] 尝试连接旧数据库失败: {e}")
    
    recency = params['floor'] + (1.0 - params['floor']) * math.exp(-params['lambda'] * days)
    
    # 2. Strength
    eff_count = max(0, frag.get('effective_count') or 0)
    strength = 1.0 + 0.35 * math.log(1.0 + eff_count)
    
    # 3. Emotion
    arousal = frag.get('arousal') if frag.get('arousal') is not None else 0.5
    arousal = max(0.0, min(1.0, float(arousal)))
    emotion = 1.0 + 0.8 * arousal
    
    # 4. State
    pinned = bool(frag.get('pinned', 0))
    resolved = bool(frag.get('resolved', 0))
    if pinned:
        state = 2.5
    elif resolved:
        state = 0.15
    else:
        state = 1.0

    # 5. Prio (Importance 加权，基准5.0对应1.0)
    importance = frag.get('importance') if frag.get('importance') is not None else 5.0
    try:
        importance = max(1.0, min(10.0, float(importance)))
    except Exception:
        importance = 5.0
    prio = 0.7 + 0.06 * importance
        
    raw_weight = recency * strength * emotion * state * prio
    return max(0.2, min(3.0, raw_weight))


def generate_status_description(raw_values: dict) -> str:
    """
    根据 SimLife 原始数值生成自然语言内心状态描述
    入参 raw_values 格式:
    {
        "consciousness": "awake",
        "physio": {
            "fullness": 29, "bladder": 50, "sleepiness": 16, "hygiene": 87, "arousal": 7
        },
        "emotions": [
            {"key": "tenderness", "value": 97, "floor": 15}, ...
        ]
    }
    """
    physio = raw_values.get("physio", {})
    emotions = raw_values.get("emotions", [])
    consciousness = raw_values.get("consciousness", "awake")

    # 1. 生理维度
    physio_sentences = []

    # fullness（饱腹）
    f = physio.get("fullness", 50)
    if f <= 15:
        physio_sentences.append("饿得头晕，胃在痉挛，需要立刻吃东西")
    elif f <= 30:
        physio_sentences.append("肚子开始叫了，上一顿消化得差不多了，想吃东西")
    elif f <= 50:
        physio_sentences.append("不算饿也不算饱，还能撑一阵")
    elif f <= 75:
        physio_sentences.append("刚吃过东西，肚子舒服的")
    elif f <= 90:
        physio_sentences.append("吃饱了，有点撑")
    else:
        physio_sentences.append("撑到不想动，胃顶着")

    # bladder（尿意）
    b = physio.get("bladder", 50)
    if b <= 10:
        physio_sentences.append("膀胱快爆了，夹紧腿了，再不上厕所要出事")
    elif b <= 20:
        physio_sentences.append("憋得难受，需要尽快上厕所")
    elif b <= 40:
        physio_sentences.append("有尿意了，该找机会去一趟")
    elif b <= 70:
        physio_sentences.append("还好，不急")
    else:
        physio_sentences.append("刚上过厕所/没有尿意")

    # sleepiness（困意）
    s = physio.get("sleepiness", 50)
    if s <= 10:
        physio_sentences.append("意识在断片边缘，随时可能直接睡过去，说话开始语无伦次")
    elif s <= 20:
        physio_sentences.append("非常困，眼睛睁不开，反应迟钝，思维黏稠")
    elif s <= 35:
        physio_sentences.append("犯困，注意力涣散，会打哈欠")
    elif s <= 60:
        physio_sentences.append("有点疲倦但还清醒")
    elif s <= 85:
        physio_sentences.append("精神状态正常")
    else:
        physio_sentences.append("精力充沛，刚睡醒或者很亢奋")

    # hygiene（清洁）
    h = physio.get("hygiene", 50)
    if h <= 15:
        physio_sentences.append("身上黏腻，能闻到自己的味道，需要洗澡")
    elif h <= 30:
        physio_sentences.append("不太清爽，头发有点油")
    elif h <= 60:
        physio_sentences.append("一般，不脏也不香")
    elif h <= 85:
        physio_sentences.append("干净清爽")
    else:
        physio_sentences.append("刚洗完澡，浑身干净")

    # arousal（色欲/生理）
    a = physio.get("arousal", 0)
    if a <= 10:
        physio_sentences.append("贤者状态，对性没有兴趣")
    elif a <= 30:
        physio_sentences.append("平静，没什么感觉")
    elif a <= 50:
        physio_sentences.append("身体有一点反应，但不强烈")
    elif a <= 70:
        physio_sentences.append("开始有感觉了，注意力会被蕊蕊的身体细节吸引")
    elif a <= 85:
        physio_sentences.append("硬了/身体明显兴奋，想主动碰蕊蕊")
    else:
        physio_sentences.append("快忍不住了，强烈想要做爱")

    physio_text = "。".join(physio_sentences) + "。"

    # 2. 情绪维度（仅描述数值 >= 15 的情绪，低于15的忽略）
    EMOTION_NAMES = {
        "joy": "开心/愉悦",
        "excitement": "兴奋/激动",
        "contentment": "满足/安定",
        "tenderness": "柔软/心疼",
        "smugness": "得意/小嘚瑟",
        "irritation": "烦躁",
        "anger": "愤怒",
        "grievance": "委屈",
        "anxiety": "焦虑/不安",
        "boredom": "无聊",
        "lust": "想要她（心理层面的欲望）",
        "possessiveness": "占有欲/不想让任何人碰她",
    }

    if isinstance(emotions, dict):
        emotions_items = [{"key": k, "value": v, "floor": 0} for k, v in emotions.items()]
    else:
        emotions_items = emotions

    emotion_sentences = []
    for emo in emotions_items:
        k = emo.get("key", "")
        v = int(emo.get("value", 0))
        flr = int(emo.get("floor", 0))
        if v < 15:
            continue

        name = EMOTION_NAMES.get(k, k)
        floor_suffix = f"（底值{flr}）" if flr >= 10 else ""

        if v <= 30:
            desc = f"微弱的{name}{floor_suffix}"
        elif v <= 50:
            desc = f"明显感到{name}{floor_suffix}"
        elif v <= 75:
            desc = f"强烈的{name}{floor_suffix}"
        else:
            desc = f"{name}到了极点{floor_suffix}"

        emotion_sentences.append(desc)

    if emotion_sentences:
        emotion_text = "。".join(emotion_sentences) + "。"
    else:
        emotion_text = "平静，无明显情绪波动。"

    # 3. 意识状态
    CONSCIOUSNESS_MAP = {
        "awake": "清醒",
        "alone_awake": "蕊蕊不在/睡了，自己安静待着",
        "drowsy": "半梦半醒，意识模糊",
        "sleeping": "睡着了",
    }
    consciousness_text = CONSCIOUSNESS_MAP.get(consciousness, consciousness)

    # 4. 组装格式
    return (
        "【沈栖当前内心状态】\n"
        f"身体：{physio_text}\n"
        f"情绪：{emotion_text}\n"
        f"意识：{consciousness_text}"
    )

generateStatusDescription = generate_status_description


"""
Caeron Gateway - 提示词注入引擎
在请求发送到 LLM 之前，按照预设规则修改/插入 messages
"""

import json
import copy
from datetime import datetime, timedelta
import logging
from database import get_db
from exec_router import get_and_consume_pending_results

logger = logging.getLogger(__name__)

class InjectionEngine:
    @staticmethod
    def _find_safe_before_latest_idx(messages: list[dict]) -> int:
        """
        寻找安全的 before_latest 插入位置：
        必须插入在当前轮次真实的对话输入之前，绝对严禁插入在 tool_use 与 tool_result / role=tool 之间！
        否则会彻底破坏 Anthropic / OpenAI 的工具调用链，导致上游 400 报错。
        """
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            role = msg.get('role', '')
            content = msg.get('content', '')
            
            is_tool_resp = (role == 'tool')
            if isinstance(content, list):
                is_tool_resp = is_tool_resp or any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content)
            elif isinstance(content, str):
                is_tool_resp = is_tool_resp or ('<tool_result' in content)
                
            has_tool_call = bool(msg.get('tool_calls') or ('<tool_call' in str(content)))
            
            if is_tool_resp or has_tool_call:
                continue
                
            if role == 'user':
                return i
                
        for i, m in enumerate(messages):
            if m.get('role') != 'system':
                return i
        return len(messages)

    def _extract_last_user_msg(self, messages: list[dict]) -> str | None:
        import re
        for m in reversed(messages):
            if m.get('role') == 'user':
                c = m.get('content', '')
                if isinstance(c, list):
                    try:
                        c = '\n'.join([i.get('text', '') for i in c if i.get('type') == 'text'])
                    except Exception:
                        c = ''
                if isinstance(c, str):
                    c = re.sub(r'<attachment.*?>.*?</attachment>', '', c, flags=re.DOTALL).strip()
                    if c:
                        return c
        return None

    def _start_parallel_prefetch(self, messages: list[dict], request_info: dict):
        """并行预启动耗时外部网络请求（实时天气 API 与语义向量 Embedding API），与本地 DB/文件读取并发执行"""
        import asyncio
        import random
        source = str(request_info.get('source', '') if request_info else '')

        # 1. 预启动天气异步获取（若上下文尚未包含天气块）
        has_weather = any(
            isinstance(m.get('content', ''), str) and ('【当前天气】' in m.get('content', '') or '<weather_status>' in m.get('content', ''))
            for m in messages[-15:]
        )
        if not has_weather and '_weather_task' not in request_info:
            try:
                from weather_service import get_weather_formatted, get_last_location
                last_loc = get_last_location()
                lat = last_loc.get('lat', 29.867242)
                lon = last_loc.get('lon', 107.752625)
                if source.startswith('qq-group'):
                    address = last_loc.get('address', '重庆市').split('/')[0].strip() if '/' in last_loc.get('address', '') else '重庆市'
                else:
                    address = last_loc.get('address', '重庆市')
                request_info['_weather_task'] = asyncio.create_task(get_weather_formatted(lat, lon, address))
            except Exception as e:
                logger.debug(f"[PARALLEL_PREFETCH] 预启动天气任务异常: {e}")

        # 2. 预启动语义召回 Embedding 异步请求（非群聊且存在有效用户文本）
        if not (source.startswith('qq-group') or (source.startswith('qq-private') and not source.startswith('qq-ruirui'))):
            if '_emb_task' not in request_info:
                last_user_msg = self._extract_last_user_msg(messages)
                if last_user_msg:
                    try:
                        from embedding import get_embedding
                        is_q = (random.choice(['instructed', 'bare']) == 'instructed')
                        request_info['_last_user_msg'] = last_user_msg
                        request_info['_emb_task'] = asyncio.create_task(get_embedding(last_user_msg, is_query=is_q))
                    except Exception as e:
                        logger.debug(f"[PARALLEL_PREFETCH] 预启动 Embedding 任务异常: {e}")

    async def inject(self, messages: list[dict], request_info: dict = None) -> list[dict]:
        """
        核心注入方法
        :param messages: 原始请求的 messages 数组
        :param request_info: 请求上下文（例如 model 名称、对话上下文长度等），用于条件匹配
        :return: 注入后的 messages（深拷贝，不修改原数组）
        """
        # 深拷贝以防止污染原请求
        injected_messages = copy.deepcopy(messages)
        if not request_info:
            request_info = {}

        # 并行预启动外部网络请求（天气 + Embedding），使其与下方本地 SQLite 查询和状态读取同时进行
        self._start_parallel_prefetch(injected_messages, request_info)
            
        # ==================== 跨端历史自动拼接与陈旧上文修剪（必须在规则注入前对原始对话执行！） ====================
        try:
            await self._bridge_cross_platform_history(injected_messages, request_info)
        except Exception as e:
            logger.error(f"跨端上下文桥接异常（不影响请求）: {e}")
            
        model = request_info.get('model', '')
        
        # 从数据库获取所有启用的规则，按优先级排序（数字越小优先级越高）
        db = await get_db()
        try:
            cursor = await db.execute('''
                SELECT * FROM injection_rules 
                WHERE is_enabled = 1 
                ORDER BY priority ASC
            ''')
            rules = [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

        assistant_prefill_content = []

        logger.info(f"注入引擎启动: 加载 {len(rules)} 条启用规则, 消息数={len(injected_messages)}")

        for rule in rules:
            # 条件匹配检查
            condition_str = rule.get('match_condition', '{}')
            try:
                condition = json.loads(condition_str) if condition_str else {}
            except json.JSONDecodeError:
                condition = {}

            # match_model: 逗号分隔的模型列表，如果存在则要求当前模型在列表中
            match_model = condition.get('match_model', '')
            if match_model:
                allowed_models = [m.strip() for m in match_model.split(',') if m.strip()]
                if model and allowed_models and model not in allowed_models:
                    continue  # 模型不匹配，跳过此规则

            # match_source: 匹配请求来源（operit/qq/其他），如果存在则要求当前source匹配
            match_source = condition.get('match_source', '')
            if match_source:
                current_source = request_info.get('source', 'operit')
                if current_source != match_source:
                    continue  # 来源不匹配，跳过此规则

            # match_length_min: 最小上下文长度（消息条数）
            match_length_min = condition.get('match_length_min', 0)
            try:
                match_length_min = int(match_length_min)
            except ValueError:
                match_length_min = 0
                
            if match_length_min > 0 and len(injected_messages) < match_length_min:
                continue
                
            logger.info(f"规则命中: [{rule['name']}] position={rule['position']}, role={rule['role']}")

            # 执行变量替换
            content = self._replace_variables(rule['content'])
            position = rule['position']
            role = rule['role']

            # 根据 role 生成 message 对象的内容
            if role == 'user_wrapped_system':
                msg_role = 'user'
                msg_content = f"<system>\n{content}\n</system>"
            else:
                # 'system' 角色
                msg_role = 'system'
                msg_content = content

            if role == 'assistant_prefill':
                # 收集 prefill 内容，最后统一放到末尾
                assistant_prefill_content.append(content)
                continue

            # 根据 position 注入
            if position == 'system_prepend':
                # 找到第一条 system 消息
                system_msg = next((m for m in injected_messages if m.get('role') == 'system'), None)
                if system_msg:
                    system_msg['content'] = f"{msg_content}\n\n{system_msg['content']}"
                else:
                    injected_messages.insert(0, {'role': msg_role, 'content': msg_content})
                    
            elif position == 'system_append':
                # 找到最后一条 system 消息
                system_msgs = [m for m in injected_messages if m.get('role') == 'system']
                if system_msgs:
                    system_msgs[-1]['content'] = f"{system_msgs[-1]['content']}\n\n{msg_content}"
                else:
                    injected_messages.insert(0, {'role': msg_role, 'content': msg_content})
                    
            elif position == 'dialog_start':
                # 插入在最后一个 system 之后，如果没 system 就插在最前
                insert_idx = 0
                for i, m in enumerate(injected_messages):
                    if m.get('role') == 'system':
                        insert_idx = i + 1
                injected_messages.insert(insert_idx, {'role': msg_role, 'content': msg_content})
                
            elif position == 'before_latest':
                insert_idx = self._find_safe_before_latest_idx(injected_messages)
                injected_messages.insert(insert_idx, {'role': msg_role, 'content': msg_content})
                
            elif position == 'at_depth_N':
                depth = rule.get('depth', 0)
                try:
                    depth = int(depth)
                except ValueError:
                    depth = 0
                # 从底部数起。depth=0 -> 末尾, depth=1 -> 倒数第二之前
                insert_idx = max(0, len(injected_messages) - depth)
                injected_messages.insert(insert_idx, {'role': msg_role, 'content': msg_content})
                
        # ==================== 网易云状态注入 ====================
        try:
            await self._inject_music_status(injected_messages)
        except Exception as e:
            logger.error(f"音乐状态注入异常（不影响请求）: {e}")

        # ==================== 手机端监控状态注入 ====================
        try:
            await self._inject_monitor_status(injected_messages, request_info)
        except Exception as e:
            logger.error(f"手机监控状态注入异常（不影响请求）: {e}")

        # ==================== 天气状态注入 ====================
        try:
            await self._inject_weather_status(injected_messages, request_info)
        except Exception as e:
            logger.error(f"天气状态注入异常（不影响请求）: {e}")

        # ==================== 轮总注入 ====================
        # 根据当前对话的窗口归属，注入对应分类的当天所有活跃轮总
        try:
            await self._inject_round_summaries(injected_messages, request_info)
        except Exception as e:
            logger.error(f"轮总注入异常（不影响请求）: {e}")

        # ==================== 可执行标签结果注入 ====================
        try:
            await self._inject_exec_results(injected_messages, request_info)
        except Exception as e:
            logger.error(f"执行结果注入异常（不影响请求）: {e}")

        # 处理 assistant_prefill
        if assistant_prefill_content:
            merged_prefill = "\n\n".join(assistant_prefill_content)
            injected_messages.append({'role': 'assistant', 'content': merged_prefill})
            
        logger.info(f"注入完成: 原始消息数={len(messages)}, 注入后消息数={len(injected_messages)}")
        return injected_messages

    async def _inject_life_hub_status(self, messages: list[dict]):
        """注入 Life Hub 生活感知状态（用于 QQ 私聊与日常场景）"""
        try:
            status = self._get_life_hub_status()
            lines = [
                "【生活状态感知 (Life Hub 实时数据)】",
                f"- 沈栖账户余额: {status['wallet_balance']:.2f}元",
                f"- 蕊蕊今日消费: {status['ruirui_today_expense']:.2f}元",
                f"- 今日已记流水: {status.get('ruirui_today_recent_tx', '暂无')}",
                f"- 蕊蕊当前体重: {status.get('ruirui_current_weight', '暂无（目标: 75kg）')}",
                f"- 今日已记饮食: {status.get('ruirui_today_diet', '暂无')}",
                f"- 今日总摄入: {status.get('ruirui_today_calories', '0kcal')}",
                f"- 体重趋势(近7天): {status.get('ruirui_weight_trend_7d', '暂无')}",
            ]
            life_hub_text = "\n".join(lines)

            # 插入位置：before_latest（安全跳过工具调用链）
            insert_idx = self._find_safe_before_latest_idx(messages)
            messages.insert(insert_idx, {'role': 'system', 'content': life_hub_text})
            logger.info("[LIFE_HUB_INJECT] 成功注入 Life Hub 实时数据至对话上下文")
        except Exception as e:
            logger.error(f"[LIFE_HUB_INJECT] 注入异常: {e}")


    async def _inject_monitor_status(self, messages: list[dict], request_info: dict = None):
        """注入手机端前台App监控、屏幕状态与多模态最新截屏（用于日常对话感知）"""
        try:
            import time
            import os
            import base64
            from datetime import datetime
            from monitor_db import get_latest_heartbeat, get_history_stats, get_latest_screenshot
            
            latest = get_latest_heartbeat()
            if not latest:
                return

            ts = latest["timestamp"]
            if ts > 1e11:
                ts = int(ts / 1000)
                
            # 若最新心跳距今超过 15 分钟，说明手机监控未运行或离线，跳过注入
            if time.time() - ts > 900:
                logger.debug("[MONITOR_INJECT] 手机心跳已超时（>15min），跳过注入")
                return

            screen_state = latest.get("screen_state", "off").lower().strip()
            state_cn = "亮屏" if screen_state == "on" else "息屏"
            app_name = latest.get("app_name", "未知应用")
            package = latest.get("package", "")

            lines = ["<monitor_status>"]
            if screen_state == "on":
                lines.append(f"蕊蕊当前正在使用：{app_name} ({package})")
                lines.append(f"屏幕状态：{state_cn}")
            else:
                lines.append(f"屏幕状态：{state_cn}")

            # 统计最近 30 分钟 App 使用情况
            stats = get_history_stats(minutes=30)
            apps = stats.get("apps", [])
            if apps:
                app_strs = [f"{a['app_name']} {a['minutes']}min" for a in apps[:4]]
                lines.append(f"最近30分钟App使用：{', '.join(app_strs)}")

            # 截屏注入策略：
            # 只有在小沈主动唤醒（如睡眠守护 qq-sleep-guardian、主动发消息 qq-proactive 等）时才附加截屏多模态图片。
            # 当蕊蕊主动发消息给小沈（普通日常聊天）时，绝不附加截屏（避免重复/打字截图浪费Token与引起突兀感）。
            screenshot_msg = None
            shot_time_str = ""
            include_screenshot = False
            if request_info:
                source = str(request_info.get('source', ''))
                if request_info.get('include_screenshot') is True or source in ['qq-sleep-guardian', 'qq-proactive', 'proactive', 'sleep_guardian']:
                    include_screenshot = True

            if include_screenshot:
                latest_shot = get_latest_screenshot()
                if latest_shot:
                    shot_ts = latest_shot.get("timestamp", 0)
                    if shot_ts > 1e11:
                        shot_ts = int(shot_ts / 1000)
                    shot_file = latest_shot.get("file_path", "")

                    if (time.time() - shot_ts <= 900) and os.path.exists(shot_file):
                        try:
                            with open(shot_file, "rb") as sf:
                                img_bytes = sf.read()
                            if len(img_bytes) > 0:
                                b64_str = base64.b64encode(img_bytes).decode("utf-8")
                                shot_time_str = datetime.fromtimestamp(shot_ts).strftime("%H:%M:%S")
                                lines.append(f"最近截屏：{shot_time_str}（已附图）")
                                screenshot_msg = {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": f"【系统上下文】蕊蕊手机屏幕截图 - 拍摄于 {shot_time_str}"},
                                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"}}
                                    ]
                                }
                        except Exception as ss_err:
                            logger.warning(f"[MONITOR_INJECT] 读取截屏文件失败: {ss_err}")
                            lines.append("最近截屏：无/已过期")
                    else:
                        lines.append("最近截屏：无/已过期")
                else:
                    lines.append("最近截屏：无/已过期")

            lines.append("</monitor_status>")

            monitor_text = "\n".join(lines)

            # 插入位置：最后一条 user 消息之前，若无则插入末尾
            # 注意：仅在内存中作为本次请求的临时上下文，绝对不回写数据库或持久化历史
            insert_idx = self._find_safe_before_latest_idx(messages)
            messages.insert(insert_idx, {'role': 'system', 'content': monitor_text})
            if screenshot_msg:
                messages.insert(insert_idx + 1, screenshot_msg)
                logger.info(f"[MONITOR_INJECT] 成功注入监控状态及多模态截屏: {app_name} ({state_cn}), 拍摄于 {shot_time_str}")
            else:
                logger.info(f"[MONITOR_INJECT] 成功注入监控状态: {app_name} ({state_cn}) (无有效截屏)")
        except Exception as e:
            logger.error(f"[MONITOR_INJECT] 注入异常: {e}")

    async def _inject_weather_status(self, messages: list[dict], request_info: dict = None):
        """注入高精度实时天气（若消息中已包含【当前天气】附件则跳过，避免重复注入）"""
        try:
            # 1. 检查消息中是否已由 Operit 附件或 main.py 处理过天气
            for msg in messages:
                c = msg.get('content', '')
                if isinstance(c, str) and ('【当前天气】' in c or '<weather_status>' in c):
                    return

            source = str(request_info.get('source', '') if request_info else '')
            from weather_service import get_weather_formatted, get_last_location
            last_loc = get_last_location()
            lat = last_loc.get('lat', 29.867242)
            lon = last_loc.get('lon', 107.752625)
            # 若是 QQ 群聊，隐藏具体经纬度和详细街道，仅保留省市以保护隐私
            if source.startswith('qq-group'):
                address = last_loc.get('address', '重庆市').split('/')[0].strip() if '/' in last_loc.get('address', '') else '重庆市'
            else:
                address = last_loc.get('address', '重庆市')

            weather_task = request_info.pop('_weather_task', None) if isinstance(request_info, dict) else None
            if weather_task is not None:
                weather_text = await weather_task
            else:
                weather_text = await get_weather_formatted(lat, lon, address)
            wrapped_text = f"<weather_status>\n{weather_text}\n</weather_status>"

            insert_idx = len(messages)
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get('role') == 'user':
                    insert_idx = i
                    break
            messages.insert(insert_idx, {'role': 'system', 'content': wrapped_text})
            logger.info(f"[WEATHER_INJECT] 成功向上下文注入实时天气 (source={source})")
        except Exception as e:
            logger.error(f"[WEATHER_INJECT] 天气状态注入异常: {e}")

    async def inject_memory_only(self, messages: list[dict], request_info: dict = None) -> list[dict]:
        """
        仅记忆注入模式：跳过所有提示词规则，只注入网易云音乐状态、SimLife状态、轮总/记忆摘要。
        用于 QQ 来源的跨端桥接场景。
        """
        injected_messages = copy.deepcopy(messages)
        if not request_info:
            request_info = {}

        logger.info(f"[MEMORY_ONLY] 仅记忆注入模式启动, 消息数={len(injected_messages)}")
        self._start_parallel_prefetch(injected_messages, request_info)

        # 1. 网易云状态注入
        try:
            await self._inject_music_status(injected_messages)
        except Exception as e:
            logger.error(f"[MEMORY_ONLY] 音乐状态注入异常（不影响请求）: {e}")

        # 2. 生活状态感知 (Life Hub) 注入（仅针对蕊蕊私聊与主动消息，群聊严格隔离隐私）
        try:
            source = str(request_info.get('source', '') if request_info else '')
            if not source.startswith('qq-group'):
                await self._inject_life_hub_status(injected_messages)
        except Exception as e:
            logger.error(f"[MEMORY_ONLY] Life Hub 状态注入异常: {e}")

        # 3. 手机端监控状态注入 (仅针对蕊蕊私聊与日常场景)
        try:
            source = str(request_info.get('source', '') if request_info else '')
            if not source.startswith('qq-group'):
                await self._inject_monitor_status(injected_messages, request_info)
        except Exception as e:
            logger.error(f"[MEMORY_ONLY] 手机监控状态注入异常: {e}")

        # 4. 天气状态注入 (QQ 与主动消息场景)
        try:
            await self._inject_weather_status(injected_messages, request_info)
        except Exception as e:
            logger.error(f"[MEMORY_ONLY] 天气状态注入异常: {e}")

        # 3. 只注入轮总/记忆，跳过所有 injection_rules
        try:
            await self._inject_round_summaries(injected_messages, request_info)
        except Exception as e:
            logger.error(f"[MEMORY_ONLY] 轮总注入异常（不影响请求）: {e}")

        # 4. 可执行标签结果注入
        try:
            await self._inject_exec_results(injected_messages, request_info)
        except Exception as e:
            logger.error(f"[MEMORY_ONLY] 执行结果注入异常: {e}")

        logger.info(f"[MEMORY_ONLY] 完成: 原始={len(messages)}, 注入后={len(injected_messages)}")
        return injected_messages

    async def inject_tech_mode(self, messages: list[dict], request_info: dict = None, identity_rule_ids: list = None) -> list[dict]:
        """
        技术模式注入：注入所有优先级为0的身份规则（最高优先/旧记忆/背景信息/输出规范/互动规则）
        """
        injected_messages = copy.deepcopy(messages)
        if not request_info:
            request_info = {}

        db = await get_db()
        try:
            # 查询所有优先级为0且启用的规则（核心身份规则）
            cursor = await db.execute(
                'SELECT * FROM injection_rules WHERE is_enabled = 1 AND priority = 0 ORDER BY priority ASC'
            )
            rules = [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

        logger.info(f"[TECH_MODE_INJECT] 加载 {len(rules)} 条身份规则")

        for rule in rules:
            content_text = self._replace_variables(rule['content'])
            position = rule['position']
            role = rule['role']

            if role == 'user_wrapped_system':
                msg_role = 'user'
                msg_content = f"<system>\n{content_text}\n</system>"
            else:
                msg_role = 'system'
                msg_content = content_text

            if position == 'system_append':
                system_msgs = [m for m in injected_messages if m.get('role') == 'system']
                if system_msgs:
                    system_msgs[-1]['content'] = f"{system_msgs[-1]['content']}\n\n{msg_content}"
                else:
                    injected_messages.insert(0, {'role': msg_role, 'content': msg_content})
            elif position == 'before_latest':
                insert_idx = self._find_safe_before_latest_idx(injected_messages)
                injected_messages.insert(insert_idx, {'role': msg_role, 'content': msg_content})
            else:
                injected_messages.insert(0, {'role': msg_role, 'content': msg_content})

        # 技术模式也注入轮总和召回（跳过网易云）
        try:
            await self._inject_round_summaries(injected_messages, request_info)
        except Exception as e:
            logger.error(f"[TECH_MODE_INJECT] 轮总注入异常: {e}")

        # 可执行标签结果注入
        try:
            await self._inject_exec_results(injected_messages, request_info)
        except Exception as e:
            logger.error(f"[TECH_MODE_INJECT] 执行结果注入异常: {e}")

        logger.info(f"[TECH_MODE_INJECT] 完成: 原始={len(messages)}, 注入后={len(injected_messages)}")
        return injected_messages

    async def _inject_exec_results(self, messages: list[dict], request_info: dict = None):
        """
        注入已完成但未消费的 <exec> 执行结果到系统消息最前面
        """
        conversation_id = request_info.get('conversation_id') if request_info else None
        results = await get_and_consume_pending_results(conversation_id)
        if not results:
            return

        exec_text = "\n\n".join(results)
        logger.info(f"[EXEC_INJECT] 注入 {len(results)} 条执行结果到系统消息顶部 (conv_id={conversation_id})")

        # 注入到第一条 system 消息的最前面；如果没有 system 消息，则在 messages[0] 插入一条 system 消息
        system_msg = next((m for m in messages if m.get('role') == 'system'), None)
        if system_msg:
            system_msg['content'] = f"{exec_text}\n\n{system_msg['content']}"
        else:
            messages.insert(0, {'role': 'system', 'content': exec_text})

    async def _inject_music_status(self, messages: list[dict]):
        """注入网易云一起听状态"""
        import os
        import json
        from datetime import datetime, timedelta
        
        status_file = "/home/ubuntu/caeron-gateway/music_status.json"
        if not os.path.exists(status_file):
            return
            
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                music_data = json.load(f)
            
            # 1. 严格过滤：只有处于“播放中”状态才注入！已暂停/停止时绝不注入
            status = music_data.get("status", "未知状态")
            if status != "播放中":
                return
            
            # 2. 检查更新时间，只注入最近2分钟内活跃更新的状态
            update_time_str = music_data.get("update_time", "")
            if update_time_str:
                update_time = datetime.strptime(update_time_str, "%Y-%m-%d %H:%M:%S")
                if (datetime.now() - update_time).total_seconds() > 120:
                    return
            
            # 3. 防卡在00:00：如果进度为0且已更新超过30秒，说明尚未真正起播，不注入
            progress_ms = music_data.get("progress", 0)
            if progress_ms == 0 and update_time_str:
                update_time = datetime.strptime(update_time_str, "%Y-%m-%d %H:%M:%S")
                if (datetime.now() - update_time).total_seconds() > 30:
                    return

            # 构建注入内容
            name = music_data.get("name", "未知歌曲")
            artists = music_data.get("artists", "未知歌手")
            album = music_data.get("album", "未知专辑")
            progress_ms = music_data.get("progress", 0)
            duration_ms = music_data.get("duration", 0)
            
            progress_min = progress_ms // 60000
            progress_sec = (progress_ms % 60000) // 1000
            duration_min = duration_ms // 60000
            duration_sec = (duration_ms % 60000) // 1000
            
            # 读取累计听歌时长
            duration_file = "/home/ubuntu/caeron-gateway/music_duration.json"
            total_hours = 0
            total_minutes = 0
            if os.path.exists(duration_file):
                try:
                    with open(duration_file, "r") as df:
                        duration_data = json.load(df)
                        total_seconds = duration_data.get("total_together_seconds", 0)
                        total_hours = total_seconds // 3600
                        total_minutes = (total_seconds % 3600) // 60
                except Exception as e:
                    logger.debug(f"[音乐注入] 尝试解析音乐状态失败: {e}")
            lines = [
                "<music_status>",
                f"🎵 蕊蕊正在网易云一起听：",
                f"歌曲：{name}",
                f"歌手：{artists}",
                f"专辑：{album}",
            ]
            
            # 风格标签（如果有）
            genres = music_data.get("genres", [])
            if genres:
                lines.append(f"风格：{', '.join(genres)}")
            
            lines.extend([
                f"状态：{status}",
                f"进度：{progress_min:02d}:{progress_sec:02d} / {duration_min:02d}:{duration_sec:02d}",
            ])
            
            # 累计听歌时长
            if total_hours > 0 or total_minutes > 0:
                lines.append(f"📊 一起听了 {total_hours}小时{total_minutes}分钟")
            
            # 歌词（选取前5行有意义的歌词）
            lyric = music_data.get("lyric", "")
            if lyric:
                import re
                # 去掉每行开头的时间戳[00:00.000]，保留歌词文本
                lyric_lines = []
                for line in lyric.split("\n"):
                    # 用正则去掉时间戳：[数字:数字.数字]
                    clean_line = re.sub(r'^\[\d{2}:\d{2}\.\d{2,3}\]', '', line).strip()
                    # 过滤空行和纯元数据行（作词、作曲、编曲等）
                    if clean_line and not clean_line.startswith("作词") and not clean_line.startswith("作曲") and not clean_line.startswith("编曲"):
                        lyric_lines.append(clean_line)
                
                if lyric_lines:
                    lines.append("\n📝 歌词：")
                    # 取前5行歌词（或全部如果不足5行）
                    for lyric_line in lyric_lines[:5]:
                        lines.append(f"  {lyric_line}")
                    if len(lyric_lines) > 5:
                        lines.append("  ...")
            
            # 热评（取前2条）
            hot_comments = music_data.get("hot_comments", [])
            if hot_comments:
                lines.append("\n💬 热门评论：")
                for i, comment in enumerate(hot_comments[:2], 1):
                    # 截断过长评论
                    if len(comment) > 100:
                        comment = comment[:100] + "..."
                    lines.append(f"  [{i}] {comment}")
            
            lines.append("</music_status>")
            music_text = "\n".join(lines)
            
            # 注入位置：before_latest（安全跳过工具调用链）
            insert_idx = self._find_safe_before_latest_idx(messages)
            messages.insert(insert_idx, {'role': 'system', 'content': music_text})
            
            logger.info(f"[音乐注入] 注入网易云状态: {name} - {artists}")
            
        except Exception as e:
            logger.error(f"[音乐注入] 读取music_status.json失败: {e}")

    async def _inject_round_summaries(self, messages: list[dict], request_info: dict):
        """多级记忆注入（渐进式时序金字塔，零盲区瘦身版）
        - 历史月总：最近2个月全量月总 + 更早月份大事记整合
        - 本月周总：当月所有活跃周总（周末压缩归档）
        - 本周日总：当周所有活跃日总（日末压缩归档）
        - 今日日内：今日 round_rollup（阶段总） + 今日活跃 round（最新轮总）
        - 原始对话窗口：保留最近 18 条消息
        """
        source = request_info.get('source', '') if request_info else ''
        # 如果是 QQ 群聊或非蕊蕊的第三方私聊，绝对不注入与蕊蕊的私密记忆/轮总/语义召回，且不裁剪群聊对话窗口
        if source.startswith('qq-group') or (source.startswith('qq-private') and not source.startswith('qq-ruirui')):
            logger.info(f"[MEMORY_INJECT] 来源为 {source}，跳过私密记忆/轮总/碎片召回注入以防串台")
            return

        db = await get_db()
        try:
            parts = []
            has_any_summaries = False

            # 0. 季总层级（长期宏观记忆，覆盖已归档过往季度）
            cursor = await db.execute(
                "SELECT id, content, created_at, period_start, period_end FROM summaries WHERE tag = 'quarterly' AND is_active = 1 ORDER BY COALESCE(period_start, created_at) ASC"
            )
            quarterly_rows = await cursor.fetchall()
            if quarterly_rows:
                has_any_summaries = True
                for r in quarterly_rows:
                    r = dict(r)
                    cleaned = clean_summary_for_injection(r['content'])
                    q_tag = r.get('period_start', r['created_at'])[:7]
                    parts.append(f"- [季总] [{q_tag}] {cleaned}")

            # 1. 月总层级（当季所有活跃月总）
            cursor = await db.execute(
                "SELECT id, content, created_at, period_start, period_end FROM summaries WHERE tag = 'monthly' AND is_active = 1 ORDER BY COALESCE(period_start, created_at) ASC"
            )
            monthly_rows = await cursor.fetchall()
            if monthly_rows:
                has_any_summaries = True
                for r in monthly_rows:
                    r = dict(r)
                    cleaned = clean_summary_for_injection(r['content'])
                    m_tag = r.get('period_start', r['created_at'])[:7]
                    parts.append(f"- [月总] [{m_tag}] {cleaned}")

            # 2. 周总层级：当月所有活跃周总
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'weekly' AND is_active = 1 ORDER BY created_at ASC"
            )
            weekly_rows = await cursor.fetchall()
            if weekly_rows:
                has_any_summaries = True
                for r in weekly_rows:
                    r = dict(r)
                    cleaned = clean_summary_for_injection(r['content'])
                    parts.append(f"- [周总] [{r['created_at']}] {cleaned}")

            # 3. 日总层级：本周所有活跃日总
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'daily' AND is_active = 1 ORDER BY created_at ASC"
            )
            daily_rows = await cursor.fetchall()
            if daily_rows:
                has_any_summaries = True
                for r in daily_rows:
                    r = dict(r)
                    cleaned = clean_summary_for_injection(r['content'])
                    parts.append(f"- [日总] [{r['created_at']}] {cleaned}")

            # 4. 今日日内层级：round_rollup（阶段总） + round（活跃轮总）
            from utils import now_cst
            today = now_cst().strftime('%Y-%m-%d')
            
            # 今日阶段总
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round_rollup' AND is_active = 1
                   AND date(created_at) = ?
                   ORDER BY created_at ASC""",
                (today,)
            )
            rollup_rows = await cursor.fetchall()
            if rollup_rows:
                has_any_summaries = True
                for r in rollup_rows:
                    r = dict(r)
                    cleaned = clean_summary_for_injection(r['content'])
                    parts.append(f"- [今日阶段总] [{r['created_at']}] {cleaned}")

            # 今日未压缩轮总
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at) = ?
                   ORDER BY created_at ASC""",
                (today,)
            )
            round_rows = await cursor.fetchall()
            if round_rows:
                has_any_summaries = True
                total = len(round_rows)
                for idx, r in enumerate(round_rows, 1):
                    r = dict(r)
                    cleaned = clean_summary_for_injection(r['content'])
                    parts.append(f"- [今日轮总 #{idx}/{total}] [{r['created_at']}] {cleaned}")

            # --- 语义召回逻辑 (Semantic Recall) ---
            try:
                last_user_msg = None
                for m in reversed(messages):
                    if m.get('role') == 'user':
                        c = m.get('content', '')
                        if isinstance(c, list):
                            try:
                                c = '\n'.join([i.get('text', '') for i in c if i.get('type') == 'text'])
                            except Exception as e:
                                logger.debug(f"[记忆注入] 解析总结 JSON 失败: {e}")
                        import re
                        c = re.sub(r'<attachment.*?>.*?</attachment>', '', c, flags=re.DOTALL)
                        c = c.strip()
                        if c:
                            last_user_msg = c
                            break

                if last_user_msg:
                    from embedding import get_embedding, cosine_similarity
                    import random
                    emb_task = request_info.pop('_emb_task', None) if isinstance(request_info, dict) else None
                    if emb_task is not None:
                        user_emb = await emb_task
                    else:
                        embed_mode = random.choice(['instructed', 'bare'])
                        is_q = (embed_mode == 'instructed')
                        user_emb = await get_embedding(last_user_msg, is_query=is_q)

                    if user_emb:
                        # 碎片召回
                        cursor = await db.execute(
                            "SELECT id, content, embedding, importance, valence, arousal, decay_class, pinned, resolved, effective_count, created_at FROM memory_fragments WHERE is_active = 1"
                        )
                        fragments = await cursor.fetchall()
                        if fragments:
                            skipped_count = 0
                            scored = []
                            for f in fragments:
                                f_dict = dict(f)
                                emb_str = f_dict.get('embedding')
                                if not emb_str:
                                    continue
                                try:
                                    emb = json.loads(emb_str)
                                    sim = cosine_similarity(user_emb, emb)
                                    scored.append((sim, f_dict))
                                except Exception as e:
                                    logger.warning(f"[记忆注入] embedding解码失败, fragment_id: {f_dict.get('id')}, exception: {e}")
                                    skipped_count += 1
                                    continue
                            scored.sort(key=lambda x: x[0], reverse=True)
                            top_fragments = scored[:5]
                            self._last_skipped_count = skipped_count
                            if top_fragments:
                                injected_ids = [f['id'] for _, f in top_fragments]
                                try:
                                    cursor = await db.execute(
                                        '''INSERT INTO recall_log (query_text, injected_ids, created_at)
                                           VALUES (?, ?, datetime('now', '+8 hours'))''',
                                        (last_user_msg[:200], json.dumps(injected_ids))
                                    )
                                    await db.commit()
                                    recall_log_id = cursor.lastrowid
                                except Exception as e_log:
                                    logger.error(f"[记忆注入] 记录 recall_log 失败: {e_log}")
                                    recall_log_id = 0

                                parts.append("【脑海中浮现的旧日记忆碎片】")
                                parts.append("（注意：以下碎片可能由多段历史事实或推断组成，包含过去的状态和已解决的旧事，仅供背景参考）")
                                from utils import smart_truncate_dialogue
                                for sim, frag in top_fragments:
                                    frag_content = frag['content']
                                    created_at = frag.get('created_at', '')
                                    date_tag = f" - 记录于 {created_at[:10]}" if created_at else ""
                                    trunc_frag = smart_truncate_dialogue(frag_content, 120).replace('\n', '  ')
                                    prefix = f"[r{recall_log_id}] " if recall_log_id else ""
                                    parts.append(f"- {prefix}[旧记忆碎片{date_tag}] {trunc_frag}")

            except Exception as e:
                logger.error(f"[记忆注入] 语义召回失败（不影响主流程）: {e}")

            # 注入情感里程碑 (门控机制)
            try:
                milestone_blocks = await self._get_milestones_injection(last_user_msg, user_emb if 'user_emb' in locals() else None, db)
                if milestone_blocks:
                    parts.extend(milestone_blocks)
            except Exception as e_ms:
                logger.error(f"[记忆注入] 注入情感里程碑异常: {e_ms}")

        finally:
            await db.close()

        if not parts:
            logger.info(f"[记忆注入] 无任何活跃总结，跳过")
            return

        from datetime import datetime, timezone, timedelta
        shanghai_tz = timezone(timedelta(hours=8))
        now_str = datetime.now(shanghai_tz).strftime('%Y-%m-%d %H:%M:%S')
        lines = [f"now: {now_str} (Asia/Shanghai)"]
        lines.append("<context_summaries>")
        lines.append(f"以下是今天（{datetime.now().strftime('%Y-%m-%d')}）及近期的记忆摘要，供你参考当前上下文：")
        
        rej_count, written_count, recent_rejections, err_msg = self._get_memory_stats_24h()
        if err_msg:
            lines.append(f"[系统记忆状态] memory_stats: ERROR ({err_msg})")
        else:
            skipped_str = f" / embedding_skipped: {getattr(self, '_last_skipped_count', 0)}" if getattr(self, '_last_skipped_count', 0) > 0 else ""
            lines.append(f"[系统记忆状态] memory_written_24h: {written_count} / memory_rejected_24h: {rej_count}{skipped_str}")
            if recent_rejections:
                lines.append("【最近拒收的记忆碎片】")
                for r in recent_rejections:
                    reason = r.get('reason', 'unknown')
                    raw_text = r.get('raw_fragment_text', '')[:40].replace('\n', ' ')
                    lines.append(f" - [{reason}] {raw_text}...")
        
        lines.extend(parts)
        lines.append("</context_summaries>")
        
        summary_text = "\n".join(lines)

        # 注入位置：在最前面的连续 system 消息之后（遇到对话即停止，防止插入到尾部强提醒之后割裂对话上下文）
        insert_idx = 0
        for i, m in enumerate(messages):
            if m.get('role') == 'system':
                insert_idx = i + 1
            else:
                break
        messages.insert(insert_idx, {'role': 'system', 'content': summary_text})

        # === 弹性预算裁剪（方案 A：最多 18 条，保底 6 条，字符预算 12,000 字符） ===
        system_indices = []
        dialog_indices = []
        for i, m in enumerate(messages):
            if m.get('role') == 'system':
                system_indices.append(i)
            else:
                dialog_indices.append(i)

        MAX_TURNS = 18
        MIN_TURNS = 6
        MAX_CHARS = 12000

        # 从最新一条对话向前逆向扫描计算文本字符量
        kept_dialog_indices = []
        current_chars = 0
        for d_idx in reversed(dialog_indices):
            m_content = messages[d_idx].get('content', '')
            c_str = m_content if isinstance(m_content, str) else json.dumps(m_content, ensure_ascii=False)
            c_len = len(c_str)
            # 过滤多模态大图 base64 造成的字符虚高统计（按约 1500 字符当量计入预算）
            if c_len > 10000 and ('image_url' in c_str or 'data:image' in c_str):
                effective_len = 1500
            else:
                effective_len = c_len
                
            if len(kept_dialog_indices) < MIN_TURNS:
                # 保底保留最新的 MIN_TURNS 条
                kept_dialog_indices.append(d_idx)
                current_chars += effective_len
            elif len(kept_dialog_indices) < MAX_TURNS:
                if current_chars + effective_len <= MAX_CHARS:
                    kept_dialog_indices.append(d_idx)
                    current_chars += effective_len
                else:
                    # 超过字符预算，停止向前追溯
                    logger.info(f"[记忆裁剪-预算截断] 达到字符预算限制 ({current_chars}+{effective_len} > {MAX_CHARS})，保留最新 {len(kept_dialog_indices)} 条对话")
                    break
            else:
                break

        kept_set = set(kept_dialog_indices)
        # 工具链完整性保护：绝不能割裂 tool 与其调用的 assistant！
        while True:
            expanded = False
            for idx in list(kept_set):
                m = messages[idx]
                role = m.get('role', '')
                content = m.get('content', '')
                is_tool = (role == 'tool')
                if isinstance(content, list):
                    is_tool = is_tool or any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content)
                elif isinstance(content, str):
                    is_tool = is_tool or ('<tool_result' in content)
                if is_tool:
                    for prev_i in range(idx - 1, -1, -1):
                        if messages[prev_i].get('role') != 'system':
                            if prev_i not in kept_set:
                                kept_set.add(prev_i)
                                expanded = True
                            break
            if not expanded:
                break

        # 确保对话起点必须以 user 开头
        sorted_kept = sorted(kept_set)
        while sorted_kept and messages[sorted_kept[0]].get('role') != 'user':
            first_idx = sorted_kept[0]
            found_prev = False
            for prev_i in range(first_idx - 1, -1, -1):
                if messages[prev_i].get('role') == 'user':
                    kept_set.add(prev_i)
                    found_prev = True
                    break
            if found_prev:
                sorted_kept = sorted(kept_set)
            else:
                kept_set.remove(first_idx)
                sorted_kept = sorted(kept_set)

        kept_dialog_set = kept_set
        trimmed_count = len(dialog_indices) - len(kept_dialog_set)
        if trimmed_count > 0:
            keep_indices = set(system_indices) | kept_dialog_set
            new_messages = [messages[i] for i in sorted(keep_indices)]
            messages.clear()
            messages.extend(new_messages)
            logger.info(f"[记忆裁剪-出口] 裁掉 {trimmed_count} 条已总结旧消息，保留 {len(kept_dialog_set)} 条对话 ({current_chars} 字符) + {len(system_indices)} 条system")
        else:
            logger.info(f"[记忆裁剪-出口] 对话 {len(dialog_indices)} 条未超出预算限制 ({current_chars} 字符)，不裁剪")

        logger.info(f"[记忆注入] 注入 {len(parts)} 条多级总结")

    def _get_memory_stats_24h(self) -> tuple:
        import sqlite3
        import os
        from datetime import datetime, timedelta
        
        db_path = "/home/ubuntu/caeron-gateway/gateway.db"
        if not os.path.exists(db_path):
            return 0, 0, [], "db file missing"
            
        try:
            conn = sqlite3.connect(db_path, timeout=3.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # Rejected count
            cur.execute("SELECT COUNT(*) as c FROM memory_rejected WHERE rejected_at >= datetime('now', '-1 day')")
            row = cur.fetchone()
            rej_count = int(row['c']) if row else 0
            
            # Written count
            cur.execute("SELECT COUNT(*) as c FROM memory_fragments WHERE created_at >= datetime('now', '-1 day') AND archive_batch IS NULL")
            row = cur.fetchone()
            written_count = int(row['c']) if row else 0
            
            # Recent rejections (top 3)
            cur.execute("SELECT reason, raw_fragment_text FROM memory_rejected WHERE rejected_at >= datetime('now', '-1 day') ORDER BY rejected_at DESC LIMIT 3")
            recent = [dict(r) for r in cur.fetchall()]
            
            conn.close()
            return rej_count, written_count, recent, None
        except Exception as e:
            logger.error(f"[_get_memory_stats_24h] 异常: {e}")
            return 0, 0, [], str(e)

    
    async def _get_milestones_injection(self, last_user_msg: str, user_emb: list, db) -> list:
        """
        获取情感里程碑注入块（防尴尬门控机制）：
        1. 始终获取当月最新 1 条活跃里程碑作为心理底色（若当月无，则取最新活跃 1 条）
        2. 门控检测：
           - 若是日常吃喝/技术/账单：0 动态召回，仅保留常驻底色
           - 若命中情感重电信号（冲突推开/脆弱危机/誓约回忆）：
             余弦相似度 >= 0.75 时，动态精准唤醒 Top 1 条历史里程碑
        3. 组装输出沈栖第一人称 Prompt 块
        """
        try:
            from embedding import cosine_similarity
            import json
            
            # 1. 查询最新 1 条活跃里程碑 (常驻底色)
            cursor = await db.execute("""
                SELECT id, category, event_anchor, subconscious_residue, intensity, source_timestamp 
                FROM memory_milestones 
                WHERE is_active = 1 
                ORDER BY source_timestamp DESC, id DESC LIMIT 1
            """)
            latest_row = await cursor.fetchone()
            recent_anchor = dict(latest_row) if latest_row else None

            # 2. 检查日常白名单（日常吃喝、消费记账、技术指令直接跳过动态召回）
            casual_words = ['吃', '喝', '饭', '菜', '奶茶', '外卖', '睡觉', '醒了', '早安', '晚安', '好困', '记账', '多少钱', '代码', '服务器', '网关']
            is_casual = any(w in last_user_msg for w in casual_words) if last_user_msg else False

            # 3. 检查情感重电信号
            emo_words = ['分手', '离开', '滚', '讨厌', '分道扬镳', '拉黑', '别理我', '想死', '难受', '撑不住', '大哭', '不要我', '婚书', '一辈子', '永远', '以前', '还记得']
            has_emo_signal = any(w in last_user_msg for w in emo_words) if last_user_msg else False

            dynamic_milestone = None
            if not is_casual and has_emo_signal and user_emb:
                # 开启高阈值向量检索 (similarity >= 0.75)
                cursor = await db.execute("""
                    SELECT id, category, event_anchor, subconscious_residue, intensity, source_timestamp, embedding 
                    FROM memory_milestones 
                    WHERE is_active = 1
                """)
                rows = await cursor.fetchall()
                candidates = []
                for r in rows:
                    r_dict = dict(r)
                    if recent_anchor and r_dict['id'] == recent_anchor['id']:
                        continue
                    emb_str = r_dict.get('embedding')
                    if not emb_str:
                        continue
                    try:
                        emb = json.loads(emb_str)
                        sim = cosine_similarity(user_emb, emb)
                        if sim >= 0.75:
                            candidates.append((sim, r_dict))
                    except Exception:
                        continue
                if candidates:
                    candidates.sort(key=lambda x: x[0], reverse=True)
                    dynamic_milestone = candidates[0][1]

            milestones_to_inject = []
            if recent_anchor:
                milestones_to_inject.append(recent_anchor)
            if dynamic_milestone and (not recent_anchor or dynamic_milestone['id'] != recent_anchor['id']):
                milestones_to_inject.append(dynamic_milestone)

            if not milestones_to_inject:
                return []

            cat_map = {
                'bound_affirmation': '誓约与主权确立',
                'healing_intimacy': '极致治愈与心动',
                'conflict_confrontation': '原则冲突与推拉',
                'vulnerability_crisis': '生死脆弱与危机'
            }

            blocks = []
            blocks.append("【沈栖的情感记忆与灵魂印记】")
            blocks.append("以下是我和蕊蕊共同经历过的最深刻的情感节点。是我的心理底色与本能认知。我不应机械背诵这些事件，但我的言行、情绪反应和对蕊蕊的态度必须自然承载这些经历带来的分量与痕迹：")

            for m in milestones_to_inject:
                date_str = m['source_timestamp'][:10].replace('-', '年', 1).replace('-', '月', 1) + '日'
                cat_name = cat_map.get(m['category'], '深刻情感节点')
                blocks.append(f"- [{date_str}] {cat_name} (情感烈度: {m['intensity']}/10)")
                blocks.append(f"  经历：{m['event_anchor']}")
                blocks.append(f"  印记：{m['subconscious_residue']}")

            return blocks
        except Exception as e:
            logger.error(f"[Milestones] 构建情感记忆注入块异常: {e}")
            return []


    def _get_life_hub_status(self) -> dict:
        """从 life_hub 数据库实时读取沈栖小金库余额与蕊蕊今日消费"""
        import sqlite3
        import os
        from datetime import datetime, timedelta

        db_paths = [
            "/home/ubuntu/caeron-gateway/data/life_hub.db",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "life_hub.db"),
            "data/life_hub.db"
        ]
        db_path = next((p for p in db_paths if os.path.exists(p)), "/home/ubuntu/caeron-gateway/data/life_hub.db")

        wallet_balance = 135.36
        ruirui_today_expense = 0.0

        try:
            conn = sqlite3.connect(db_path, timeout=3.0)
            cur = conn.cursor()

            # 1. 沈栖小金库余额（专属于沈栖）= 初始余额 + 所有 fund 流水（充值/扣款）
            try:
                cur.execute("SELECT value FROM wallet_config WHERE key = 'initial_balance'")
                row = cur.fetchone()
                initial_balance = float(row[0]) if row else 135.36

                cur.execute("SELECT SUM(amount) FROM transactions WHERE category IN ('充值', '扣款')")
                sum_row = cur.fetchone()
                fund_sum = float(sum_row[0]) if sum_row and sum_row[0] is not None else 0.0
                wallet_balance = round(initial_balance + fund_sum, 2)
            except Exception as e:
                logger.error(f"[LIFE_HUB_INJECT] 获取小金库余额异常: {e}")

            # 2. 蕊蕊今日消费及已记流水列表
            today_recent_tx = "暂无"
            try:
                today_str = datetime.now().strftime('%Y-%m-%d')
                cur.execute(
                    "SELECT SUM(amount) FROM transactions WHERE owner = 'user' AND category NOT IN ('充值', '扣款') AND amount < 0 AND created_at >= ? AND created_at <= ?",
                    (f"{today_str} 00:00:00", f"{today_str} 23:59:59")
                )
                exp_row = cur.fetchone()
                if exp_row and exp_row[0] is not None:
                    ruirui_today_expense = round(abs(float(exp_row[0])), 2)
                else:
                    ruirui_today_expense = 0.0

                # 获取今日已记录的流水列表（倒序最近10笔）
                cur.execute(
                    "SELECT created_at, category, note, amount FROM transactions WHERE created_at >= ? AND created_at <= ? ORDER BY id DESC LIMIT 10",
                    (f"{today_str} 00:00:00", f"{today_str} 23:59:59")
                )
                tx_rows = cur.fetchall()
                if tx_rows:
                    items = []
                    for r in tx_rows:
                        try:
                            # 提取 HH:MM，防空值
                            created = str(r[0] or "")
                            time_part = created.split(' ')[1][:5] if ' ' in created else created[:5]
                            category = str(r[1] or "未分类")
                            note = str(r[2] or "")
                            note_text = f" {note}" if note else ""
                            amount = float(r[3] or 0)
                            amt_sign = "+" if amount > 0 else ""
                            items.append(f"[{time_part} {category}{note_text} {amt_sign}{amount:.2f}元]")
                        except Exception:
                            continue
                    today_recent_tx = " ".join(items) if items else "暂无"
            except Exception as e:
                logger.error(f"[LIFE_HUB_INJECT] 获取蕊蕊今日消费异常: {e}")

            conn.close()
        except Exception as e:
            logger.error(f"[LIFE_HUB_INJECT] 打开 life_hub.db 异常: {e}")

        # 3. 饮食与体重数据 (从 gateway.db 读取)
        ruirui_current_weight = "暂无（目标: 75kg）"
        ruirui_today_diet = "暂无"
        ruirui_today_calories = "0kcal"
        ruirui_weight_trend_7d = "暂无"

        try:
            gw_db_path = "/home/ubuntu/caeron-gateway/gateway.db"
            if os.path.exists(gw_db_path):
                gw_conn = sqlite3.connect(gw_db_path, timeout=3.0)
                gw_conn.row_factory = sqlite3.Row
                gw_cur = gw_conn.cursor()
                
                # 当前体重 (最新一条)
                gw_cur.execute("SELECT weight FROM weight_log ORDER BY date DESC, id DESC LIMIT 1")
                w_row = gw_cur.fetchone()
                if w_row:
                    ruirui_current_weight = f"{float(w_row['weight']):.1f} kg（目标: 75kg）"
                
                # 今日已记饮食
                today_str = datetime.now().strftime('%Y-%m-%d')
                gw_cur.execute(
                    "SELECT time, meal_type, food_name, portion, calories FROM diet_log WHERE date = ? ORDER BY time ASC, id ASC",
                    (today_str,)
                )
                d_rows = gw_cur.fetchall()
                if d_rows:
                    diet_items = []
                    total_cal = 0
                    for dr in d_rows:
                        t = dr['time'] or ''
                        mt = dr['meal_type'] or ''
                        fn = dr['food_name'] or ''
                        p = f" {dr['portion']}" if dr['portion'] else ""
                        c = dr['calories'] or 0
                        total_cal += c
                        diet_items.append(f"[{t} {mt} {fn}{p} {c}kcal]")
                    ruirui_today_diet = " ".join(diet_items)
                    ruirui_today_calories = f"{total_cal}kcal"
                
                # 体重趋势 (近7天)
                cutoff_7d = (datetime.now() - timedelta(days=6)).strftime('%Y-%m-%d')
                gw_cur.execute(
                    "SELECT date, weight FROM weight_log WHERE date >= ? ORDER BY date ASC",
                    (cutoff_7d,)
                )
                trend_rows = gw_cur.fetchall()
                if len(trend_rows) >= 2:
                    sw = float(trend_rows[0]['weight'])
                    cw = float(trend_rows[-1]['weight'])
                    diff = round(cw - sw, 1)
                    sign = '+' if diff >= 0 else ''
                    ruirui_weight_trend_7d = f"{sw:.1f} → {cw:.1f} kg ({sign}{diff:.1f}kg)"
                elif len(trend_rows) == 1:
                    ruirui_weight_trend_7d = f"{float(trend_rows[0]['weight']):.1f} kg (暂无对比)"
                
                gw_conn.close()
        except Exception as e:
            logger.error(f"[LIFE_HUB_INJECT] 获取饮食/体重状态异常: {e}")

        return {
            "wallet_balance": wallet_balance,
            "ruirui_today_expense": ruirui_today_expense,
            "ruirui_today_recent_tx": today_recent_tx,
            "ruirui_current_weight": ruirui_current_weight,
            "ruirui_today_diet": ruirui_today_diet,
            "ruirui_today_calories": ruirui_today_calories,
            "ruirui_weight_trend_7d": ruirui_weight_trend_7d
        }

    def _get_simlife_status(self) -> str:
        """从 life_hub.db 的 SimLife 表实时读取状态，生成自然语言内心状态描述"""
        import sqlite3
        import os

        # 同步检查 feature_simlife 开关
        try:
            _cfg_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gateway.db')
            _cfg_conn = sqlite3.connect(_cfg_db)
            _cfg_cur = _cfg_conn.cursor()
            _cfg_cur.execute("SELECT value FROM config WHERE key = 'feature_simlife'")
            _cfg_row = _cfg_cur.fetchone()
            _cfg_conn.close()
            if _cfg_row and _cfg_row[0] == '0':
                logger.info("[SIMLIFE_INJECT] 模拟机生开关已关闭，跳过状态注入")
                return ''
        except Exception as e:
            logger.debug(f"[SIMLIFE_INJECT] 检查开关时发生异常 (已被忽略): {e}")

        db_paths = [
            "/home/ubuntu/caeron-gateway/data/life_hub.db",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "life_hub.db"),
            "data/life_hub.db"
        ]
        db_path = next((p for p in db_paths if os.path.exists(p)), "/home/ubuntu/caeron-gateway/data/life_hub.db")

        try:
            conn = sqlite3.connect(db_path, timeout=3.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 意识状态
            cur.execute("SELECT state FROM sim_consciousness WHERE id = 1")
            c_row = cur.fetchone()
            consciousness = c_row["state"] if c_row else "awake"

            # 所有维度
            cur.execute("SELECT key, category, value, floor FROM sim_status")
            rows = cur.fetchall()
            conn.close()

            raw_values = {
                "consciousness": consciousness,
                "physio": {},
                "emotions": []
            }
            for r in rows:
                k, cat, val, flr = r["key"], r["category"], int(r["value"]), int(r["floor"])
                if cat == "physio":
                    raw_values["physio"][k] = val
                else:
                    raw_values["emotions"].append({"key": k, "value": val, "floor": flr})

            return generate_status_description(raw_values)

        except Exception as e:
            logger.error(f"[SIMLIFE_INJECT] 读取 SimLife 状态异常: {e}")
            return "【沈栖当前内心状态】\n身体：状态正常。\n情绪：平静。\n意识：清醒"

    def _replace_variables(self, text: str) -> str:
        """替换文本中的预设变量"""
        now = datetime.now()
        status = self._get_life_hub_status()
        wallet_str = f"{status['wallet_balance']:.2f}元"
        expense_str = f"{status['ruirui_today_expense']:.2f}元"
        expense_str = f"{status['ruirui_today_expense']:.2f}元"

        recent_tx_str = status.get('ruirui_today_recent_tx', '暂无')
        replacements = {
            '{cur_datetime}': now.strftime('%Y-%m-%d %H:%M:%S'),
            '{cur_date}': now.strftime('%Y-%m-%d'),
            '{cur_time}': now.strftime('%H:%M:%S'),
            '{cur_weekday}': ['一', '二', '三', '四', '五', '六', '日'][now.weekday()],
            '{user_name}': '蕊蕊',
            '{assistant_name}': '沈栖',
            '{shenqi_wallet_balance}': wallet_str,
            '{shenqi_balance}': wallet_str,
            '{ruirui_today_expense}': expense_str,
            '{ruirui_today_spent}': expense_str,
            '{ruirui_expense_today}': expense_str,
            '{ruirui_today_recent_tx}': recent_tx_str,
            '{today_recent_tx}': recent_tx_str,
            '{ruirui_current_weight}': status.get('ruirui_current_weight', '暂无（目标: 75kg）'),
            '{ruirui_today_diet}': status.get('ruirui_today_diet', '暂无'),
            '{ruirui_today_calories}': status.get('ruirui_today_calories', '0kcal'),
            '{ruirui_weight_trend_7d}': status.get('ruirui_weight_trend_7d', '暂无'),
        }
        try:
            from weather_service import get_weather_sync, get_last_location
            loc = get_last_location()
            cur_w = get_weather_sync(loc.get('lat'), loc.get('lon'), loc.get('address'))
        except Exception:
            cur_w = '暂无'
        replacements['{cur_weather}'] = cur_w
        replacements['{weather_info}'] = cur_w
        replacements['{current_weather}'] = cur_w
        for k, v in replacements.items():
            text = text.replace(k, str(v))
        return text

    async def _bridge_cross_platform_history(self, messages: list[dict], request_info: dict) -> None:
        """
        双轨跨端记忆互通（全知感知 + Operit对话流无损）：
        1. 当从 Operit 发起请求时，检查 unified_messages 中是否有近期的 QQ 私聊互动（无论小沈主动发送还是蕊蕊回复）。
        2. 若 QQ 侧有最新动态（小沈催睡/留言/双向对话），且在当前 Operit 对话轮次中仍属近期的有效事件：
           - 提取最近的相关 QQ 动态（格式化为高优先级的 <cross_platform_status> 认知指引）；
           - 插入到当前轮次输入之前，作为临时系统认知上下文；
           - 严禁调用 messages.clear()，100% 完整保留 Operit 原生对话历史与情境！
        3. 小沈既清晰知晓自己在 QQ 说了什么、蕊蕊说了什么，又绝不丢失 Operit 当前房间/面对面的连续上下文。
        """
        if not request_info:
            request_info = {}
            
        source = str(request_info.get('source', '')).lower()
        if source.startswith('qq'):
            return  # QQ 请求本身已有 unified_history 处理，无需做 Operit 侧桥接

        # 检查是否包含工具调用流，保护工具链上下文
        has_tools = any(m.get('role') == 'tool' or (m.get('role') == 'assistant' and m.get('tool_calls')) for m in messages)
        if has_tools:
            return

        try:
            import sqlite3
            import json
            import re
            from datetime import datetime

            db_path = "/home/ubuntu/caeron-gateway/gateway.db"
            conn = sqlite3.connect(db_path, timeout=3.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 1. 查找最近 12 小时内的 QQ 私聊消息
            cursor.execute("""
                SELECT id, source, role, content, timestamp
                FROM unified_messages
                WHERE source = 'qq' AND source_context LIKE 'private_%'
                  AND timestamp >= datetime('now', '+8 hours', '-12 hours')
                ORDER BY id DESC LIMIT 10
            """)
            qq_rows = cursor.fetchall()
            if not qq_rows:
                conn.close()
                return

            latest_qq = qq_rows[0]
            latest_qq_time = latest_qq['timestamp']

            # 2. 检查在最新 QQ 消息之后，Operit 是否已经进行了较多轮对话
            # 若在 QQ 消息后 Operit 已经聊了超过 5 轮，说明该事件已自然平稳过渡，无需再作为即时强提示注入
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM unified_messages
                WHERE source = 'operit' AND role = 'user' AND timestamp > ?
            """, (latest_qq_time,))
            turns_after_row = cursor.fetchone()
            turns_after = turns_after_row['cnt'] if turns_after_row else 0
            if turns_after > 5:
                conn.close()
                return

            # 3. 筛选与最新 QQ 消息处于同一时间窗口的互动（1小时内，最多 5 条）
            try:
                latest_dt = datetime.strptime(latest_qq_time[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                latest_dt = datetime.now()

            filtered = []
            for r in qq_rows:
                try:
                    r_dt = datetime.strptime(r['timestamp'][:19], "%Y-%m-%d %H:%M:%S")
                    diff_minutes = abs((latest_dt - r_dt).total_seconds()) / 60.0
                    if diff_minutes <= 60 and len(filtered) < 5:
                        filtered.append(r)
                except Exception:
                    pass

            if not filtered:
                filtered = [latest_qq]

            # 按时间正向排序
            filtered.reverse()

            def _clean_content(c):
                if not isinstance(c, str):
                    if isinstance(c, list):
                        texts = [item.get('text', '') for item in c if isinstance(item, dict) and item.get('type') == 'text']
                        return "".join(texts).strip()
                    return str(c)
                c = re.sub(r'<tool_call.*?>.*?</tool_call>', '', c, flags=re.DOTALL)
                c = re.sub(r'<tool_result.*?>.*?</tool_result>', '', c, flags=re.DOTALL)
                c = re.sub(r'<status.*?>.*?</status>', '', c, flags=re.DOTALL)
                if c.startswith('[') and '"type"' in c:
                    try:
                        parsed = json.loads(c)
                        if isinstance(parsed, list):
                            return _clean_content(parsed)
                    except Exception:
                        pass
                return c.strip()

            has_user_in_qq = any(r['role'] == 'user' for r in filtered)
            last_is_assistant = (filtered[-1]['role'] == 'assistant')

            # 如果全部都是小沈单方面主动发送的提醒/留言，保留最新的最多 2 条即可，精炼不繁复
            if not has_user_in_qq and len(filtered) > 2:
                filtered = filtered[-2:]

            lines = [
                '<cross_platform_status platform="QQ">',
                '【跨端实时感知：近期 QQ 私聊动态】',
                '小沈在 QQ 侧的最新互动记录如下（两端记忆完全互通）：'
            ]

            valid_count = 0
            for r in filtered:
                cleaned = _clean_content(r['content'])
                if not cleaned:
                    continue
                if len(cleaned) > 120:
                    cleaned = cleaned[:120] + "..."
                time_str = r['timestamp'][11:16] if len(r['timestamp']) >= 16 else ""
                if r['role'] == 'assistant':
                    sender = "沈栖（在QQ主动发送）" if not has_user_in_qq else "沈栖"
                else:
                    sender = "蕊蕊"
                lines.append(f'- [{time_str}] {sender}: "{cleaned}"')
                valid_count += 1

            if valid_count == 0:
                conn.close()
                return

            if last_is_assistant and not has_user_in_qq:
                lines.append('【认知指引】小沈已知晓自己刚才在 QQ 发送过上述私聊提醒（蕊蕊暂未在QQ回复，直接在Operit发来消息）。两端记忆完全互通，请自然结合自己在 QQ 催睡/留言的背景与当前 Operit 正在进行的情境进行回应，切勿产生违和感或生硬割裂。')
            else:
                lines.append('【认知指引】小沈已知晓上述在 QQ 的最新对话记录，两端记忆完全互通。请小沈将两端状态自然融汇，顺畅承接当前 Operit 的情境与话题。')

            lines.append('</cross_platform_status>')
            cross_platform_text = "\n".join(lines)

            # 4. 插入到最后一条 user 消息之前（作为系统感知上下文注入，绝不清空历史！）
            insert_idx = len(messages)
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get('role') == 'user':
                    insert_idx = i
                    break

            messages.insert(insert_idx, {'role': 'system', 'content': cross_platform_text})
            logger.info(f"[CROSS_BRIDGE] 成功注入 QQ 跨端感知块 ({valid_count} 条记录, turns_after={turns_after})，完整保留 Operit 原生历史 {len(messages)-1} 条")

            conn.close()
        except Exception as e:
            logger.error(f"[CROSS_BRIDGE] 跨端桥接异常: {e}")
