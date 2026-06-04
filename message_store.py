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


async def _delete_excess_messages(db, conversation_id: str, delete_from_index: int):
    # 查出要删除的 messages
    cursor = await db.execute('''
        SELECT id, role, content FROM messages 
        WHERE conversation_id = ? AND message_index > ?
    ''', (conversation_id, delete_from_index))
    excess_msgs = await cursor.fetchall()
    
    if not excess_msgs:
        return
        
    # 删 messages
    await db.execute('''
        DELETE FROM messages 
        WHERE conversation_id = ? AND message_index > ?
    ''', (conversation_id, delete_from_index))
    
    deleted_count = len(excess_msgs)
    logger.info(f"[REROLL_DB_CLEANUP] 对话 {conversation_id[:8]} 删除了 {deleted_count} 条废弃记录 (index > {delete_from_index})")
    
    # 删 unified_messages 中的对应 assistant 记录
    deleted_assistant_count = sum(1 for m in excess_msgs if m['role'] == 'assistant')
    if deleted_assistant_count > 0:
        await db.execute('''
            DELETE FROM unified_messages 
            WHERE id IN (
                SELECT id FROM unified_messages 
                WHERE source = 'operit' AND role = 'assistant'
                ORDER BY id DESC LIMIT ?
            )
        ''', (deleted_assistant_count,))
        
    # 级联清理 memories 和 memory_fragments
    from utils import clean_chat_text
    dropped_texts = [m['content'] for m in excess_msgs if m['role'] == 'assistant' and m['content']]
    for text in dropped_texts:
        cleaned_ast = clean_chat_text(text).strip()
        if cleaned_ast:
            cursor = await db.execute("SELECT id FROM memories WHERE category='dialogue' AND content LIKE ?", ('%' + cleaned_ast + '%',))
            mem_rows = await cursor.fetchall()
            mem_ids = [str(r['id']) for r in mem_rows]
            
            if mem_ids:
                ids_str = ",".join(mem_ids)
                await db.execute(f"DELETE FROM memory_fragments WHERE source_dialogue_id IN ({ids_str})")
                await db.execute(f"DELETE FROM memories WHERE id IN ({ids_str})")
                logger.info(f"[REROLL_DB_CLEANUP] 删除了 {len(mem_ids)} 条 memories 及其关联碎片")

