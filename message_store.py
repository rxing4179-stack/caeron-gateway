import re
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


async def generate_conversation_id(messages: list) -> str:
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
    
    if not _session_state['conversation_id']:
        try:
            db = await get_db()
            cursor = await db.execute(
                "SELECT conversation_id, created_at FROM messages WHERE role = 'user' AND conversation_id NOT LIKE 'qq-%' ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                last_conv_id = row['conversation_id']
                last_created = row['created_at']
                
                cursor2 = await db.execute(
                    "SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY id DESC LIMIT 500",
                    (last_conv_id,)
                )
                rows2 = await cursor2.fetchall()
                hashes = set()
                for r in rows2:
                    hashes.add(_hash_content(r['content']))
                
                import datetime
                try:
                    last_dt = datetime.datetime.strptime(last_created, '%Y-%m-%d %H:%M:%S')
                    last_ts = last_dt.timestamp()
                except:
                    last_ts = now
                
                _session_state['conversation_id'] = last_conv_id
                _session_state['known_msg_hashes'] = hashes
                _session_state['last_activity'] = last_ts
                logger.info(f"[SESSION] 重启恢复状态: conv_id={last_conv_id[:8]}, 恢复了 {len(hashes)} 个哈希")
            await db.close()
        except Exception as e:
            logger.error(f"[SESSION] 恢复状态失败: {e}")
            
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
    
    # 尝试在数据库中通过最近的 user 消息寻找历史会话
    db = await get_db()
    try:
        user_msgs = [m for m in messages if m.get('role') == 'user']
        if len(user_msgs) > 1:
            # 使用倒数第二条user消息作为锚点（最后一条通常是新消息）
            anchor_msg = user_msgs[-2]
            anchor_content = anchor_msg.get('content', '')
            if isinstance(anchor_content, list):
                anchor_content = json.dumps(anchor_content, ensure_ascii=False)
            
            cursor = await db.execute(
                "SELECT conversation_id FROM messages WHERE role = 'user' AND content = ? AND conversation_id NOT LIKE 'qq-%' ORDER BY id DESC LIMIT 1",
                (anchor_content,)
            )
            row = await cursor.fetchone()
            if row:
                found_conv_id = row['conversation_id']
                logger.info(f"[SESSION] 通过数据库锚点找回丢失的session: {found_conv_id[:8]}")
                _session_state['conversation_id'] = found_conv_id
                _session_state['last_activity'] = now
                
                # 重新加载这个会话的 hashes
                cursor = await db.execute(
                    "SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY id DESC LIMIT 500",
                    (found_conv_id,)
                )
                rows = await cursor.fetchall()
                _session_state['known_msg_hashes'] = {_hash_content(r['content']) for r in rows}
                _session_state['known_msg_hashes'].update(incoming_hashes)
                return found_conv_id
    finally:
        await db.close()

    # 没找到历史会话，创建新session
    new_id = _generate_fresh_id(messages)
    logger.info(f"[SESSION] 新session {new_id[:8]}... "
                f"(overlap={has_overlap}, timed_out={is_timed_out}, "
                f"gap={now - _session_state.get('last_activity', 0):.0f}s)")
    
    _session_state['conversation_id'] = new_id
    _session_state['last_activity'] = now
    _session_state['known_msg_hashes'] = incoming_hashes
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
        SELECT id, role, content, message_index FROM messages 
        WHERE conversation_id = ? AND message_index > ?
        ORDER BY message_index ASC
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
    
    # 删 unified_messages 中的对应 assistant 记录和 user 记录
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
        logger.info(f"[REROLL_DB_CLEANUP] 对话 {conversation_id[:8]} 从 unified_messages 删除了 {deleted_assistant_count} 条废弃 assistant 记录")
        
    deleted_user_count = sum(1 for m in excess_msgs if m['role'] == 'user')
    if deleted_user_count > 0:
        await db.execute('''
            DELETE FROM unified_messages 
            WHERE id IN (
                SELECT id FROM unified_messages 
                WHERE source = 'operit' AND role = 'user'
                ORDER BY id DESC LIMIT ?
            )
        ''', (deleted_user_count,))
        logger.info(f"[REROLL_DB_CLEANUP] 对话 {conversation_id[:8]} 从 unified_messages 删除了 {deleted_user_count} 条废弃 user 记录")
        
    # 关键：立即提交事务，防止后续代码提前 return 导致 SQLite 自动回滚删除操作
    await db.commit()
        
    # 级联清理 summaries 表中相关的轮总（避免重roll废弃的内容残留在轮总中）
    deleted_times = [m['created_at'] for m in excess_msgs if ('created_at' in m.keys() and m['created_at'])]
    min_deleted_time = min(deleted_times) if deleted_times else None
    if min_deleted_time:
        await db.execute("""
            UPDATE summaries 
            SET is_active = 0 
            WHERE tag = 'round' AND is_active = 1 AND created_at >= ?
        """, (min_deleted_time,))
        logger.info(f"[REROLL_DB_CLEANUP] 废弃了 created_at >= {min_deleted_time} 的旧轮总")

    # 级联清理 memories 和 memory_fragments
    from utils import clean_chat_text
    dropped_texts = [m['content'] for m in excess_msgs if m['role'] == 'assistant' and m['content']]
    for text_val in dropped_texts:
        cleaned_ast = clean_chat_text(text_val).strip()
        if cleaned_ast:
            cursor = await db.execute("SELECT id FROM memories WHERE category='dialogue' AND (content LIKE ? OR content LIKE ?)", 
                ('%' + cleaned_ast[:60] + '%', '%' + cleaned_ast + '%'))
            mem_rows = await cursor.fetchall()
            mem_ids = [str(r['id']) for r in mem_rows]
            
            if mem_ids:
                ids_str = ",".join(mem_ids)
                await db.execute(f"DELETE FROM memory_fragments WHERE source_dialogue_id IN ({ids_str})")
                await db.execute(f"DELETE FROM memories WHERE id IN ({ids_str})")
                logger.info(f"[REROLL_DB_CLEANUP] 删除了 {len(mem_ids)} 条 memories 及其关联碎片")
    await db.commit()

async def store_incoming_messages(conversation_id: str, messages: list) -> tuple:
    """
    存储客户端发送的一批入站消息，支持智能锚点比对、历史截断(Reroll/回滚/编辑)检测与增量消息写入。
    返回: (stored_count, was_rerolled, current_turn_messages)
    """
    if not messages:
        return 0, False, []

    chat_messages = [m for m in messages if m.get('role') != 'system']
    if not chat_messages:
        return 0, False, []

    db = await get_db()
    was_rerolled = False
    try:
        cursor = await db.execute(
            '''SELECT MAX(message_index) as max_idx, COUNT(*) as total_count FROM messages WHERE conversation_id = ?''',
            (conversation_id,)
        )
        row = await cursor.fetchone()
        last_index = row['max_idx'] if row and row['max_idx'] is not None else 0
        total_in_db = row['total_count'] if row else 0

        def _norm_text(val):
            if not val:
                return ""
            if isinstance(val, str) and val.lstrip().startswith('['):
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, list):
                        val = parsed
                except Exception:
                    pass
            if isinstance(val, list):
                text_parts = [b.get('text', '') for b in val if isinstance(b, dict) and b.get('type') == 'text']
                val = '\n'.join(text_parts) if text_parts else '[IMAGE_ONLY]'
            from utils import clean_chat_text
            cleaned = clean_chat_text(val)
            cleaned = re.sub(r'<attachment[^>]*>', '', cleaned).replace('</attachment>', '')
            cleaned = re.sub(r'\[Time:[^\]]*\]', '', cleaned)
            res = cleaned.strip()
            return res if res else '[IMAGE_ONLY]'

        def _calc_hash(role, val):
            norm = _norm_text(val)
            key = norm if norm else (str(val)[:80] if val else "")
            return hashlib.md5(f"{role}:{key}".encode('utf-8')).hexdigest()[:16]

        # 预计算入站user消息的归一化哈希 → 位置映射
        incoming_user_hashes = {}
        for i, msg in enumerate(chat_messages):
            if msg.get('role') == 'user':
                h = _calc_hash('user', msg.get('content', ''))
                incoming_user_hashes[h] = i

        match_pos = -1
        matched_db_index = -1

        if last_index > 0:
            cursor2 = await db.execute(
                '''SELECT content, message_index FROM messages WHERE conversation_id = ? AND role = 'user' ORDER BY message_index DESC LIMIT 15''',
                (conversation_id,)
            )
            anchor_candidates = await cursor2.fetchall()

            for anchor in anchor_candidates:
                anchor_hash = _calc_hash('user', anchor['content'] or '')
                if anchor_hash in incoming_user_hashes:
                    match_pos = incoming_user_hashes[anchor_hash]
                    matched_db_index = anchor['message_index']
                    break

        if match_pos >= 0:
            # 找到锚点，对比锚点之后的消息
            cursor3 = await db.execute(
                '''SELECT id, role, content, message_index FROM messages WHERE conversation_id = ? AND message_index > ? ORDER BY message_index ASC''',
                (conversation_id, matched_db_index)
            )
            db_after_anchor = await cursor3.fetchall()
            incoming_after_anchor_msgs = chat_messages[match_pos + 1:]

            divergence_offset = 0
            max_compare = min(len(db_after_anchor), len(incoming_after_anchor_msgs))
            for i in range(max_compare):
                db_msg = db_after_anchor[i]
                inc_msg = incoming_after_anchor_msgs[i]

                if db_msg['role'] != inc_msg.get('role'):
                    break

                if db_msg['role'] == 'assistant':
                    # Assistant 消息格式差异不作为分歧截断点
                    divergence_offset += 1
                    continue

                db_norm = _norm_text(db_msg['content'] or '')
                inc_norm = _norm_text(inc_msg.get('content', ''))

                if db_norm == inc_norm:
                    divergence_offset += 1
                else:
                    break

            if divergence_offset < len(db_after_anchor):
                first_invalid_db_index = db_after_anchor[divergence_offset]['message_index']
                delete_from_index = first_invalid_db_index - 1
                await _delete_excess_messages(db, conversation_id, delete_from_index)
                last_index = delete_from_index
                was_rerolled = True

            slice_start = match_pos + 1 + divergence_offset
            new_messages = chat_messages[slice_start:]
            start_index = last_index + 1
            logger.info(f"[STORE] 锚点匹配成功 pos={match_pos}, db_idx={matched_db_index}, 发现相同消息 {divergence_offset} 条, 新增 {len(new_messages)} 条 (对话: {conversation_id[:8]}...)")
        elif last_index > 0:
            # 存在历史但没有匹配到任何用户锚点 -> 发生了深度回滚或编辑了最初的消息
            cursor_all = await db.execute(
                '''SELECT id, role, content, message_index FROM messages WHERE conversation_id = ? ORDER BY message_index ASC''',
                (conversation_id,)
            )
            all_db_msgs = await cursor_all.fetchall()
            divergence_idx = 0
            max_c = min(len(all_db_msgs), len(chat_messages))
            for i in range(max_c):
                db_m = all_db_msgs[i]
                inc_m = chat_messages[i]
                if db_m['role'] != inc_m.get('role'):
                    break
                if db_m['role'] == 'assistant':
                    divergence_idx += 1
                    continue
                if _norm_text(db_m['content']) == _norm_text(inc_m.get('content')):
                    divergence_idx += 1
                else:
                    break

            if divergence_idx < len(all_db_msgs):
                del_from = all_db_msgs[divergence_idx]['message_index'] - 1
                await _delete_excess_messages(db, conversation_id, del_from)
                last_index = del_from
                was_rerolled = True

            new_messages = chat_messages[divergence_idx:]
            start_index = last_index + 1
            logger.info(f"[STORE] 深度回滚/编辑匹配 divergence_idx={divergence_idx}, 新增 {len(new_messages)} 条 (对话: {conversation_id[:8]}...)")
        else:
            # 纯新会话
            new_messages = chat_messages
            start_index = 1

        if not new_messages:
            return 0, was_rerolled, []

        now_bj = now_cst().strftime('%Y-%m-%d %H:%M:%S')
        stored = 0
        unified_to_save = []
        for i, msg in enumerate(new_messages):
            content = msg.get('content', '')
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)

            await db.execute(
                '''INSERT INTO messages (conversation_id, role, content, message_index, created_at) VALUES (?, ?, ?, ?, ?)''',
                (conversation_id, msg['role'], content, start_index + i, now_bj)
            )
            stored += 1

            if not conversation_id.startswith('qq-') and msg['role'] != 'assistant':
                if not unified_to_save or unified_to_save[-1].get('content') != msg.get('content'):
                    unified_to_save.append(msg)

        await db.execute(
            '''UPDATE conversations SET last_message_at = datetime('now', '+8 hours'), message_count = message_count + ? WHERE conversation_id = ?''',
            (stored, conversation_id)
        )
        await db.commit()
        logger.info(f"存储 {stored} 条入站消息 (对话: {conversation_id[:8]}...)")
    except Exception as e:
        logger.error(f"消息存档异常: {e}")
        return 0, False, []
    finally:
        await db.close()

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
        return
        
    if isinstance(content, str):
        if '<tool_result' in content or '<tool_call' in content or '<status type="warning"' in content:
            return  # 绝不将工具调用结果和警告状态存入统一跨端历史
        
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
    """检测消息是否包含技术内容（代码、命令、文件路径、工具调用等）"""
    if not isinstance(content, str):
        return False
    
    if '<tool_result' in content or '<tool_call' in content or '<status' in content:
        return True
        
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

