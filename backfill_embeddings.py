"""
backfill_embeddings.py - 回填 memories 表中缺失的 embedding 向量
用法: /home/ubuntu/caeron-gateway/venv/bin/python3 backfill_embeddings.py
"""
import asyncio
import json
import time
import logging
import sys
import os

sys.path.insert(0, '/home/ubuntu/caeron-gateway')
os.chdir('/home/ubuntu/caeron-gateway')

import aiosqlite
import httpx
import numpy as np
from config import get_config

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('backfill')
logging.getLogger('httpx').setLevel(logging.WARNING)

DB_PATH = '/home/ubuntu/caeron-gateway/gateway.db'

async def embed_text(text: str) -> list:
    """自包含的 embedding 函数，截断更激进，避免 413"""
    api_key = await get_config('embedding_api_key', '')
    model = await get_config('embedding_model', 'BAAI/bge-large-zh-v1.5')
    
    if not api_key:
        return []
    
    # 激进截断：最多 600 字符（bge-large-zh 约 512 token ≈ 600 汉字安全值）
    text = text[:600]
    
    url = "https://api.siliconflow.cn/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "input": text,
        "encoding_format": "float"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 413:
            # 再砍一刀到 300 字符
            payload["input"] = text[:300]
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(url, json=payload, headers=headers, timeout=15.0)
                    resp.raise_for_status()
                    data = resp.json()
                    return data["data"][0]["embedding"]
            except:
                return []
        return []
    except Exception as e:
        logger.error(f"Embedding error: {e}")
        return []

async def main():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    
    cursor = await db.execute(
        "SELECT id, content FROM memories WHERE category='dialogue' AND embedding IS NULL ORDER BY id"
    )
    rows = await cursor.fetchall()
    total = len(rows)
    logger.info(f"找到 {total} 条需要回填的 dialogue 记录")
    
    if total == 0:
        logger.info("无需回填，退出。")
        await db.close()
        return

    success = 0
    failed = 0
    skipped = 0
    start_time = time.time()

    for i, row in enumerate(rows):
        rid = row['id']
        content = row['content']
        
        if not content or not content.strip():
            skipped += 1
            continue
        
        try:
            emb = await embed_text(content)
            if emb and len(emb) > 0:
                emb_json = json.dumps(emb)
                await db.execute(
                    "UPDATE memories SET embedding = ? WHERE id = ?",
                    (emb_json, rid)
                )
                await db.commit()
                success += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            logger.error(f"[{rid}] 失败: {e}")
        
        done = i + 1
        if done % 100 == 0 or done == total:
            elapsed = time.time() - start_time
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            logger.info(
                f"进度: {done}/{total} ({done*100//total}%) | "
                f"成功={success} 失败={failed} 跳过={skipped} | "
                f"速率={rate:.1f}条/秒 ETA={eta/60:.1f}分钟"
            )
        
        await asyncio.sleep(0.5)

    elapsed_total = time.time() - start_time
    await db.close()
    
    logger.info("=" * 50)
    logger.info(f"回填完成！总耗时: {elapsed_total/60:.1f} 分钟")
    logger.info(f"总计: {total} | 成功: {success} | 失败: {failed} | 跳过: {skipped}")
    logger.info("=" * 50)

if __name__ == '__main__':
    asyncio.run(main())
