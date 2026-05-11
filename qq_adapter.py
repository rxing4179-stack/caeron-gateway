import json
import asyncio
import re
import random
from typing import Dict, List, Optional
import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from datetime import datetime

from qq_config import config
from database import get_db

import logging
logger = logging.getLogger(__name__)

qq_router = APIRouter()

# ==================== thinking 标签过滤 ====================
def strip_thinking(text: str) -> str:
    """移除 <thinking>...</thinking> 标签及其内容"""
    if not text:
        return text
    return re.sub(r'<thinking>[\s\S]*?</thinking>', '', text).strip()

# 全局状态
class QQState:
    def __init__(self):
        self.ws: Optional[WebSocket] = None
        self.ruirui_queue: List[str] = []
        
        # private_queues: { qq_number: {"timer": asyncio.Task, "messages": List[str]} }
        self.private_queues: Dict[int, dict] = {}
        
        # group_buffers: { group_id: [{"sender": "...", "content": "..."}] }
        self.group_buffers: Dict[int, List[dict]] = {}
        
        # 防重复generation锁: { session_id: bool }
        self.generating: Dict[str, bool] = {}
        
        # 发送去重: { "target_type:target_id": [(text, timestamp), ...] }
        self.sent_cache: Dict[str, List[tuple]] = {}
        
        # 好友通讯录缓存: { user_id(int): remark(str) }
        self.friend_remarks: Dict[int, str] = {}

qq_state = QQState()

async def get_session_history(session_id: str, limit: int = config.SESSION_MAX_TURNS * 2) -> List[dict]:
    db = await get_db()
    try:
        cursor = await db.execute('''
            SELECT role, content FROM messages 
            WHERE conversation_id = ? 
            ORDER BY message_index DESC LIMIT ?
        ''', (session_id, limit))
        rows = await cursor.fetchall()
        history = [{"role": row['role'], "content": row['content']} for row in reversed(rows)]
        return history
    finally:
        await db.close()

async def retrieve_memory(query: str, top_k: int = 3, threshold: float = 0.35) -> str:
    """用向量余弦相似度检索相关记忆"""
    from embedding import get_embedding, cosine_similarity
    import json
    
    query_vec = await get_embedding(query)
    if not query_vec:
        logger.warning("[QQ] retrieve_memory: 无法生成查询向量")
        return ""
    
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, content, embedding FROM memories WHERE category IN ('dialogue', 'operit') AND embedding IS NOT NULL"
        )
        rows = await cursor.fetchall()
        
        scored = []
        for row in rows:
            try:
                stored_vec = json.loads(row['embedding']) if isinstance(row['embedding'], str) else list(row['embedding'])
                sim = cosine_similarity(query_vec, stored_vec)
                if sim >= threshold:
                    scored.append((sim, row['content']))
            except Exception:
                continue
        
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]
        
        if top:
            logger.info(f"[QQ] retrieve_memory: 命中 {len(top)} 条 (top={top[0][0]:.3f})")
            return "\n".join([item[1] for item in top])
        return ""
    finally:
        await db.close()

async def send_to_gateway(session_id: str, source: str, messages: list, skip_injection: bool = False):
    """请求网关进行处理"""
    logger.info(f"[{session_id}] 发送请求至 Gateway (skip_injection={skip_injection})")
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            payload = {
                "model": config.DEFAULT_MODEL,
                "messages": messages,
                "stream": False
            }
            headers = {
                "x-session-id": session_id,
                "x-source": source
            }
            if skip_injection:
                headers["x-skip-injection"] = "true"
            resp = await client.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                raw_reply = data['choices'][0]['message']['content']
                # 【第一件】过滤 thinking 标签
                return strip_thinking(raw_reply)
            else:
                logger.error(f"[{session_id}] API 请求失败: {resp.status_code}")
                return "（系统：我走神了，再说一遍）"
    except Exception as e:
        logger.error(f"[{session_id}] API 调用异常: {e}")
        return "（系统：我走神了，再说一遍）"