async def get_unified_history(char_limit: int = 15000, exclude_count: int = 0, filter_technical: bool = False, source_filter: str = None, context_filter: str = None, ruirui_sync: bool = False, keep_image_turns: int = 0) -> list:
    """
    获取统一的历史记录，按字符数限制（从新到旧截断），
    将连续同一角色的多条消息智能合并为一个回合，保证上下文严格对齐且不丢失任何用户输入与图片。
    """
    db = await get_db()
    try:
        if context_filter:
            cursor = await db.execute('''
                SELECT source, source_context, role, content, char_count, timestamp 
                FROM unified_messages 
                WHERE source_context = ?
                ORDER BY timestamp DESC, id DESC LIMIT 150
            ''', (context_filter,))
        elif ruirui_sync:
            cursor = await db.execute('''
                SELECT source, source_context, role, content, char_count, timestamp 
                FROM unified_messages 
                WHERE source = 'operit' OR (source = 'qq' AND source_context LIKE 'private_%')
                ORDER BY timestamp DESC, id DESC LIMIT 150
            ''')
        elif source_filter:
            cursor = await db.execute('''
                SELECT source, source_context, role, content, char_count, timestamp 
                FROM unified_messages 
                WHERE source = ?
                ORDER BY timestamp DESC, id DESC LIMIT 150
            ''', (source_filter,))
        else:
            cursor = await db.execute('''
                SELECT source, source_context, role, content, char_count, timestamp 
                FROM unified_messages 
                ORDER BY timestamp DESC, id DESC LIMIT 150
            ''')
        rows = await cursor.fetchall()
        
        if exclude_count > 0:
            rows = rows[exclude_count:]

        # 从新到旧筛选出不超过 char_limit 的记录
        selected_rows = []
        total_chars = 0
        row_idx = 0
        for row in rows:
            row_idx += 1
            content = row['content']
            
            # 过滤技术内容
            if filter_technical:
                if isinstance(content, str) and _is_technical_content(content):
                    continue
                if content.startswith('[') and '"type"' in content:
                    try:
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
                    except Exception as e:
                        pass
            
            if total_chars + row['char_count'] > char_limit:
                break
                
            selected_rows.append((row, row_idx))
            total_chars += row['char_count']
            
        # 翻转为时间正序 (ASC)
        asc_rows = list(reversed(selected_rows))
        
        # 组装并合并连续同一角色的消息（时间间隔>30分钟的不合并，插入分隔）
        merged_history = []
        effective_keep = min(keep_image_turns, 3) if keep_image_turns > 0 else 3
        MERGE_GAP_SECONDS = 1800  # 30分钟
        
        prev_timestamp = None
        
        for row, r_idx in asc_rows:
            source = row['source']
            source_context = row['source_context']
            role = row['role']
            content = row['content']
            row_ts = row['timestamp'] if 'timestamp' in row.keys() else None
            
            # 清理 assistant 消息中可能残留的思考过程
            if role == 'assistant' and isinstance(content, str):
                content = re.sub(r'<think>[\s\S]*?</think>\s*', '', content)
                match = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}[\s\S]*)', content)
                if match and match.start() > 0:
                    content = match.group(1)
            
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
            
            if '<attachment' in content or content.startswith('<system>'):
                prefix = ""
                
            # 处理多模态结构
            msg_content = None
            if content.startswith('[') and '"type"' in content:
                try:
                    content_obj = json.loads(content)
                    if isinstance(content_obj, list):
                        # 超过保留轮次的图片转换为 [图片] 占位符
                        if r_idx > effective_keep:
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
                        msg_content = content_obj
                except Exception:
                    pass

            if msg_content is None:
                msg_content = f"{prefix}{content}"
            
            # 计算与上一条消息的时间间隔
            time_gap_big = False
            if row_ts and prev_timestamp:
                try:
                    from datetime import datetime
                    ts_fmt = "%Y-%m-%d %H:%M:%S"
                    t_cur = datetime.strptime(str(row_ts)[:19], ts_fmt)
                    t_prev = datetime.strptime(str(prev_timestamp)[:19], ts_fmt)
                    gap_sec = (t_cur - t_prev).total_seconds()
                    if gap_sec > MERGE_GAP_SECONDS:
                        time_gap_big = True
                except Exception:
                    pass
            
            prev_timestamp = row_ts
                
            # 智能合并同一角色的连续消息
            # 但如果时间间隔>30分钟，则不合并，而是插入 [用户未回复] 分隔
            if merged_history and merged_history[-1]['role'] == role:
                if time_gap_big:
                    # 时间间隔大，不合并。插入对方角色的占位消息来分隔
                    sep_role = 'user' if role == 'assistant' else 'assistant'
                    merged_history.append({'role': sep_role, 'content': '[对方未回复]'})
                    merged_history.append({'role': role, 'content': msg_content})
                else:
                    # 时间间隔小，正常合并
                    prev_c = merged_history[-1]['content']
                    if isinstance(prev_c, str) and isinstance(msg_content, str):
                        merged_history[-1]['content'] = prev_c + "\n\n" + msg_content
                    elif isinstance(prev_c, list) and isinstance(msg_content, list):
                        merged_history[-1]['content'] = prev_c + msg_content
                    elif isinstance(prev_c, list) and isinstance(msg_content, str):
                        merged_history[-1]['content'] = prev_c + [{"type": "text", "text": "\n\n" + msg_content}]
                    elif isinstance(prev_c, str) and isinstance(msg_content, list):
                        merged_history[-1]['content'] = [{"type": "text", "text": prev_c}] + msg_content
            else:
                merged_history.append({'role': role, 'content': msg_content})
                
        return merged_history
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
        
    if source == 'qq-judge':
        return
        
    if source == 'qq-proactive' and '[SKIP]' in content.upper():
        return
    
    if skip_messages_table:
        if not source.startswith('qq'):
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
            
            if not source.startswith('qq'):
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
    if not source.startswith('qq'):
        await store_unified_message(source, source_context, 'assistant', content)

