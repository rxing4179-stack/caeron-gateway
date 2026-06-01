import json
import asyncio
import re
import random
import base64
import io
from typing import Dict, List, Optional
import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from datetime import datetime

from qq_config import config
from database import get_db
import message_store

import logging
logger = logging.getLogger(__name__)

# ==================== 图片压缩工具 ====================
def _compress_base64_image(b64_data: str, max_side: int = 256, quality: int = 60) -> str:
    """将 base64 图片缩放到 max_side px 并用 JPEG 重编码，减小 token 占用"""
    try:
        from PIL import Image
        # 去掉 data:image/...;base64, 前缀
        if ',' in b64_data:
            header, raw = b64_data.split(',', 1)
        else:
            header, raw = '', b64_data
        
        img_bytes = base64.b64decode(raw)
        img = Image.open(io.BytesIO(img_bytes))
        
        # 等比缩放
        w, h = img.size
        if max(w, h) > max_side:
            ratio = max_side / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        
        # 转 RGB（去 alpha）再编码 JPEG
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality)
        compressed = base64.b64encode(buf.getvalue()).decode('ascii')
        return f"data:image/jpeg;base64,{compressed}"
    except Exception as e:
        logger.warning(f"图片压缩失败，替换为占位符: {e}")
        return "[历史图片]"

def _compress_history_images(content):
    """对历史消息中的 multimodal content 做图片压缩"""
    if not isinstance(content, str):
        return content
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(parsed, list):
        return content
    
    changed = False
    for block in parsed:
        if isinstance(block, dict) and block.get('type') == 'image_url':
            url = block.get('image_url', {}).get('url', '')
            if 'base64,' in url and len(url) > 10000:
                block['image_url']['url'] = _compress_base64_image(url)
                changed = True
    
    return json.dumps(parsed, ensure_ascii=False) if changed else content

async def parse_message_to_multimodal(message_data, raw_message) -> str:
    """如果消息中包含图片，下载图片转base64内联，返回多模态JSON列表字符串，否则返回纯文本"""
    if not isinstance(message_data, list):
        return raw_message
    
    has_image = any(isinstance(m, dict) and m.get("type") == "image" for m in message_data)
    if not has_image:
        return raw_message
        
    content_list = []
    for item in message_data:
        if not isinstance(item, dict): continue
        t = item.get("type")
        if t == "text":
            text = item.get("data", {}).get("text", "")
            if text:
                content_list.append({"type": "text", "text": text})
        elif t == "image":
            url = item.get("data", {}).get("url", "")
            if url:
                # 下载图片并转 base64，避免临时链接过期
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.get(url)
                        if resp.status_code == 200:
                            img_bytes = resp.content
                            b64 = base64.b64encode(img_bytes).decode('ascii')
                            # 检测 content-type
                            ct = resp.headers.get("content-type", "image/jpeg")
                            if "png" in ct:
                                mime = "image/png"
                            elif "gif" in ct:
                                mime = "image/gif"
                            elif "webp" in ct:
                                mime = "image/webp"
                            else:
                                mime = "image/jpeg"
                            data_url = f"data:{mime};base64,{b64}"
                            # 压缩
                            data_url = _compress_base64_image(data_url, max_side=512, quality=70)
                            content_list.append({"type": "image_url", "image_url": {"url": data_url}})
                            logger.info(f"[QQ] 图片下载成功并转base64 ({len(b64)} chars)")
                        else:
                            logger.warning(f"[QQ] 图片下载失败: HTTP {resp.status_code}")
                            content_list.append({"type": "text", "text": "[图片加载失败]"})
                except Exception as e:
                    logger.warning(f"[QQ] 图片下载异常: {e}")
                    content_list.append({"type": "text", "text": "[图片加载失败]"})
                
    if not content_list:
        return raw_message
    return json.dumps(content_list, ensure_ascii=False)

def _content_chars(content) -> int:
    """计算 content 字符数（兼容 str 和 multimodal list）"""
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, ensure_ascii=False))

qq_router = APIRouter()

# ==================== thinking 标签过滤 ====================
def strip_thinking(text: str) -> str:
    """移除 <thinking>...</thinking> 或 <think>...</think> 或 <思考链>...</思考链> 标签及其内容"""
    if not text:
        return text
    # 匹配 <thinking> 或 <think> 或 <思考链> 或 <思考> 或 <thought>，直到遇到闭合标签，或者一直匹配到字符串末尾
    return re.sub(r'<(?:thinking|think|思考链|思考|thought)>[\s\S]*?(?:</(?:thinking|think|思考链|思考|thought)>|$)', '', text).strip()

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

async def send_to_gateway(session_id: str, source: str, messages: list):
    """请求网关进行处理"""
    logger.info(f"[{session_id}] 发送请求至 Gateway")
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            payload = {
                "model": config.DEFAULT_MODEL,
                "messages": messages,
                "stream": False
            }
            headers = {
                "x-session-id": session_id,
                "x-source": source,
                "x-skip-rules": "true"
            }
            resp = await client.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                raw_reply = data['choices'][0]['message']['content']
                # 【第一件】过滤 thinking 标签
                reply = strip_thinking(raw_reply)
                # 【第三件】剔除大模型模仿学习生成的上下文前缀
                reply = re.sub(r'^\[(QQ·私聊|QQ·群聊|QQ|Operit)[^\]]*\]\s*', '', reply)
                return reply
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