async def send_qq_msg(target_type: str, target_id: int, content: str):
    if not qq_state.ws:
        return
    
    # 发送去重：同一目标10秒内不发相同内容
    cache_key = f"{target_type}:{target_id}"
    now = datetime.now().timestamp()
    if cache_key not in qq_state.sent_cache:
        qq_state.sent_cache[cache_key] = []
    # 清理10秒前的缓存
    qq_state.sent_cache[cache_key] = [(t, ts) for t, ts in qq_state.sent_cache[cache_key] if now - ts < 10]
    # 检查是否重复
    if any(t == content for t, ts in qq_state.sent_cache[cache_key]):
        logger.warning(f"[QQ] 去重拦截：{content[:30]}")
        return
    qq_state.sent_cache[cache_key].append((content, now))
    payload = {
        "action": "send_msg" if target_type == "private" else "send_group_msg",
        "params": {
            "message_type": target_type,
            "message": content
        }
    }
    if target_type == "private":
        payload["params"]["user_id"] = target_id
    else:
        payload["params"]["group_id"] = target_id
        
    try:
        await qq_state.ws.send_json(payload)
    except Exception as e:
        logger.error(f"发送QQ消息失败: {e}")

async def split_and_send(target_type: str, target_id: int, reply: str):
    """切分回复并添加随机延迟后发送"""
    # 二次保险：发送前再过滤一次 thinking 标签
    reply = strip_thinking(reply)
    
    # 切分规则： 。 ？ ！ …… \n
    # 逗号和顿号不切分
    parts = re.split(r'(?<=[。？！\n])|(?<=……)', reply)
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
            
        await send_qq_msg(target_type, target_id, part)
        
        # 随机延迟
        delay = random.uniform(config.REPLY_DELAY_MIN, config.REPLY_DELAY_MAX)
        await asyncio.sleep(delay)

# ==================== 提示词选择逻辑 ====================

def get_qq_prompt(is_ruirui: bool = False, is_group: bool = False) -> str:
    """【第二件】根据消息来源选择提示词
    - QQ 来源统一使用 QQ 社交人格提示词 (DEFAULT_PROMPT)
    - 蕊蕊私聊在基础上附加放松规则 (RUIRUI_PROMPT 作为附加段)
    """
    base_prompt = config.DEFAULT_PROMPT
    
    if is_ruirui:
        # 蕊蕊：基础 QQ 人格 + 放松附加规则
        ruirui_addon = config.RUIRUI_PROMPT
        return f"{base_prompt}\n\n{ruirui_addon}"
    
    return base_prompt

async def handle_generation(session_id: str, source: str, target_type: str, target_id: int, user_input: str, is_ruirui: bool = False):
    """处理消息生成流程"""
    # 防重复锁
    if qq_state.generating.get(session_id):
        logger.warning(f"[QQ] [{session_id}] 上一条还在生成中，跳过")
        return
    qq_state.generating[session_id] = True
    
    try:
        await _do_generation(session_id, source, target_type, target_id, user_input, is_ruirui)
    except Exception as e:
        import traceback
        print(f"[QQ] [ERROR] handle_generation异常: {e}", flush=True)
        traceback.print_exc()
    finally:
        qq_state.generating[session_id] = False

