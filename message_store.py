from utils import now_cst, today_cst_str
"""
Caeron Gateway - 消息存储管道
拦截并存档所有经过网关的对话消息（入站 + 出站）

Step 2 of Phase 4.0
"""

import hashlib
import json
import logging
import asyncio
from database import get_db

import time as _time

logger = logging.getLogger(__name__)


# ==================== 会话跟踪器 ====================
# 解决Operit滑动窗口截断导致messages[:3]变化→conversation_id碎片化的问题
# 策略：基于消息内容重叠检测 + 30分钟超时

_session_state = {
    'conversation_id': None,
    'last_activity': 0,
    'known_msg_hashes': set(),  # 已知的用户消息内容哈希
}

SESSION_TIMEOUT = 30 * 60  # 30分钟无活动才视为session结束


def _hash_content(content) -> str:
    """对消息内容生成简短哈希"""
    if isinstance(content, list):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    if not content:
        content = ''
    return hashlib.md5(content.encode('utf-8')).hexdigest()[:12]


def _generate_fresh_id(messages: list) -> str:
    """生成全新的conversation_id（仅在确认是新session时调用）"""
    fingerprint_parts = []
    for msg in messages[:3]:
        role = msg.get('role', '')
        content = msg.get('content', '')
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False, sort_keys=True)
        fingerprint_parts.append(f"{role}:{content[:200]}")
    if not fingerprint_parts:
        fingerprint_parts.append('empty')
    fingerprint = '|'.join(fingerprint_parts)
    return hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:16]


