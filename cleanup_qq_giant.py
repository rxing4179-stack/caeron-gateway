import asyncio
import aiosqlite
import json

async def cleanup():
    db_path = '/home/ubuntu/caeron-gateway/gateway.db'
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        
        # Find giant QQ messages
        cursor = await db.execute('''
            SELECT id, LENGTH(content) as clen, content 
            FROM messages 
            WHERE conversation_id LIKE 'qq-%' 
              AND LENGTH(content) > 50000
            ORDER BY id DESC
        ''')
        rows = await cursor.fetchall()
        print(f"Found {len(rows)} giant messages to clean")
        
        cleaned = 0
        for row in rows:
            msg_id = row['id']
            content = row['content']
            clen = row['clen']
            
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    # Replace base64 image URLs with placeholder
                    changed = False
                    for block in parsed:
                        if isinstance(block, dict) and block.get('type') == 'image_url':
                            url = block.get('image_url', {}).get('url', '')
                            if 'base64,' in url and len(url) > 1000:
                                block['image_url']['url'] = '[历史图片已清理]'
                                changed = True
                    
                    if changed:
                        new_content = json.dumps(parsed, ensure_ascii=False)
                        await db.execute('UPDATE messages SET content = ? WHERE id = ?', (new_content, msg_id))
                        print(f"  ID={msg_id}: {clen} -> {len(new_content)} chars")
                        cleaned += 1
                    else:
                        # It's a giant text message (like group context buffer), truncate
                        if clen > 50000:
                            # Keep first 2000 chars of text
                            truncated = content[:2000] + '...[已截断]'
                            await db.execute('UPDATE messages SET content = ? WHERE id = ?', (truncated, msg_id))
                            print(f"  ID={msg_id}: text truncated {clen} -> {len(truncated)} chars")
                            cleaned += 1
                else:
                    # Not a list, just truncate
                    if clen > 50000:
                        truncated = content[:2000] + '...[已截断]'
                        await db.execute('UPDATE messages SET content = ? WHERE id = ?', (truncated, msg_id))
                        print(f"  ID={msg_id}: truncated {clen} -> {len(truncated)} chars")
                        cleaned += 1
            except json.JSONDecodeError:
                # Plain text that's too long
                if clen > 50000:
                    truncated = content[:2000] + '...[已截断]'
                    await db.execute('UPDATE messages SET content = ? WHERE id = ?', (truncated, msg_id))
                    print(f"  ID={msg_id}: plain text truncated {clen} -> {len(truncated)} chars")
                    cleaned += 1
        
        await db.commit()
        print(f"\nDone! Cleaned {cleaned} messages")

if __name__ == '__main__':
    asyncio.run(cleanup())