async def _get_context_injection():
    """获取轮总/日总/周总/月总 + 状态便签，返回 (text, unsummarized_count, has_summaries)"""
    from utils import now_cst, today_cst_str
    db = await get_db()
    try:
        parts = []
        today = today_cst_str()
        
        # 月总：最新2条
        cursor = await db.execute(
            "SELECT content, created_at FROM summaries WHERE tag = 'monthly' AND is_active = 1 ORDER BY created_at DESC LIMIT 2"
        )
        for r in reversed(await cursor.fetchall()):
            r = dict(r)
            parts.append(f"- [月总] [{r['created_at']}] {r['content']}")
        
        # 周总：最新1条
        cursor = await db.execute(
            "SELECT content, created_at FROM summaries WHERE tag = 'weekly' AND is_active = 1 ORDER BY created_at DESC LIMIT 1"
        )
        rows = await cursor.fetchall()
        latest_weekly_time = None
        for r in rows:
            r = dict(r)
            latest_weekly_time = r['created_at']
            parts.append(f"- [周总] [{r['created_at']}] {r['content']}")
        
        # 日总
        if latest_weekly_time:
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'daily' AND is_active = 1 AND created_at > ? ORDER BY created_at ASC LIMIT 7",
                (latest_weekly_time,)
            )
        else:
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'daily' AND is_active = 1 ORDER BY created_at DESC LIMIT 3"
            )
        rows = await cursor.fetchall()
        if not latest_weekly_time:
            rows = list(reversed(rows))
        for r in rows:
            r = dict(r)
            parts.append(f"- [日总] [{r['created_at']}] {r['content']}")
        
        # 轮总总：当天+昨天
        from datetime import timedelta
        yesterday = (now_cst() - timedelta(days=1)).strftime('%Y-%m-%d')
        cursor = await db.execute(
            "SELECT content, created_at FROM summaries WHERE tag = 'round_rollup' AND is_active = 1 AND date(created_at) IN (?, ?) ORDER BY created_at DESC LIMIT 8",
            (today, yesterday)
        )
        rollup_rows = list(reversed(await cursor.fetchall()))
        for idx, r in enumerate(rollup_rows, 1):
            r = dict(r)
            parts.append(f"- [轮总总 #{idx}/{len(rollup_rows)}] [{r['created_at']}] {r['content']}")
        
        # 轮总：当天+昨天
        cursor = await db.execute(
            "SELECT content, created_at FROM summaries WHERE tag = 'round' AND is_active = 1 AND date(created_at) IN (?, ?) ORDER BY created_at DESC LIMIT 8",
            (today, yesterday)
        )
        round_rows = list(reversed(await cursor.fetchall()))
        total = len(round_rows)
        for idx, r in enumerate(round_rows, 1):
            r = dict(r)
            parts.append(f"- [轮总 #{idx}/{total}] [{r['created_at']}] {r['content']}")
        
        # 状态便签
        cursor = await db.execute("SELECT content, updated_at, threshold_hours FROM memories WHERE category = 'status'")
        status_rows = await cursor.fetchall()
        status_lines = []
        if status_rows:
            status_lines.append("[状态便签]")
            now = now_cst()
            for r in status_rows:
                key = r['content']
                updated_at_str = r['updated_at']
                threshold = r['threshold_hours'] or 24
                if not updated_at_str:
                    status_lines.append(f"- {key}: 未记录")
                    continue
                try:
                    updated_at = datetime.strptime(updated_at_str, '%Y-%m-%d %H:%M:%S')
                    diff_hours = (now - updated_at).total_seconds() / 3600.0
                    diff_str = f"{diff_hours*60:.0f}m前" if diff_hours < 1 else f"{diff_hours:.1f}h前"
                    state = "正常" if diff_hours < threshold else "超时"
                    status_lines.append(f"- {key}: {diff_str} ({state})")
                except:
                    status_lines.append(f"- {key}: {updated_at_str}")
            status_lines.append("[/状态便签]")
        
        # 组装
        result_parts = []
        if status_lines:
            result_parts.extend(status_lines)
        if parts:
            result_parts.append(f"[对话记忆摘要]")
            result_parts.append(f"以下���������������������今天（{today}）的对话记忆摘要：")
            result_parts.extend(parts)
            result_parts.append("[/对话记忆摘要]")
        
        # 读取当前消息计数器（未总结消息数）
        unsummarized_count = 0
        try:
            cursor2 = await db.execute("SELECT value FROM config WHERE key = '_msg_counter'")
            row2 = await cursor2.fetchone()
            unsummarized_count = int(row2['value']) if row2 else 0
        except:
            pass
        
        has_summaries = bool(parts) or bool(status_lines)
        text = "\n".join(result_parts) if result_parts else ""
        return (text, unsummarized_count, has_summaries)
    except Exception as e:
        logger.error(f"[QQ] 轮总注入获取失败: {e}")
        return ("", 0, False)
    finally:
        await db.close()