def generate_conversation_id(messages: list) -> str:
    """
    基于会话连续性检测生成稳定的对话ID
    
    策略：
    1. 提取本次请求中所有user消息的内容哈希
    2. 与内存中记录的"当前session已知消息"做交集
    3. 如果有交集 → 同一个session，复用conversation_id
    4. 如果无交集但未超时(30分钟) → 仍视为同一session（用户可能只是在思考）
    5. 无交集且超时 → 新session，生成新conversation_id
    
    这解决了Operit滑动窗口截断导致的碎片化问题：
    即使窗口滑动导致messages[:3]变化，只要有任何一条user消息
    在之前的请求中出现过，就能识别为同一个session。
    """
    global _session_state
    
    now = _time.time()
    
    # 提取本次请求中所有user消息的哈希
    incoming_hashes = set()
    for msg in messages:
        if msg.get('role') == 'user':
            incoming_hashes.add(_hash_content(msg.get('content', '')))
    
    # 检测与已知消息的重叠
    has_overlap = bool(incoming_hashes & _session_state['known_msg_hashes'])
    is_timed_out = (now - _session_state['last_activity']) > SESSION_TIMEOUT
    
    if _session_state['conversation_id'] and (has_overlap or not is_timed_out):
        # 继续当前session
        _session_state['last_activity'] = now
        _session_state['known_msg_hashes'].update(incoming_hashes)
        # 防止集合无限增长（保留最近500个哈希）
        if len(_session_state['known_msg_hashes']) > 500:
            _session_state['known_msg_hashes'] = incoming_hashes
        logger.debug(f"[SESSION] 复用session {_session_state['conversation_id'][:8]}... "
                     f"(overlap={has_overlap}, timeout={is_timed_out}, known={len(_session_state['known_msg_hashes'])})")
        return _session_state['conversation_id']
    
    # 新session
    new_id = _generate_fresh_id(messages)
    logger.info(f"[SESSION] 新session {new_id[:8]}... "
                f"(overlap={has_overlap}, timed_out={is_timed_out}, "
                f"gap={now - _session_state['last_activity']:.0f}s)")
    
    _session_state = {
        'conversation_id': new_id,
        'last_activity': now,
        'known_msg_hashes': incoming_hashes,
    }
    return new_id


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
    增量存储入站消息（兼容Operit滑动窗口）
    
    策略：
    1. 取数据库中该对话最后一条消息的内容哈希
    2. 在入站消息数组中找到这条消息的位置
    3. 存储该位置之后的所有新消息
    
    这比旧的"按计数"方案更健壮：即使Operit截断了开头的旧消息，
    只要最后一条已存消息还在上下文里，就能正确找到增量边界。
    """
    chat_messages = [m for m in messages if m.get('role') in ('user', 'assistant')]
    
    if not chat_messages:
        return 0
    
    db = await get_db()
    try:
        # 取数据库中最后一条消息的内容哈希和index
        cursor = await db.execute(
            '''SELECT content, message_index FROM messages 
               WHERE conversation_id = ? 
               ORDER BY message_index DESC LIMIT 1''',
            (conversation_id,)
        )
        last_stored = await cursor.fetchone()
        
        if not last_stored:
            # 全新对话，全部存入
            new_messages = chat_messages
            start_index = 0
        else:
            last_index = last_stored['message_index']
            
            # 取最近几条已存的USER消息作为锚点候选
            # （不用assistant消息，因为Operit会修改assistant内容：去掉thinking标签等）
            cursor2 = await db.execute(
                '''SELECT content, message_index FROM messages 
                   WHERE conversation_id = ? AND role = 'user'
                   ORDER BY message_index DESC LIMIT 5''',
                (conversation_id,)
            )
            anchor_candidates = await cursor2.fetchall()
            
            # 预计算入站user消息的哈希 → 位置映射
            incoming_user_hashes = {}
            for i, msg in enumerate(chat_messages):
                if msg.get('role') == 'user':
                    c = msg.get('content', '')
                    if isinstance(c, list):
                        c = json.dumps(c, ensure_ascii=False)
                    h = hashlib.md5(c.encode('utf-8')).hexdigest()[:16]
                    incoming_user_hashes[h] = i  # 同哈希保留最后出现的位置
            
            # 从最新的锚点开始尝试匹配
            match_pos = -1
            matched_db_index = -1
            for anchor in anchor_candidates:
                anchor_content = anchor['content'] or ''
                anchor_hash = hashlib.md5(anchor_content.encode('utf-8')).hexdigest()[:16]
                if anchor_hash in incoming_user_hashes:
                    match_pos = incoming_user_hashes[anchor_hash]
                    matched_db_index = anchor['message_index']
                    break
            
            if match_pos >= 0:
                # 找到锚点：存储锚点之后的所有消息（跳过锚点本身和它之前已存的）
                new_messages = chat_messages[match_pos + 1:]
                # 新消息的起始index = 锚点的db_index + 1 + 锚点后已存的assistant消息数
                # 简化：直接从last_index+1开始，确保不重叠
                start_index = last_index + 1
                logger.info(f"[STORE] 锚点匹配成功 pos={match_pos}, db_idx={matched_db_index}, "
                           f"新增{len(new_messages)}条 (对话: {conversation_id[:8]}...)")
            else:
                # 没找到匹配 — 所有锚点都被Operit滑掉了
                # 存储最后一条user消息（确保不丢失）
                last_user = None
                for msg in reversed(chat_messages):
                    if msg.get('role') == 'user':
                        last_user = msg
                        break
                new_messages = [last_user] if last_user else chat_messages[-1:]
                start_index = last_index + 1
                logger.warning(f"[STORE] 未找到锚点，存储最新user消息 (对话: {conversation_id[:8]}...)")
        
        if not new_messages:
            return 0
        
        now_bj = now_cst().strftime('%Y-%m-%d %H:%M:%S')
        stored = 0
        for i, msg in enumerate(new_messages):
            content = msg.get('content', '')
            if isinstance(content, list):
                # 多模态消息：提取文本，把base64图片替换为占位符，避免存储膨胀
                text_parts = []
                img_count = 0
                for part in content:
                    if isinstance(part, dict):
                        if part.get('type') == 'text':
                            text_parts.append(part.get('text', ''))
                        elif part.get('type') == 'image_url':
                            img_count += 1
                            text_parts.append(f'[图片{img_count}]')
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = ' '.join(text_parts) if text_parts else json.dumps(content, ensure_ascii=False)
            
            # 去重/覆盖逻辑：检查数据库最后一条同角色消息
            msg_role = msg.get('role', '')
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()[:16] if isinstance(content, str) else ''
            
            cursor_dup = await db.execute(
                '''SELECT message_index, content FROM messages 
                   WHERE conversation_id = ? AND role = ?
                   ORDER BY message_index DESC LIMIT 1''',
                (conversation_id, msg_role)
            )
            last_same_role = await cursor_dup.fetchone()
            
            if last_same_role:
                last_content = last_same_role['content'] or ''
                last_hash = hashlib.md5(last_content.encode('utf-8')).hexdigest()[:16]
                last_idx = last_same_role['message_index']
                
                if content_hash == last_hash:
                    # 完全重复，跳过（重roll/重试场景）
                    logger.info(f"[STORE_DEDUP] 跳过重复{msg_role}消息 (hash={content_hash}, 对话: {conversation_id[:8]}...)")
                    continue
                elif msg_role == 'user' and last_idx == start_index + i:
                    # 同位置但内容不同，视为编辑重发，覆盖
                    await db.execute(
                        '''UPDATE messages SET content = ?, created_at = ? 
                           WHERE conversation_id = ? AND message_index = ?''',
                        (content, now_bj, conversation_id, last_idx)
                    )
                    logger.info(f"[STORE_DEDUP] 覆盖编辑后的user消息 (idx={last_idx}, 对话: {conversation_id[:8]}...)")
                    stored += 1
                    continue
            
            await db.execute(
                '''INSERT INTO messages (conversation_id, role, content, message_index, created_at)
                   VALUES (?, ?, ?, ?, ?)''',
                (conversation_id, msg_role, content, start_index + i, now_bj)
            )
            stored += 1
        
        # 更新对话元信息
        await db.execute(
            '''UPDATE conversations 
               SET last_message_at = datetime('now', '+8 hours'), 
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
    
    重roll处理：如果最后一条消息是assistant，覆盖而不是新增
    """
    if not content or not content.strip():
        return
    
    now_bj = now_cst().strftime('%Y-%m-%d %H:%M:%S')
    db = await get_db()
    try:
        # 检查最后一条消息是否是assistant（重roll场景）
        cursor = await db.execute(
            '''SELECT id, role, message_index FROM messages 
               WHERE conversation_id = ? 
               ORDER BY message_index DESC LIMIT 1''',
            (conversation_id,)
        )
        last_msg = await cursor.fetchone()
        
        if last_msg and last_msg['role'] == 'assistant':
            # 重roll：覆盖最后一条assistant消息
            await db.execute(
                '''UPDATE messages SET content = ?, created_at = ?
                   WHERE id = ?''',
                (content, now_bj, last_msg['id'])
            )
            logger.info(f"覆盖AI回复(重roll) (对话: {conversation_id[:8]}..., {len(content)} 字符)")
        else:
            # 正常新增
            next_index = (last_msg['message_index'] + 1) if last_msg else 0
            await db.execute(
                '''INSERT INTO messages (conversation_id, role, content, message_index, created_at)
                   VALUES (?, ?, ?, ?, ?)''',
                (conversation_id, 'assistant', content, next_index, now_bj)
            )
            await db.execute(
                '''UPDATE conversations 
                   SET last_message_at = ?,
                       message_count = message_count + 1
                   WHERE conversation_id = ?''',
                (now_bj, conversation_id)
            )
            logger.info(f"存储AI回复 (对话: {conversation_id[:8]}..., {len(content)} 字符)")
        
        await db.commit()
        
        # 触发后台向量化任务
        asyncio.create_task(_embed_and_store_dialogue_memory(conversation_id, content))
        
    except Exception as e:
        logger.error(f"存储AI回复失败: {e}")
    finally:
        await db.close()

