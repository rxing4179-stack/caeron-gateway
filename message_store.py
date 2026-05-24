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
        unified_to_save = []
        for i, msg in enumerate(new_messages):
            content = msg.get('content', '')
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            
            await db.execute(
                '''INSERT INTO messages (conversation_id, role, content, message_index, created_at)
                   VALUES (?, ?, ?, ?, ?)''',
                (conversation_id, msg['role'], content, start_index + i, now_bj)
            )
            stored += 1
            
            # 记录要同步到 unified_messages 的记录，稍后在关闭当前数据库连接后统一保存，避免死锁
            if not conversation_id.startswith('qq-'):
                unified_to_save.append(msg)
        
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
    except Exception as e:
        logger.error(f"消息存档异常: {e}")
        return 0
    finally:
        await db.close()

    # === 在主数据库连接关闭后，单独保存跨端同步记录，防止写锁冲突死锁 ===
    for msg in unified_to_save:
        await store_unified_message('operit', 'main', msg['role'], msg.get('content', ''))

    return stored

async def cleanup_db_for_reroll(conversation_id: str, dropped_texts: list = None):
    """
    Reroll 清理逻辑：
    删除数据库中该对话最后一条 user 消息之后的所有消息（即废弃的 assistant 回复）。
    """
    db = await get_db()
    try:
        # 1. 找到最后一条 user 消息的 index
        cursor = await db.execute('''
            SELECT message_index FROM messages 
            WHERE conversation_id = ? AND role = 'user' 
            ORDER BY message_index DESC LIMIT 1
        ''', (conversation_id,))
        row = await cursor.fetchone()
        if not row:
            return
            
        last_user_idx = row['message_index']
        
        # 2. 删除该 user 之后的所有消息 (实际上就是之前被 Reroll 废弃的 assistant 回复)
        cursor = await db.execute('''
            DELETE FROM messages 
            WHERE conversation_id = ? AND message_index > ?
        ''', (conversation_id, last_user_idx))
        deleted_count = cursor.rowcount
        
        if deleted_count > 0:
            logger.info(f"[REROLL_DB_CLEANUP] 对话 {conversation_id[:8]} 删除了 {deleted_count} 条废弃记录")
            # 同步删除 unified_messages 中的同等数量的 operit assistant 记录
            await db.execute('''
                DELETE FROM unified_messages 
                WHERE id IN (
                    SELECT id FROM unified_messages 
                    WHERE source = 'operit' AND role = 'assistant'
                    ORDER BY id DESC LIMIT ?
                )
            ''', (deleted_count,))
            
        # 3. 级联清理 memories 和 memory_fragments
        if dropped_texts:
            for text in dropped_texts:
                if text.strip():
                    # 匹配 memories 表中包含该废弃回复的记录
                    cursor = await db.execute("SELECT id FROM memories WHERE content LIKE ?", ('%' + text.strip() + '%',))
                    mem_rows = await cursor.fetchall()
                    mem_ids = [str(r['id']) for r in mem_rows]
                    
                    if mem_ids:
                        ids_str = ",".join(mem_ids)
                        # 先删 fragments
                        await db.execute(f"DELETE FROM memory_fragments WHERE source_dialogue_id IN ({ids_str})")
                        # 再删 memories
                        await db.execute(f"DELETE FROM memories WHERE id IN ({ids_str})")
                        logger.info(f"[REROLL_DB_CLEANUP] 删除了 {len(mem_ids)} 条 memories 及其关联碎片")

        await db.commit()
    except Exception as e:
        logger.error(f"Reroll 清理数据库异常: {e}")
    finally:
        await db.close()

# ==================== 统一跨端上下文存储 ====================

# 简单的并发发送锁，防止双端同时读取历史和发送导致状态不一致
GATEWAY_SEND_LOCK = asyncio.Lock()