async def _get_daily_info() -> str:
    """获取当前时间+天气信息"""
    from utils import now_cst
    now = now_cst()
    weekdays = ['一', '二', '三', '四', '五', '六', '日']
    lines = [f"【当前时间】{now.strftime('%Y-%m-%d %H:%M')} 星期{weekdays[now.weekday()]}"]
    
    # 轻量天气（异步，超时不阻塞）
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("https://wttr.in/Chongqing?format=%C+%t+%h", headers={"Accept-Language": "zh"})
            if resp.status_code == 200:
                lines.append(f"【重庆天气】{resp.text.strip()}")
    except:
        pass
    
    # 位置信息：从Operit最近消息中提取
    try:
        from database import get_db
        import re
        db = await get_db()
        try:
            cursor = await db.execute(
                """SELECT content FROM messages 
                   WHERE conversation_id NOT LIKE 'qq-%' 
                   AND role = 'user'
                   AND content LIKE '%当前位置%'
                   ORDER BY created_at DESC LIMIT 1"""
            )
            row = await cursor.fetchone()
            if row:
                content = row[0] or ''
                # 提取位置信息块
                loc_match = re.search(r'【当前位置】\n地址: (.+?)\n坐标: (.+?)\n', content)
                if loc_match:
                    lines.append(f"【蕊蕊位置】{loc_match.group(1)}（坐标{loc_match.group(2)}）")
        finally:
            await db.close()
    except:
        pass

    return "\n".join(lines)