async def store_incoming_messages(conversation_id: str, messages: list) -> tuple[int, bool, list]:
    """
    增量存储入站消息（兼容Operit滑动窗口）
    返回 (新增条数, 是否触发了reroll, 新增消息列表)
    """
    chat_messages = [m for m in messages if m.get('role') in ('user', 'assistant', 'tool')]
    
    if not chat_messages:
        return 0, False, []
        
    was_rerolled = False
    new_messages = []
    
    db = await get_db()
    try:
        # 获取当前对话的最大序号
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
                # 1. 获取 DB 中在 anchor 之后的所有消息
                cursor3 = await db.execute(
                    '''SELECT id, role, content, message_index FROM messages 
                       WHERE conversation_id = ? AND message_index > ?
                       ORDER BY message_index ASC''',
                    (conversation_id, matched_db_index)
                )
                db_after_anchor = await cursor3.fetchall()
                
                # 2. 准备 incoming 中在 anchor 之后的所有消息
                incoming_after_anchor_msgs = chat_messages[match_pos + 1:]
                
                # 3. 逐个对比，找到分歧点
                divergence_offset = 0
                max_compare = min(len(db_after_anchor), len(incoming_after_anchor_msgs))
                for i in range(max_compare):
                    db_msg = db_after_anchor[i]
                    inc_msg = incoming_after_anchor_msgs[i]
                    
                    if db_msg['role'] != inc_msg.get('role'):
                        break
                        
                    inc_content = inc_msg.get('content', '')
                    if isinstance(inc_content, list):
                        inc_content = json.dumps(inc_content, ensure_ascii=False)
                    
                    db_hash = hashlib.md5((db_msg['content'] or '').encode('utf-8')).hexdigest()[:16]
                    inc_hash = hashlib.md5(inc_content.encode('utf-8')).hexdigest()[:16]
                    
                    if db_hash != inc_hash:
                        break
                    
                    divergence_offset += 1
                
                # ==== 检测历史截断 / Reroll / 回滚 / 编辑 ====
                # 如果 DB 中在分歧点之后还有消息，说明发生了截断、重置、或消息被编辑，必须删除这些过期的记录
                if divergence_offset < len(db_after_anchor):
                    first_invalid_db_index = db_after_anchor[divergence_offset]['message_index']
                    delete_from_index = first_invalid_db_index - 1
                    await _delete_excess_messages(db, conversation_id, delete_from_index)
                    last_index = delete_from_index
                    was_rerolled = True
                
                # 新消息从 分歧点 开始截取并存入数据库
                slice_start = match_pos + 1 + divergence_offset
                new_messages = chat_messages[slice_start:]
                
                start_index = last_index + 1
                logger.info(f"[STORE] 锚点匹配成功 pos={match_pos}, db_idx={matched_db_index}, 发现相同消息 {divergence_offset} 条, "
                           f"新增 {len(new_messages)} 条 (对话: {conversation_id[:8]}...)")
            else:
                # 没找到匹配 — 所有锚点都被Operit滑掉了
                # 截取最后一条user消息及之后的所有消息（确保不丢失tool_result等）
                last_user_idx = len(chat_messages) - 1
                while last_user_idx >= 0 and chat_messages[last_user_idx].get('role') != 'user':
                    last_user_idx -= 1
                
                if last_user_idx != -1:
                    new_messages = chat_messages[last_user_idx:]
                else:
                    new_messages = chat_messages[-1:]
                    
                start_index = last_index + 1
                logger.warning(f"[STORE] 未找到锚点，存储最新user及后续消息 {len(new_messages)} 条 (对话: {conversation_id[:8]}...)")
        
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
        return 0, False, []
    finally:
        await db.close()

    # === 在主数据库连接关闭后，单独保存跨端同步记录，防止写锁冲突死锁 ===
    for msg in unified_to_save:
        await store_unified_message('operit', 'main', msg['role'], msg.get('content', ''))

    return stored, was_rerolled, new_messages


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
        
    text_content = content
    char_count = 0
    if isinstance(content, list):
        image_count = 0
        for block in content:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    char_count += len(block.get('text', ''))
                elif block.get('type') == 'image_url':
                    image_count += 1
                    char_count += 500  # 图片固定算 500 个字符
        if image_count > 0:
            metadata['images'] = image_count
        text_content = json.dumps(content, ensure_ascii=False)
    else:
        char_count = len(text_content) if text_content else 0
    
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

def _is_technical_content(content: str) -> bool:
    """检测消息是否包含技术内容（代码、命令、文件路径等）"""
    if not isinstance(content, str):
        return False
    
    # 检测代码块标记
    if '```' in content or '~~~' in content:
        return True
    
    # 检测文件路径
    if '/home/' in content or '/usr/' in content or '/var/' in content or 'C:\\' in content:
        return True
    
    # 检测常见编程语言关键词和代码模式
    code_patterns = [
        'def ', 'async def', 'class ', 'import ', 'from ', 'require(',
        'function ', 'const ', 'let ', 'var ', '=>',
        'async with', 'await ', '.py', '.js', '.ts', '.sh',
        'if __name__', 'return ', 'self.', 'this.',
        'SELECT ', 'INSERT ', 'UPDATE ', 'DELETE ', 'CREATE TABLE',
        'git ', 'npm ', 'pip ', 'docker ', 'kubectl ',
        'Exception', 'Error:', 'Traceback', 'at line',
        'localhost:', '127.0.0.1', 'http://127',
        '.execute(', '.query(', '.fetchall(', '.commit()'
    ]
    
    content_lower = content.lower()
    for pattern in code_patterns:
        if pattern.lower() in content_lower:
            return True
    
    # 检测Python/bash命令行输出特征
    lines = content.split('\n')
    if any(line.strip().startswith('$') or line.strip().startswith('>>>') for line in lines):
        return True
    
    return False

