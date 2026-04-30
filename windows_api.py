from utils import now_cst, today_cst_str
# ==================== 窗口管理 CRUD ====================

@app.get("/admin/api/windows")
async def admin_list_windows():
    """列出所有手动创建的窗口及其关联对话，加上一个'未归类'虚拟窗口"""
    db = await get_db()
    try:
        # 1. 获取所有手动窗口
        cursor = await db.execute('SELECT * FROM windows ORDER BY updated_at DESC')
        win_rows = [dict(r) for r in await cursor.fetchall()]

        windows = []
        for wr in win_rows:
            cur2 = await db.execute('''
                SELECT c.conversation_id, c.model, c.message_count, c.created_at, c.last_message_at,
                       (SELECT content FROM messages WHERE conversation_id = c.conversation_id AND role = 'user'
                        AND content NOT LIKE '==========%%' ORDER BY message_index ASC LIMIT 1) as first_real_msg
                FROM conversations c WHERE c.window_id = ?
                ORDER BY COALESCE(c.last_message_at, c.created_at) DESC
            ''', [wr['id']])
            convs = []
            total_msgs = 0
            first_active = None
            last_active = None
            for row in await cur2.fetchall():
                row = dict(row)
                preview_raw = row.get('first_real_msg') or ''
                preview_raw = re.sub(r'<attachment[^>]*>[\s\S]*?</attachment>', '', preview_raw).strip()
                convs.append({
                    'conversation_id': row['conversation_id'],
                    'model': row['model'],
                    'message_count': row['message_count'],
                    'created_at': row['created_at'],
                    'last_message_at': row['last_message_at'],
                    'preview': (preview_raw[:100] + '...' if len(preview_raw) > 100 else preview_raw) if preview_raw else '',
                })
                total_msgs += row['message_count']
                ts = row['last_message_at'] or row['created_at']
                if not last_active or ts > last_active:
                    last_active = ts
                if not first_active or (row['created_at'] and row['created_at'] < first_active):
                    first_active = row['created_at']

            windows.append({
                'window_id': wr['id'],
                'title': wr['name'],
                'description': wr['description'],
                'color': wr['color'],
                'conversations': convs,
                'total_messages': total_msgs,
                'first_active': first_active,
                'last_active': last_active,
                'is_manual': True,
            })

        # 2. 未归类对话（window_id 为 NULL）
        cur3 = await db.execute('''
            SELECT c.conversation_id, c.model, c.message_count, c.created_at, c.last_message_at,
                   (SELECT content FROM messages WHERE conversation_id = c.conversation_id AND role = 'user'
                    AND content NOT LIKE '==========%%' ORDER BY message_index ASC LIMIT 1) as first_real_msg
            FROM conversations c WHERE c.window_id IS NULL
            ORDER BY COALESCE(c.last_message_at, c.created_at) DESC
        ''')
        unassigned = []
        un_total = 0
        un_first = None
        un_last = None
        for row in await cur3.fetchall():
            row = dict(row)
            preview_raw = row.get('first_real_msg') or ''
            preview_raw = re.sub(r'<attachment[^>]*>[\s\S]*?</attachment>', '', preview_raw).strip()
            unassigned.append({
                'conversation_id': row['conversation_id'],
                'model': row['model'],
                'message_count': row['message_count'],
                'created_at': row['created_at'],
                'last_message_at': row['last_message_at'],
                'preview': (preview_raw[:100] + '...' if len(preview_raw) > 100 else preview_raw) if preview_raw else '',
            })
            un_total += row['message_count']
            ts = row['last_message_at'] or row['created_at']
            if not un_last or ts > un_last:
                un_last = ts
            if not un_first or (row['created_at'] and row['created_at'] < un_first):
                un_first = row['created_at']

        if unassigned:
            windows.append({
                'window_id': None,
                'title': '未归类对话',
                'description': '尚未分配到任何窗口的对话',
                'color': '#6b7280',
                'conversations': unassigned,
                'total_messages': un_total,
                'first_active': un_first,
                'last_active': un_last,
                'is_manual': False,
            })

        return windows
    finally:
        await db.close()


@app.post("/admin/api/windows")
async def admin_create_window(request: Request):
    """创建新窗口"""
    body = await request.json()
    name = body.get('name', '').strip()
    if not name:
        return JSONResponse({'error': '窗口名称不能为空'}, status_code=400)
    description = body.get('description', '')
    color = body.get('color', '#4a90d9')
    db = await get_db()
    try:
        cursor = await db.execute(
            'INSERT INTO windows (name, description, color) VALUES (?, ?, ?)',
            [name, description, color]
        )
        await db.commit()
        return {'id': cursor.lastrowid, 'name': name, 'description': description, 'color': color}
    finally:
        await db.close()


@app.put("/admin/api/windows/{window_id}")
async def admin_update_window(window_id: int, request: Request):
    """更新窗口名称/描述/颜色"""
    body = await request.json()
    db = await get_db()
    try:
        sets = []
        params = []
        for field in ['name', 'description', 'color']:
            if field in body:
                sets.append(f"{field} = ?")
                params.append(body[field])
        if not sets:
            return JSONResponse({'error': '无更新字段'}, status_code=400)
        sets.append("updated_at = datetime('now', '+8 hours'))")
        params.append(window_id)
        await db.execute(f"UPDATE windows SET {', '.join(sets)} WHERE id = ?", params)
        await db.commit()
        return {'success': True}
    finally:
        await db.close()


@app.delete("/admin/api/windows/{window_id}")
async def admin_delete_window(window_id: int):
    """删除窗口（对话回归未归类，不删除对话本身）"""
    db = await get_db()
    try:
        await db.execute('UPDATE conversations SET window_id = NULL WHERE window_id = ?', [window_id])
        await db.execute('DELETE FROM windows WHERE id = ?', [window_id])
        await db.commit()
        return {'success': True}
    finally:
        await db.close()


@app.post("/admin/api/windows/{window_id}/assign")
async def admin_assign_conversations(window_id: int, request: Request):
    """将对话分配到指定窗口"""
    body = await request.json()
    conversation_ids = body.get('conversation_ids', [])
    if not conversation_ids:
        return JSONResponse({'error': '未指定对话'}, status_code=400)
    db = await get_db()
    try:
        cur = await db.execute('SELECT id FROM windows WHERE id = ?', [window_id])
        if not await cur.fetchone():
            return JSONResponse({'error': '窗口不存在'}, status_code=404)
        placeholders = ','.join(['?' for _ in conversation_ids])
        await db.execute(
            f'UPDATE conversations SET window_id = ? WHERE conversation_id IN ({placeholders})',
            [window_id] + conversation_ids
        )
        await db.commit()
        return {'success': True, 'assigned': len(conversation_ids)}
    finally:
        await db.close()


@app.post("/admin/api/windows/unassign")
async def admin_unassign_conversations(request: Request):
    """将对话从窗口中移除（回到未归类）"""
    body = await request.json()
    conversation_ids = body.get('conversation_ids', [])
    if not conversation_ids:
        return JSONResponse({'error': '未指定对话'}, status_code=400)
    db = await get_db()
    try:
        placeholders = ','.join(['?' for _ in conversation_ids])
        await db.execute(
            f'UPDATE conversations SET window_id = NULL WHERE conversation_id IN ({placeholders})',
            conversation_ids
        )
        await db.commit()
        return {'success': True}
    finally:
        await db.close()