async def _do_generation(session_id: str, source: str, target_type: str, target_id: int, user_input: str, is_ruirui: bool = False):
    """实际生成逻辑"""
    history = await get_session_history(session_id)
    
    is_group = (target_type == "group")
    
    if is_ruirui:
        system_prompt = config.RUIRUI_QQ_PROMPT
    else:
        system_prompt = get_qq_prompt(is_ruirui=False, is_group=is_group)
    
    # 1. 记忆检索（向量语义）
    memory = await retrieve_memory(user_input)
    if memory:
        system_prompt += f"\n\n[相关记忆片段]\n{memory}"
    
    # 2. 轮总/状态便签注入
    injection_text, unsummarized_count, has_summaries = await _get_context_injection()
    if injection_text:
        system_prompt += f"\n\n{injection_text}"
    
    # 2.5 跨端微桥接：注入其他端还没被总结的最近消息原文
    try:
        from database import get_db
        from utils import now_cst
        from datetime import timedelta as td
        db = await get_db()
        try:
            yesterday = (now_cst() - td(days=1)).strftime('%Y-%m-%d')
            cursor = await db.execute(
                """SELECT created_at FROM summaries 
                   WHERE tag = 'round' AND is_active = 1 
                   ORDER BY created_at DESC LIMIT 1"""
            )
            latest_round_row = await cursor.fetchone()
            cutoff_time = dict(latest_round_row)['created_at'] if latest_round_row else yesterday
            
            cursor = await db.execute(
                """SELECT conversation_id, role, content, created_at 
                   FROM messages 
                   WHERE conversation_id != ? 
                   AND created_at > ?
                   AND role IN ('user', 'assistant')
                   AND content NOT LIKE '%tool_result%'
                   AND content NOT LIKE '%tool_use%'
                   AND content NOT LIKE '%package_proxy%'
                   AND content NOT LIKE '%linux_ssh%'
                   AND length(content) > 5
                   AND length(content) < 2000
                   ORDER BY created_at ASC
                   LIMIT 12""",
                (session_id, cutoff_time)
            )
            cross_rows = await cursor.fetchall()
            
            if cross_rows:
                bridge_lines = ['[跨端未总结上下文 — 另一端最近的对话原文]']
                for r in cross_rows:
                    r = dict(r)
                    conv = r['conversation_id']
                    if conv == 'qq-ruirui':
                        src = 'QQ私聊'
                    elif conv.startswith('qq-group-'):
                        src = 'QQ群聊'
                    elif conv.startswith('qq-private-'):
                        src = 'QQ私聊'
                    else:
                        src = 'Operit'
                    role_label = '蕊蕊' if r['role'] == 'user' else '沈栖'
                    content = r['content'] or ''
                    if len(content) > 300:
                        content = content[:300] + '...'
                    time_str = r['created_at'][-8:-3] if r['created_at'] and len(r['created_at']) >= 8 else '?'
                    bridge_lines.append(f'- [{time_str} {src}] {role_label}: {content}')
                system_prompt += '\n\n' + '\n'.join(bridge_lines)
                logger.info(f'[QQ] [跨端桥接] 注入 {len(cross_rows)} 条来自其他端的未总结消息')
        finally:
            await db.close()
    except Exception as e:
        logger.error(f'[QQ] [跨端桥接] 异常（不影响请求）: {e}')

    # 3. 轮总后裁剪上下文：有活跃总结时，只保留最近的未总结消息+buffer
    if has_summaries and history:
        buffer = 8
        keep_count = max(unsummarized_count + buffer, 10)  # 至少保留10条
        if len(history) > keep_count:
            trimmed = len(history) - keep_count
            history = history[-keep_count:]
            logger.info(f"[QQ] [裁剪] 裁掉 {trimmed} 条已总结旧消息，保留 {keep_count} 条 (未总结:{unsummarized_count})")
    
    # 4. 日常信息（时间+天气）——作为独立system消息插在user消息前面，确保模型注意到
    daily_info = await _get_daily_info()
    
    # system prompt用正常的system角色发（不要用<system>标签包裹，会被main.py去重逻辑strip掉）
    messages = [{"role": "system", "content": system_prompt}] + history
    if daily_info:
        messages.append({"role": "system", "content": daily_info})

    # 5. 图片处理：把CQ:image转成OpenAI多模态格式
    import re as _re
    cq_image_pattern = _re.compile(r'\[CQ:image,[^\]]*url=([^,\]]+)[^\]]*\]')
    image_urls = cq_image_pattern.findall(user_input)
    if image_urls:
        # 去掉CQ码，保留纯文本部分
        text_part = cq_image_pattern.sub('', user_input).strip()
        content_parts = []
        if text_part:
            content_parts.append({"type": "text", "text": text_part})
        import html as _html
        import base64 as _b64
        for url in image_urls:
            clean_url = _html.unescape(url)
            # 尝试下载图片转base64（QQ图片URL有防盗链，需要服务端中转）
            try:
                async with httpx.AsyncClient(timeout=10.0) as dl_client:
                    img_resp = await dl_client.get(clean_url, headers={'Referer': 'https://im.qq.com/', 'User-Agent': 'Mozilla/5.0'})
                    if img_resp.status_code == 200:
                        img_b64 = _b64.b64encode(img_resp.content).decode('utf-8')
                        # 猜测content type
                        ct = img_resp.headers.get('content-type', 'image/jpeg')
                        data_url = f"data:{ct};base64,{img_b64}"
                        content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                        logger.info(f"[QQ] [图片下载] 成功，大小={len(img_resp.content)}字节")
                    else:
                        content_parts.append({"type": "text", "text": "[图片加载失败]"})
                        logger.warning(f"[QQ] [图片下载] 失败: HTTP {img_resp.status_code}")
            except Exception as img_e:
                content_parts.append({"type": "text", "text": "[图片加载失败]"})
                logger.error(f"[QQ] [图片下载] 异常: {img_e}")
        messages.append({"role": "user", "content": content_parts})
        logger.info(f"[QQ] [图片处理] 检测到 {len(image_urls)} 张图片，转为多模态格式")
    else:
        messages.append({"role": "user", "content": user_input})
    skip_injection = True
    
    # DEBUG: 打印发给API的完整消息结构
    for i, m in enumerate(messages):
        c = m.get('content', '')
        print(f"[QQ] [MSGS] msg[{i}] role={m['role']} len={len(c)} preview={c[:60]}", flush=True)  # 全部QQ来源跳过operit注入引擎，已有自己的prompt
    
    # 记录输入日志
    logger.info(f"[QQ] [{session_id}] [IN] {'(蕊蕊)' if is_ruirui else ''} {user_input[:50]}")
    
    reply = await send_to_gateway(session_id, source, messages, skip_injection=skip_injection)
    
    # 记录输出日志
    logger.info(f"[QQ] [{session_id}] [OUT] {reply[:50]}")
    
    await split_and_send(target_type, target_id, reply)

# ==================== 场景处理 ====================

async def ruirui_timeout_handler():
    print(f"[QQ] [DEBUG] ruirui_timeout_handler启动, 等待{config.SILENCE_TIMEOUT}秒", flush=True)
    await asyncio.sleep(config.SILENCE_TIMEOUT)
    print(f"[QQ] [DEBUG] timeout到期, queue长度={len(qq_state.ruirui_queue)}", flush=True)
    if qq_state.ruirui_queue:
        combined = "\n".join(qq_state.ruirui_queue)
        qq_state.ruirui_queue.clear()
        asyncio.create_task(handle_generation("qq-ruirui", "qq-ruirui", "private", config.RUIRUI_QQ, combined, is_ruirui=True))