async def get_unified_history(char_limit: int = 15000, exclude_count: int = 0, filter_technical: bool = False) -> list:
    """
    获取统一的历史记录，按字符数限制（从新到旧截断）
    exclude_count: 排除最新的N条记录（用于在存入新消息后，重新拉取时不包含新消息）
    """
    db = await get_db()
    try:
        cursor = await db.execute('''
            SELECT source, source_context, role, content, char_count 
            FROM unified_messages 
            ORDER BY timestamp DESC
        ''')
        rows = await cursor.fetchall()
        
        if exclude_count > 0:
            rows = rows[exclude_count:]

        
        history = []
        total_chars = 0
        for row in rows:
            if total_chars + row['char_count'] > char_limit:
                break
            
            source = row['source']
            source_context = row['source_context']
            role = row['role']
            content = row['content']
            
            # 【新增】过滤技术内容
            if filter_technical:
                # 对于字符串内容直接检测
                if isinstance(content, str) and _is_technical_content(content):
                    continue
                # 对于多模态消息，检查其中的文本块
                if content.startswith('[') and '"type"' in content:
                    try:
                        import json
                        content_obj = json.loads(content)
                        if isinstance(content_obj, list):
                            has_technical = False
                            for block in content_obj:
                                if isinstance(block, dict) and block.get('type') == 'text':
                                    if _is_technical_content(block.get('text', '')):
                                        has_technical = True
                                        break
                            if has_technical:
                                continue
                    except:
                        pass
            
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
            
            # 如果是带有系统/附件标签的，避免加前缀破坏解析
            if '<attachment' in content or content.startswith('<system>'):
                prefix = ""
                
            if content.startswith('[') and '"type"' in content:
                try:
                    import json
                    content_obj = json.loads(content)
                    if isinstance(content_obj, list):
                        # 核心修复：清理历史记录中的巨大 base64 图片，防止 token 爆炸
                        for block in content_obj:
                            if isinstance(block, dict) and block.get("type") == "image_url":
                                block["type"] = "text"
                                block["text"] = "[图片]"
                                if "image_url" in block:
                                    del block["image_url"]

                        if prefix:
                            if content_obj and content_obj[0].get("type") == "text":
                                content_obj[0]["text"] = prefix + content_obj[0]["text"]
                            else:
                                content_obj.insert(0, {"type": "text", "text": prefix})
                        history.append({'role': role, 'content': content_obj})
                        total_chars += row['char_count']
                        continue
                except:
                    pass

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
    
    if skip_messages_table:
        if source != 'qq':
            await store_unified_message(source, source_context, 'assistant', content)
        return
        
    now_bj = now_cst().strftime('%Y-%m-%d %H:%M:%S')
    db = await get_db()
    is_reroll = False
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
            is_reroll = True
            # 重roll：覆盖最后一条assistant消息
            await db.execute(
                '''UPDATE messages SET content = ?, created_at = ?
                   WHERE id = ?''',
                (content, now_bj, last_msg['id'])
            )
            logger.info(f"覆盖AI回复(重roll) (对话: {conversation_id[:8]}..., {len(content)} 字符)")
            
            if source != 'qq':
                # 删除旧的 unified_messages 记录
                await db.execute('''
                    DELETE FROM unified_messages 
                    WHERE id IN (
                        SELECT id FROM unified_messages 
                        WHERE source = ? AND role = 'assistant'
                        ORDER BY id DESC LIMIT 1
                    )
                ''', (source,))
                
            # 删除旧的 memory 记录和 memory_fragments
            await db.execute('''
                DELETE FROM memory_fragments WHERE source_dialogue_id IN (
                    SELECT id FROM memories WHERE category = 'dialogue' ORDER BY id DESC LIMIT 1
                )
            ''')
            await db.execute('''
                DELETE FROM memories WHERE id IN (
                    SELECT id FROM memories WHERE category = 'dialogue' ORDER BY id DESC LIMIT 1
                )
            ''')
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

    # === 新增：统一历史桥接 ===
    # 在所有数据库清理工作（重roll的删除）完成后，再保存新的 unified_message
    if source != 'qq':
        await store_unified_message(source, source_context, 'assistant', content)

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