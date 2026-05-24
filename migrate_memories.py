import asyncio
import json
import sqlite3
from utils import clean_chat_text
from embedding import get_embedding

async def run_migration():
    print("开始执行历史数据清洗与向量重算...")
    
    # 1. 取出所有数据
    conn = sqlite3.connect('database.sqlite')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, content FROM memories WHERE category = 'dialogue'")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    
    print(f"找到 {len(rows)} 条历史对话记录。")
    
    updated_count = 0
    for row in rows:
        orig_content = row['content']
        
        # 尝试拆分 user 和 assistant
        parts = orig_content.split('\nAssistant: ', 1)
        if len(parts) == 2:
            user_part = parts[0]
            if user_part.startswith('User: '):
                user_part = user_part[6:]
            ast_part = parts[1]
            
            # 清洗
            clean_user = clean_chat_text(user_part)
            clean_ast = clean_chat_text(ast_part)
            
            new_content = f"User: {clean_user}\nAssistant: {clean_ast}"
        else:
            # 非常规格式，直接整体清洗
            new_content = clean_chat_text(orig_content)
            
        # 如果内容发生了改变（移除了垃圾元数据），则更新并重算 embedding
        if new_content != orig_content:
            print(f"正在清洗并重算 ID: {row['id']} 的向量...")
            new_emb = await get_embedding(new_content)
            emb_json = json.dumps(new_emb) if new_emb else None
            
            # 独立更新，防止长事务锁库
            update_conn = sqlite3.connect('database.sqlite')
            update_conn.execute(
                "UPDATE memories SET content = ?, embedding = ? WHERE id = ?",
                (new_content, emb_json, row['id'])
            )
            update_conn.commit()
            update_conn.close()
            
            updated_count += 1
            
    print(f"迁移完成！共清洗并重算了 {updated_count} 条记录。")

if __name__ == "__main__":
    asyncio.run(run_migration())