async def process_ruirui(msg_content: str):
    print(f"[QQ] [DEBUG] process_ruirui被调用: msg={msg_content[:30]}", flush=True)
    # 取消旧定时器
    if hasattr(qq_state, '_ruirui_timer') and qq_state._ruirui_timer:
        qq_state._ruirui_timer.cancel()
    
    qq_state.ruirui_queue.append(msg_content)
    
    if len(qq_state.ruirui_queue) > 50:
        logger.warning("[QQ] 蕊蕊缓存队列超过50条��")
    
    # ��置新定时器 (和其他人一样用 SILENCE_TIMEOUT)
    qq_state._ruirui_timer = asyncio.create_task(ruirui_timeout_handler())

async def private_timeout_handler(qq: int):
    await asyncio.sleep(config.SILENCE_TIMEOUT)
    q_data = qq_state.private_queues.pop(qq, None)
    if q_data and q_data["messages"]:
        combined = "\n".join(q_data["messages"])
        session_id = f"qq-private-{qq}"
        asyncio.create_task(handle_generation(session_id, session_id, "private", qq, combined))

async def process_private(qq: int, msg_content: str):
    if qq not in qq_state.private_queues:
        qq_state.private_queues[qq] = {"messages": []}
    
    # 取消旧定时器
    if "timer" in qq_state.private_queues[qq]:
        qq_state.private_queues[qq]["timer"].cancel()
        
    qq_state.private_queues[qq]["messages"].append(msg_content)
    
    # 设置新定时器
    timer_task = asyncio.create_task(private_timeout_handler(qq))
    qq_state.private_queues[qq]["timer"] = timer_task

async def process_group(group_id: int, sender_name: str, msg_content: str, raw_data: dict = None):
    if group_id not in qq_state.group_buffers:
        qq_state.group_buffers[group_id] = []
        
    buffer = qq_state.group_buffers[group_id]
    buffer.append({"sender": sender_name, "content": msg_content, "time": datetime.now()})
    
    # 清理过期或超出数量的缓冲
    cutoff = datetime.now().timestamp() - config.GROUP_BUFFER_TIME
    qq_state.group_buffers[group_id] = [m for m in buffer if m["time"].timestamp() > cutoff][-config.GROUP_BUFFER_SIZE:]
    
    # 检查触发条件
    is_triggered = False
    
    # 1. @Bot触发
    if f"[CQ:at,qq={config.BOT_QQ}]" in msg_content:
        is_triggered = True
        msg_content = msg_content.replace(f"[CQ:at,qq={config.BOT_QQ}]", "").strip()
    
    # 2. 引用回复触发 (仅回复Bot自己的消息才触发)
    if not is_triggered:
        if f"[CQ:reply," in msg_content and f"[CQ:at,qq={config.BOT_QQ}]" in msg_content:
            is_triggered = True
            msg_content = re.sub(r'\[CQ:reply,id=[^\]]*\]', '', msg_content)
            msg_content = re.sub(r'\[CQ:at,qq=\d+\]', '', msg_content).strip()
    
    # 3. 关键词触发
    if not is_triggered:
        for kw in config.GROUP_KEYWORDS:
            if kw and kw in msg_content:
                is_triggered = True
                break
                
    if is_triggered:
        # 构建上下文
        ctx_lines = [f"[群聊上下文 - 群号{group_id}]"]
        for m in qq_state.group_buffers[group_id][:-1]:
            ctx_lines.append(f"[{m['sender']}] {m['content']}")
        ctx_lines.append(f"[触发消息 - {sender_name}] {msg_content}")
        
        combined = "\n".join(ctx_lines)
        session_id = f"qq-group-{group_id}"
        asyncio.create_task(handle_generation(session_id, session_id, "group", group_id, combined))

# ==================== WS 路由 ====================

