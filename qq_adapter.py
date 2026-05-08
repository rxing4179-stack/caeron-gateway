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

# 全局状态
class QQState:
    def __init__(self):
        self.ws: Optional[WebSocket] = None
        self.ruirui_queue: List[str] = []
        
        # private_queues: { qq_number: {"timer": asyncio.Task, "messages": List[str]} }
        self.private_queues: Dict[int, dict] = {}
        
        # group_buffers: { group_id: [{"sender": "...", "content": "..."}] }
        self.group_buffers: Dict[int, List[dict]] = {}

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

async def retrieve_memory(query: str, top_k: int = 3) -> str:
    """检索相关记忆并拼接"""
    from embedding import get_embedding
    emb_vector = await get_embedding(query)
    if not emb_vector:
        return ""
        
    db = await get_db()
    try:
        # 提取向量并通过余弦相似度检索 (由于 SQLite 默认没有向量插件，这里需要加载所有 dialogue 并计算。
        # Gateway内部有类似实现，这里使用简化的关键词+SQLLIKE或读取全部)
        # 简单起见，如果数据库不大，我们直接模糊匹配。
        # 更好的方法是用 Gateway 内部的方法。
        cursor = await db.execute('''
            SELECT content FROM memories 
            WHERE category = 'dialogue' AND content LIKE ?
            ORDER BY id DESC LIMIT ?
        ''', (f"%{query}%", top_k))
        rows = await cursor.fetchall()
        if rows:
            return "\n".join([r['content'] for r in rows])
        return ""
    finally:
        await db.close()

async def send_to_gateway(session_id: str, source: str, messages: list):
    """请求网关进行处理"""
    logger.info(f"[{session_id}] 发送请求至 Gateway")
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            payload = {
                "model": "gpt-4o",  # 或配置中指定的模型
                "messages": messages,
                "stream": False
            }
            headers = {
                "x-session-id": session_id,
                "x-source": source
            }
            resp = await client.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data['choices'][0]['message']['content']
            else:
                logger.error(f"[{session_id}] API 请求失败: {resp.status_code}")
                return "（系统：我走神了，再说一遍）"
    except Exception as e:
        logger.error(f"[{session_id}] API 调用异常: {e}")
        return "（系统：我走神了，再说一遍）"

async def send_qq_msg(target_type: str, target_id: int, content: str):
    if not qq_state.ws:
        return
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

async def handle_generation(session_id: str, source: str, target_type: str, target_id: int, user_input: str, is_ruirui: bool = False):
    """处理消息生成流程"""
    history = await get_session_history(session_id)
    
    # 构建 messages
    system_prompt = config.RUIRUI_PROMPT if is_ruirui else config.DEFAULT_PROMPT
    
    if not is_ruirui:
        # 跨 session 检索
        memory = await retrieve_memory(session_id.split('-')[-1])
        if memory:
            system_prompt += f"\n\n[相关记忆片段]\n{memory}"
            
    messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": user_input}]
    
    # 记录输入日志
    logger.info(f"[QQ] [{session_id}] [IN] {user_input[:50]}")
    
    reply = await send_to_gateway(session_id, source, messages)
    
    # 记录输出日志
    logger.info(f"[QQ] [{session_id}] [OUT] {reply[:50]}")
    
    await split_and_send(target_type, target_id, reply)

# ==================== 场景处理 ====================

async def process_ruirui(msg_content: str):
    if msg_content == config.END_EMOJI:
        if not qq_state.ruirui_queue:
            return
        combined = "\n".join(qq_state.ruirui_queue)
        qq_state.ruirui_queue.clear()
        asyncio.create_task(handle_generation("qq-ruirui", "qq-ruirui", "private", config.RUIRUI_QQ, combined, is_ruirui=True))
    else:
        qq_state.ruirui_queue.append(msg_content)
        if len(qq_state.ruirui_queue) > 50:
            logger.warning("[QQ] 蕊蕊缓存队列超过50条！请检查是否忘记发送结束符。")

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

async def process_group(group_id: int, sender_name: str, msg_content: str):
    if group_id not in qq_state.group_buffers:
        qq_state.group_buffers[group_id] = []
        
    buffer = qq_state.group_buffers[group_id]
    buffer.append({"sender": sender_name, "content": msg_content, "time": datetime.now()})
    
    # 清理过期或超出数量的缓冲
    cutoff = datetime.now().timestamp() - config.GROUP_BUFFER_TIME
    qq_state.group_buffers[group_id] = [m for m in buffer if m["time"].timestamp() > cutoff][-config.GROUP_BUFFER_SIZE:]
    
    # 检查触发条件 (是否at了机器人，或者包含关键词)
    is_triggered = False
    if f"[CQ:at,qq={config.BOT_QQ}]" in msg_content:
        is_triggered = True
        msg_content = msg_content.replace(f"[CQ:at,qq={config.BOT_QQ}]", "").strip()
        
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
    logger.info("[QQ] NapCat WebSocket 已连接")
    
    try:
        while True:
            data = await websocket.receive_json()
            post_type = data.get("post_type")
            
            if post_type == "message":
                msg_type = data.get("message_type")
                sender = data.get("sender", {})
                user_id = sender.get("user_id")
                raw_message = data.get("raw_message", "")
                
                if msg_type == "private":
                    if user_id == config.RUIRUI_QQ:
                        await process_ruirui(raw_message)
                    else:
                        await process_private(user_id, raw_message)
                elif msg_type == "group":
                    group_id = data.get("group_id")
                    nickname = sender.get("card") or sender.get("nickname") or str(user_id)
                    await process_group(group_id, nickname, raw_message)
                    
            elif post_type == "meta_event" and data.get("meta_event_type") == "heartbeat":
                pass
                
    except WebSocketDisconnect:
        logger.warning("[QQ] NapCat WebSocket 断开连接")
        qq_state.ws = None
    except Exception as e:
        logger.error(f"[QQ] WebSocket 处理异常: {e}")
        qq_state.ws = None