async def handle_generation(session_id: str, source: str, target_type: str, target_id: int, user_input, is_ruirui: bool = False):
    """处理消息生成流程"""
    source_context = f"group_{target_id}" if target_type == "group" else f"private_{target_id}"
    
    # 1. 保存用户的最新输入
    # user_input 可能是 str 或 list（多模态）
    store_content = user_input
    if isinstance(user_input, str) and user_input.startswith('[') and '"type"' in user_input:
        try:
            parsed = json.loads(user_input)
            if isinstance(parsed, list):
                store_content = parsed
        except:
            pass
    
    await message_store.store_unified_message('qq', source_context, 'user', store_content)
    log_preview = str(user_input)[:50] if isinstance(user_input, str) else "[多模态消息]"
    logger.info(f"[QQ] [{session_id}] [IN] {log_preview}")
    
    # 根据来源选择提示词
    is_group = (target_type == "group")
    system_prompt = get_qq_prompt(is_ruirui=is_ruirui, is_group=is_group)
    
    # 获取全局锁防并发冲突
    async with message_store.GATEWAY_SEND_LOCK:
        # 2. 拉取统一历史
        history = await message_store.get_unified_history(char_limit=15000)
        
        # 3. 组装发给大模型的上下文（包含系统提示词 + 统一历史，这里历史已经包含刚存入的 user_input）
        messages = [{"role": "system", "content": system_prompt}] + history
        
        reply = await send_to_gateway(session_id, source, messages)
        
        # 4. 保存大模型的回复
        await message_store.store_unified_message('qq', source_context, 'assistant', reply)
    
    # 记录输出日志
    logger.info(f"[QQ] [{session_id}] [OUT] {reply[:50]}")
    
    await split_and_send(target_type, target_id, reply)

# ==================== 场景处理 ====================

async def private_timeout_handler(qq: int, is_ruirui: bool = False):
    await asyncio.sleep(config.SILENCE_TIMEOUT)
    q_data = qq_state.private_queues.pop(qq, None)
    if q_data and q_data["messages"]:
        combined_list = []
        text_buffer = []
        for msg in q_data["messages"]:
            if msg.startswith('[') and '"type"' in msg:
                try:
                    obj = json.loads(msg)
                    if isinstance(obj, list):
                        if text_buffer:
                            combined_list.append({"type": "text", "text": "\n".join(text_buffer)})
                            text_buffer = []
                        combined_list.extend(obj)
                        continue
                except:
                    pass
            text_buffer.append(msg)
        
        if text_buffer:
            if not combined_list:
                combined = "\n".join(text_buffer)
            else:
                combined_list.append({"type": "text", "text": "\n".join(text_buffer)})
                combined = combined_list
        else:
            combined = combined_list if combined_list else ""
            
        session_id = "qq-ruirui" if is_ruirui else f"qq-private-{qq}"
        asyncio.create_task(handle_generation(session_id, session_id, "private", qq, combined, is_ruirui=is_ruirui))

async def process_private(qq: int, msg_content: str, is_ruirui: bool = False):
    if msg_content == config.END_EMOJI:
        # 主动触发，无需等待超时
        if "timer" in qq_state.private_queues.get(qq, {}):
            qq_state.private_queues[qq]["timer"].cancel()
        q_data = qq_state.private_queues.pop(qq, None)
        if q_data and q_data["messages"]:
            combined = "\n".join(q_data["messages"])
            session_id = "qq-ruirui" if is_ruirui else f"qq-private-{qq}"
            asyncio.create_task(handle_generation(session_id, session_id, "private", qq, combined, is_ruirui=is_ruirui))
        return

    if qq not in qq_state.private_queues:
        qq_state.private_queues[qq] = {"messages": []}
    
    # 取消旧定时器
    if "timer" in qq_state.private_queues[qq]:
        qq_state.private_queues[qq]["timer"].cancel()
        
    qq_state.private_queues[qq]["messages"].append(msg_content)
    
    # 设置新定时器
    timer_task = asyncio.create_task(private_timeout_handler(qq, is_ruirui))
    qq_state.private_queues[qq]["timer"] = timer_task

async def process_group(group_id: int, sender_name: str, msg_content: str):
    # 群聊暂时不支持多模态，退化为纯文本
    if isinstance(msg_content, str) and msg_content.startswith('[') and '"type"' in msg_content:
        try:
            obj = json.loads(msg_content)
            if isinstance(obj, list):
                texts = []
                for item in obj:
                    if item.get("type") == "text":
                        texts.append(item.get("text", ""))
                    elif item.get("type") == "image_url":
                        texts.append("[图片]")
                msg_content = "".join(texts)
        except:
            pass

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
                message_data = data.get("message", [])
                
                msg_content = await parse_message_to_multimodal(message_data, raw_message)
                
                if msg_type == "private":
                    if user_id == config.RUIRUI_QQ:
                        await process_private(user_id, msg_content, is_ruirui=True)
                    else:
                        await process_private(user_id, msg_content, is_ruirui=False)
                elif msg_type == "group":
                    group_id = data.get("group_id")
                    nickname = sender.get("card") or sender.get("nickname") or str(user_id)
                    await process_group(group_id, nickname, msg_content)
                    
            elif post_type == "meta_event" and data.get("meta_event_type") == "heartbeat":
                pass
                
    except WebSocketDisconnect:
        logger.warning("[QQ] NapCat WebSocket 断开连接")
        qq_state.ws = None
    except Exception as e:
        logger.error(f"[QQ] WebSocket 处理异常: {e}")
        qq_state.ws = None