@qq_router.websocket("/onebot/ws")
async def qq_ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    qq_state.ws = websocket
    print("[QQ] NapCat WebSocket 已连接", flush=True)
    logger.info("[QQ] NapCat WebSocket 已连接")
    
    # 连接后自动拉取好友列表缓存备注
    async def _fetch_friends():
        await asyncio.sleep(2)
        if qq_state.ws:
            await qq_state.ws.send_json({"action": "get_friend_list", "params": {}, "echo": "fetch_friends"})
            logger.info("[QQ] 已请求好友列表")
    asyncio.create_task(_fetch_friends())
    
    try:
        while True:
            data = await websocket.receive_json()
            post_type = data.get("post_type")
            
            if post_type == "message":
                msg_type = data.get("message_type")
                sender = data.get("sender", {})
                user_id = sender.get("user_id")
                raw_message = data.get("raw_message", "")
                print(f"[QQ] 收到消息: type={msg_type} user={user_id} raw={raw_message[:50]}", flush=True)
                
                # 过滤Bot自身消息，防止回复循环
                self_id = data.get("self_id")
                if user_id == config.BOT_QQ or str(user_id) == str(config.BOT_QQ):
                    continue
                if self_id and user_id == self_id:
                    continue
                # NapCat可能对Bot自己发的消息也上报为post_type=message，额外检查
                if data.get("message_type") == "private" and data.get("sub_type") == "friend" and str(user_id) == str(self_id):
                    continue
                
                if msg_type == "private":
                    if user_id == config.RUIRUI_QQ:
                        await process_ruirui(raw_message)
                    else:
                        # 在消息前加上备注标识
                        remark = qq_state.friend_remarks.get(user_id) or sender.get("remark") or sender.get("nickname") or str(user_id)
                        tagged_msg = f"[对方: {remark}] {raw_message}"
                        await process_private(user_id, tagged_msg)
                elif msg_type == "group":
                    group_id = data.get("group_id")
                    # 优先用好友备注，其次群名片，最后昵称
                    nickname = qq_state.friend_remarks.get(user_id) or sender.get("card") or sender.get("nickname") or str(user_id)
                    await process_group(group_id, nickname, raw_message, data)
                    
            elif post_type == "request" and data.get("request_type") == "friend":
                # 好友验证：自动通过 + 用验证消息作备注
                flag = data.get("flag", "")
                comment = data.get("comment", "").strip()
                user_id = data.get("user_id")
                logger.info(f"[QQ] 收到好友请求: user_id={user_id}, comment={comment}")
                
                # 自动通过
                if qq_state.ws:
                    await qq_state.ws.send_json({
                        "action": "set_friend_add_request",
                        "params": {
                            "flag": flag,
                            "approve": True,
                            "remark": comment if comment else str(user_id)
                        }
                    })
                    logger.info(f"[QQ] 已自动通过好友请求，备注: {comment or user_id}")
                    # 更新本地缓存
                    qq_state.friend_remarks[user_id] = comment if comment else str(user_id)
                    
                    # 通过后延迟一下再设��注（有些协议端需要先通过再改备注）
                    async def _set_remark():
                        await asyncio.sleep(3)
                        if qq_state.ws and comment:
                            await qq_state.ws.send_json({
                                "action": "set_friend_remark",
                                "params": {
                                    "user_id": user_id,
                                    "remark": comment
                                }
                            })
                            logger.info(f"[QQ] 已设置好友备注: {user_id} -> {comment}")
                    asyncio.create_task(_set_remark())
                    
            # 处理好友列表响应
            if data.get("echo") == "fetch_friends" and "data" in data:
                friends = data["data"]
                if isinstance(friends, list):
                    for f in friends:
                        uid = f.get("user_id")
                        remark = f.get("remark", "") or f.get("nickname", "")
                        if uid and remark:
                            qq_state.friend_remarks[int(uid)] = remark
                    logger.info(f"[QQ] 好友列表已缓存: {len(qq_state.friend_remarks)} 人")
                    
            elif post_type == "meta_event" and data.get("meta_event_type") == "heartbeat":
                pass
                
    except WebSocketDisconnect:
        print("[QQ] NapCat WebSocket 断开连接", flush=True)
        logger.warning("[QQ] NapCat WebSocket 断开连接")
        qq_state.ws = None
    except Exception as e:
        print(f"[QQ] WebSocket 处理异常: {e}", flush=True)
        logger.error(f"[QQ] WebSocket 处理异���: {e}")
        qq_state.ws = None