async def store_unified_message(source: str, source_context: str, role: str, content: str):
    """
    存储统一的跨端消息
    如果 content 是包含图片的 list，将其中的图片提取到 metadata 中，content 替换为纯文本
    """
    import json
    metadata = {}
    
    if role == 'tool':
        return  # 忽略工具返回结果，避免污染跨端上下文，也防止组装历史时引起 API 校验报错
        
    # 提取纯文本并处理图片
    text_content = content
    if isinstance(content, list):
        text_parts = []
        image_count = 0
        for block in content:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    text_parts.append(block.get('text', ''))
                elif block.get('type') == 'image_url':
                    image_count += 1
        if image_count > 0:
            metadata['images'] = image_count
            text_parts.append(f"[发送了 {image_count} 张图片]")
        text_content = '\n'.join(text_parts)
    
    char_count = len(text_content)
    meta_str = json.dumps(metadata, ensure_ascii=False) if metadata else None
    
    db = await get_db()
    try:
        await db.execute('''
            INSERT INTO unified_messages (source, source_context, role, content, char_count, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (source, source_context, role, text_content, char_count, meta_str))
        await db.commit()
    except Exception as e:
        logger.error(f"[UNIFIED] 统一消息存储异常: {e}")
    finally:
        await db.close()

async def get_unified_history(char_limit: int = 15000) -> list:
    """
    拉取统一的跨端历史记录，并打上来源标签
    """
    db = await get_db()
    try:
        cursor = await db.execute('''
            SELECT source, source_context, role, content, char_count 
            FROM unified_messages 
            ORDER BY timestamp DESC
        ''')
        rows = await cursor.fetchall()
        
        history = []
        total_chars = 0
        for row in rows:
            if total_chars + row['char_count'] > char_limit:
                break
            
            source = row['source']
            source_context = row['source_context']
            role = row['role']
            content = row['content']
            
            # 打标签
            prefix = ""
            if source == 'qq':
                if str(source_context).startswith('group_'):
                    group_id = str(source_context).split('_')[1]
                    prefix = f"[QQ·群聊 {group_id}] "
                elif str(source_context).startswith('private_'):
                    private_id = str(source_context).split('_')[1]
                    prefix = f"[QQ·私聊 {private_id}] "
                else:
                    prefix = f"[QQ] "
            elif source == 'operit':
                prefix = f"[Operit] "
            
            tagged_content = f"{prefix}{content}"
            history.append({'role': role, 'content': tagged_content})
            total_chars += row['char_count']
            
        # 翻转顺序，按时间正序排列
        return list(reversed(history))
    except Exception as e:
        logger.error(f"[UNIFIED] 获取统一历史异常: {e}")
        return []
    finally:
        await db.close()


async def store_assistant_response(conversation_id: str, content: str, source: str = 'operit', source_context: str = 'main', skip_messages_table: bool = False):
    """
    存储AI回复消息（出站）
    
    由proxy层在收到完整回复后调用：
    - 非流式：直接从JSON响应中提取
    - 流式：从收集的delta chunks拼接后调用
    
    重roll处理：如果最后一条消息是assistant，覆盖而不是新增
    """
    if not content or not content.strip():
        return
    
    # === 新增：统一历史桥接 ===
    # 如果是 Operit 来源，则在此处保存 assistant 回复到 unified_messages。
    # QQ 来源已经在 qq_adapter.py 中自行保存了，这里避免重复保存。
    if source != 'qq':
        await store_unified_message(source, source_context, 'assistant', content)
        
    if skip_messages_table:
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
                
        # 预处理清洗元数据
        from utils import clean_chat_text
        cleaned_user = clean_chat_text(user_content)
        cleaned_ast = clean_chat_text(assistant_content)
                
        # 2. 拼接成对存储
        combined_text = f"User: {cleaned_user}\nAssistant: {cleaned_ast}"
        
        # 3. 调 embedding API
        from embedding import get_embedding
        emb_vector = await get_embedding(combined_text)
        
        emb_json = json.dumps(emb_vector) if emb_vector else None
        
        # 4. 存入 memories 表
        await db.execute(
            '''INSERT INTO memories (content, category, embedding, created_at, updated_at)
               VALUES (?, 'dialogue', ?, datetime('now', '+8 hours'), datetime('now', '+8 hours'))''',
            (combined_text, emb_json)
        )
        await db.commit()
        logger.info(f"[EMBEDDING] 成功将对话对存入 memories 表并生成向量 (dim={len(emb_vector) if emb_vector else 0})")
        
        # === Memory Fragments 提取逻辑 ===
        # 获取当前轮次计数
        cursor = await db.execute("SELECT value FROM config WHERE key = 'fragment_extraction_round_counter'")
        row = await cursor.fetchone()
        counter = int(row['value']) if row else 0
        counter += 1
        
        if counter >= 5:
            # 重置计数器
            await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('fragment_extraction_round_counter', '0')")
            await db.commit()
            
            # 取最近5条对话记忆
            cursor = await db.execute(
                "SELECT id, content FROM memories WHERE category = 'dialogue' ORDER BY id DESC LIMIT 5"
            )
            rows = await cursor.fetchall()
            if rows:
                rows.reverse() # 时间正序
                dialogue_text = ""
                dialogue_ids = []
                for idx, r in enumerate(rows):
                    dialogue_text += f"[轮次{idx}] (memories_id={r['id']})\n{r['content']}\n\n"
                    dialogue_ids.append(r['id'])
                
                # 异步触发提取
                from memory_extractor import extract_fragments
                asyncio.create_task(extract_fragments(dialogue_text, dialogue_ids))
        else:
            await db.execute(f"INSERT OR REPLACE INTO config (key, value) VALUES ('fragment_extraction_round_counter', '{counter}')")
            await db.commit()
            
    except Exception as e:
        logger.error(f"[EMBEDDING] 对话向量化存储失败: {e}")
    finally:
        await db.close()