async def _embed_and_store_dialogue_memory(conversation_id: str, assistant_content: str):
    """后台任务：把对话对存入 memories 表，并生成 embedding"""
    db = await get_db()
    try:
        # 1. 查找最近一条 user message
        cursor = await db.execute("""
            SELECT content FROM messages 
            WHERE conversation_id = ? AND role = 'user' 
            ORDER BY message_index DESC LIMIT 1
        """, (conversation_id,))
        row = await cursor.fetchone()
        user_content = row['content'] if row else ''
        
        # 简单清理一下多模态格式
        if isinstance(user_content, str) and user_content.startswith('['):
            try:
                arr = json.loads(user_content)
                if isinstance(arr, list):
                    text_parts = [item.get('text', '') for item in arr if item.get('type') == 'text']
                    user_content = '\n'.join(text_parts)
            except Exception as e:
                import logging; logging.getLogger(__name__).debug(f"Exception ignored: {e}")
                
        # 预处理清洗元数据
        from utils import clean_chat_text
        cleaned_user = clean_chat_text(user_content).strip()
        cleaned_ast = clean_chat_text(assistant_content).strip()
                
        # 2. 拼接成对存储
        combined_text = f"User: {cleaned_user}\nAssistant: {cleaned_ast}"
        
        # 3. 调 embedding API
        from embedding import get_embedding
        emb_vector = await get_embedding(combined_text)
        
        emb_json = json.dumps(emb_vector) if emb_vector else None
        
        # 4. 存入 memories 表（精准更新或新增，避免重roll重复插入相同用户的对话对）
        cursor = await db.execute("""
            SELECT id FROM memories 
            WHERE category = 'dialogue' 
              AND content LIKE ?
            ORDER BY id DESC LIMIT 1
        """, (f"User: {cleaned_user}\nAssistant:%",))
        existing_mem = await cursor.fetchone()
        
        if existing_mem:
            await db.execute("""
                UPDATE memories 
                SET content = ?, embedding = ?, updated_at = datetime('now', '+8 hours')
                WHERE id = ?
            """, (combined_text, emb_json, existing_mem['id']))
            await db.execute("DELETE FROM memory_fragments WHERE source_dialogue_id = ?", (existing_mem['id'],))
            await db.commit()
            logger.info(f"[EMBEDDING] 重roll覆盖更新 dialogue 记忆 (id={existing_mem['id']})")
        else:
            await db.execute("""
                INSERT INTO memories (content, category, embedding, created_at, updated_at)
                VALUES (?, 'dialogue', ?, datetime('now', '+8 hours'), datetime('now', '+8 hours'))
            """, (combined_text, emb_json))
            await db.commit()
            logger.info(f"[EMBEDDING] 成功将对话对存入 memories 表并生成向量 (dim={len(emb_vector) if emb_vector else 0})")
        
        # === Memory Fragments 提取逻辑 (每 8 轮提取一次) ===
        cursor = await db.execute("SELECT value FROM config WHERE key = 'fragment_extraction_round_counter'")
        row = await cursor.fetchone()
        counter = int(row['value']) if row and row['value'] else 0
        counter += 1
        await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('fragment_extraction_round_counter', ?)", (str(counter),))
        await db.commit()
        
        if counter >= 8:
            # 重置计数器
            await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('fragment_extraction_round_counter', '0')")
            await db.commit()
            
            # 取最近 8 轮对话记忆
            cursor = await db.execute(
                "SELECT id, content FROM memories WHERE category = 'dialogue' ORDER BY id DESC LIMIT 8"
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
                
                # 异步触发重大情感里程碑提炼
                from milestone_extractor import extract_milestones
                asyncio.create_task(extract_milestones(dialogue_text, dialogue_ids))
    except Exception as e:
        logger.error(f"存储对话记忆/生成向量失败: {e}")
    finally:
        await db.close()


async def append_qq_user_message(conversation_id: str, content: str) -> int:
    """专为 QQ 端安全追加单条用户消息，避免触发全量锚点比对与误删(Reroll)"""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT MAX(message_index) as max_idx FROM messages WHERE conversation_id = ?",
            (conversation_id,)
        )
        row = await cursor.fetchone()
        next_idx = (row['max_idx'] + 1) if row and row['max_idx'] is not None else 0
        from utils import now_cst
        now_bj = now_cst().strftime('%Y-%m-%d %H:%M:%S')
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content, message_index, created_at) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, 'user', content, next_idx, now_bj)
        )
        await db.execute(
            "UPDATE conversations SET last_message_at = ?, message_count = message_count + 1 WHERE conversation_id = ?",
            (now_bj, conversation_id)
        )
        await db.commit()
        logger.info(f"[STORE] QQ用户消息成功追加至 messages (conv={conversation_id[:8]}, idx={next_idx})")
        return next_idx
    except Exception as e:
        logger.error(f"[STORE] QQ用户消息追加失败: {e}")
        return -1
    finally:
        await db.close()
