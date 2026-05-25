import asyncio
import json
from database import get_db
from memory_extractor import extract_fragments

async def run_batch_extraction():
    print("开始对历史记录进行记忆碎片提取...", flush=True)
    db = await get_db()
    try:
        # 获取所有还没有碎片的 memory (目前是全量)
        cursor = await db.execute("SELECT id, content FROM memories ORDER BY id ASC")
        rows = await cursor.fetchall()
        print(f"总计找到 {len(rows)} 条历史记录待提取。", flush=True)
        
        for idx, row in enumerate(rows):
            mem_id = row['id']
            content = row['content']
            
            print(f"[{idx+1}/{len(rows)}] 正在提取 ID {mem_id} 的碎片...", flush=True)
            # 只有当长度足够，或者含有有意义内容时才提取（过滤极短对话以节省 token）
            if len(content) > 20:
                await extract_fragments(content, dialogue_ids=[mem_id])
                await asyncio.sleep(0.5) # 防止并发超限
            else:
                print(f"ID {mem_id} 内容太短，跳过。", flush=True)
                
    except Exception as e:
        print(f"提取过程出错: {e}")
    finally:
        await db.close()

if __name__ == "__main__":
    asyncio.run(run_batch_extraction())
