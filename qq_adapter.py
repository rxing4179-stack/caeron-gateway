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
    finally:
        qq_state.generating[session_id] = False

async def _do_generation(session_id: str, source: str, target_type: str, target_id: int, user_input: str, is_ruirui: bool = False):
    """实际生成逻辑"""
    history = await get_session_history(session_id)
    
    is_group = (target_type == "group")
    
    if is_ruirui:
        # 蕊蕊：使用蕊蕊QQ专属提示词
        system_prompt = config.RUIRUI_QQ_PROMPT
    else:
        # 非蕊蕊：使用QQ社交人格提示词
        system_prompt = get_qq_prompt(is_ruirui=False, is_group=is_group)
    
    # 所有QQ来源都做记忆检索（用用户实际输入做向量查询）
    memory = await retrieve_memory(user_input)
    if memory:
        system_prompt += f"\n\n[相关记忆片段]\n{memory}"
    
    messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": user_input}]
    skip_injection = True  # 全部QQ来源跳过operit注入引擎，已有自己的prompt
    
    # 记录输入日志
    logger.info(f"[QQ] [{session_id}] [IN] {'(蕊蕊)' if is_ruirui else ''} {user_input[:50]}")
    
    reply = await send_to_gateway(session_id, source, messages, skip_injection=skip_injection)
    
    # 记录输出日志
    logger.info(f"[QQ] [{session_id}] [OUT] {reply[:50]}")
    
    await split_and_send(target_type, target_id, reply)

# ==================== 场景处理 ====================

async def ruirui_timeout_handler():
    await asyncio.sleep(config.SILENCE_TIMEOUT)
    if qq_state.ruirui_queue:
        combined = "\n".join(qq_state.ruirui_queue)
        qq_state.ruirui_queue.clear()
        asyncio.create_task(handle_generation("qq-ruirui", "qq-ruirui", "private", config.RUIRUI_QQ, combined, is_ruirui=True))

async def process_ruirui(msg_content: str):
    # 取消旧定时器
    if hasattr(qq_state, '_ruirui_timer') and qq_state._ruirui_timer:
        qq_state._ruirui_timer.cancel()
    
    qq_state.ruirui_queue.append(msg_content)
    
    if len(qq_state.ruirui_queue) > 50:
        logger.warning("[QQ] 蕊蕊缓存队列超过50条！")
    
    # 设置新定时器 (和其他人一样用 SILENCE_TIMEOUT)
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
    
    # 2. 引用回复触发 (回复Bot的消息)
    if not is_triggered:
        if f"[CQ:reply," in msg_content:
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
                    
                    # 通过后延迟一下再设备注（有些协议端需要先通过再改备注）
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
        logger.warning("[QQ] NapCat WebSocket 断开连接")
        qq_state.ws = None
    except Exception as e:
        logger.error(f"[QQ] WebSocket 处理异���: {e}")
        qq_state.ws = None