"""
Caeron Gateway - 消息存储管道
拦截并存档所有经过网关的对话消息（入站 + 出站）

Step 2 of Phase 4.0
"""

import hashlib
import json
import logging
from database import get_db

logger = logging.getLogger(__name__)


def generate_conversation_id(messages: list) -> str:
    """
    根据消息列表生成稳定的对话ID
    
    策略：取前两条消息（通常是system + 第一条user）的角色+内容做SHA256截断
    同一个对话的开头不会变，所以ID在对话生命周期内稳定
    
    已知局限：如果Operit截断了历史消息导致数组开头变化，ID会变
    后续Phase可升级为基于消息内容模糊匹配的策略
    """
    fingerprint_parts = []
    for msg in messages[:3]:  # 取前3条消息做指纹
        role = msg.get('role', '')
        content = msg.get('content', '')
        if isinstance(content, list):  # 多模态消息（图片等）
            content = json.dumps(content, ensure_ascii=False, sort_keys=True)
        fingerprint_parts.append(f"{role}:{content[:200]}")
    
    if not fingerprint_parts:
        # fallback：不应该走到这里，但防御性编程
        fingerprint_parts.append('empty')
    
    fingerprint = '|'.join(fingerprint_parts)
    return hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:16]


async def _get_default_window_id():
    """获取默认窗口ID（最新的'主窗口'系列）"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM windows WHERE name LIKE '主窗口%' ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await db.close()


async def ensure_conversation(conversation_id: str, model: str = None, provider_id: int = None):
    """确保对话记录存在，不存在则创建并分配默认窗口，已存在则跳过"""
    db = await get_db()
    try:
        cursor = await db.execute(
            'SELECT id, window_id FROM conversations WHERE conversation_id = ?',
            (conversation_id,)
        )
        row = await cursor.fetchone()
        if not row:
            # 新对话：创建并立即分配到默认窗口
            default_window = await _get_default_window_id()
            await db.execute(
                '''INSERT INTO conversations (conversation_id, model, provider_id, window_id)
                   VALUES (?, ?, ?, ?)''',
                (conversation_id, model, provider_id, default_window)
            )
            await db.commit()
            logger.info(f"新建对话记录: {conversation_id}, 分配窗口: {default_window}")
        else:
            # 对话已存在
            conv_id, window_id = row
            # 如果还没分配窗口，补分配
            if window_id is None:
                default_window = await _get_default_window_id()
                if default_window:
                    await db.execute(
                        'UPDATE conversations SET window_id = ? WHERE id = ?',
                        (default_window, conv_id)
                    )
                    logger.info(f"补分配对话 {conversation_id} 到窗口 {default_window}")
            # 更新model（可能切换了模型）
            if model:
                await db.execute(
                    'UPDATE conversations SET model = ? WHERE conversation_id = ?',
                    (model, conversation_id)
                )
            await db.commit()
    finally:
        await db.close()


async def store_incoming_messages(conversation_id: str, messages: list):
    """
    增量存储入站消息
    
    逻辑：
    1. 过滤出对话消息（user + assistant），跳过system（那是提示词不是对话）
    2. 对比数据库中已有消息数量
    3. 只存增量部分（新消息 = 数组尾部多出来的）
    
    这依赖一个假设：Operit每次请求发来的messages数组是追加式增长的
    即 [旧消息..., 新user消息]，旧消息部分不变
    """
    chat_messages = [m for m in messages if m.get('role') in ('user', 'assistant')]
    
    if not chat_messages:
        return 0
    
    db = await get_db()
    try:
        # 查已存消息数量
        cursor = await db.execute(
            'SELECT COUNT(*) FROM messages WHERE conversation_id = ?',
            (conversation_id,)
        )
        row = await cursor.fetchone()
        existing_count = row[0] if row else 0
        
        # 增量计算：只存数组中超出已有数量的部分
        new_messages = chat_messages[existing_count:]
        if not new_messages:
            return 0
        
        stored = 0
        for i, msg in enumerate(new_messages):
            content = msg.get('content', '')
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            
            await db.execute(
                '''INSERT INTO messages (conversation_id, role, content, message_index)
                   VALUES (?, ?, ?, ?)''',
                (conversation_id, msg['role'], content, existing_count + i)
            )
            stored += 1
        
        # 更新对话元信息
        await db.execute(
            '''UPDATE conversations 
               SET last_message_at = datetime('now'), 
                   message_count = message_count + ?
               WHERE conversation_id = ?''',
            (stored, conversation_id)
        )
        
        await db.commit()
        logger.info(f"存储 {stored} 条入站消息 (对话: {conversation_id[:8]}...)")
        return stored
    except Exception as e:
        logger.error(f"存储入站消息失败: {e}")
        return 0
    finally:
        await db.close()


async def store_assistant_response(conversation_id: str, content: str):
    """
    存储AI回复消息（出站）
    
    由proxy层在收到完整回复后调用：
    - 非流式：直接从JSON响应中提取
    - 流式：从收集的delta chunks拼接后调用
    """
    if not content or not content.strip():
        return
    
    db = await get_db()
    try:
        # 获取当前最大index
        cursor = await db.execute(
            'SELECT MAX(message_index) FROM messages WHERE conversation_id = ?',
            (conversation_id,)
        )
        row = await cursor.fetchone()
        next_index = (row[0] + 1) if row and row[0] is not None else 0
        
        await db.execute(
            '''INSERT INTO messages (conversation_id, role, content, message_index)
               VALUES (?, ?, ?, ?)''',
            (conversation_id, 'assistant', content, next_index)
        )
        
        await db.execute(
            '''UPDATE conversations 
               SET last_message_at = datetime('now'),
                   message_count = message_count + 1
               WHERE conversation_id = ?''',
            (conversation_id,)
        )
        
        await db.commit()
        logger.info(f"存储AI回复 (对话: {conversation_id[:8]}..., {len(content)} 字符)")
    except Exception as e:
        logger.error(f"存储AI回复失败: {e}")
    finally:
        await db.close()