async def _embed_and_store_dialogue_memory(conversation_id: str, assistant_content: str):
    """后台任务：把对话对存入 memories 表，并生成 embedding"""
    db = await get_db()
    try:
        # 1. 查找最近一条 user message
        cursor = await db.execute(
            '''SELECT content FROM messages 
               WHERE conversation_id = ? AND role = 'user' 
               ORDER BY message_index DESC LIMIT 1''',
            (conversation_id,)
        )
        row = await cursor.fetchone()
        user_content = row['content'] if row else ''
        
        # 简单清理一下多模态格式
        if isinstance(user_content, str) and user_content.startswith('['):
            try:
                arr = json.loads(user_content)
                if isinstance(arr, list):
                    text_parts = [item.get('text', '') for item in arr if item.get('type') == 'text']
                    user_content = '\n'.join(text_parts)
            except:
                pass
                
        # 2. 拼接
        combined_text = f"User: {user_content}\nAssistant: {assistant_content}"
        
        # 3. 调 embedding API
        from embedding import get_embedding
        emb_vector = await get_embedding(combined_text)
        
        emb_json = json.dumps(emb_vector) if emb_vector else None
        
        # 4. 存入 memories 表
        await db.execute(
            '''INSERT INTO memories (content, category, embedding)
               VALUES (?, 'dialogue', ?)''',
            (combined_text, emb_json)
        )
        await db.commit()
        logger.info(f"[EMBEDDING] 成功将对话对存入 memories 表并生成向量 (dim={len(emb_vector) if emb_vector else 0})")
    except Exception as e:
        logger.error(f"[EMBEDDING] 对话向量化存储失败: {e}")
    finally:
        await db.close()