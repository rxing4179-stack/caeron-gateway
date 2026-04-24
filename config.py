"""
Caeron Gateway - 配置管理模块
从 SQLite config 表读写动态配置项
"""

import logging
from database import get_db

logger = logging.getLogger(__name__)

# 默认配置项：key -> (value, description)
DEFAULT_CONFIG = {
    'summary_interval': ('15', '每多少条消息生成段总结'),
    'memory_extract_interval': ('3', '每多少轮提取记忆'),
    'max_memories_inject': ('10', '每次最多注入多少条记忆'),
    'memory_heat_halflife_days': ('14', '热度半衰期（天）'),
    'anchor_auto_threshold': ('0.8', '自动升级为锚点的能量阈值'),
    'embedding_provider': ('siliconflow', '向量嵌入供应商'),
    'embedding_model': ('BAAI/bge-large-zh-v1.5', '嵌入模型'),
    'embedding_api_key': ('', '嵌入 API Key'),
    'summary_model': ('', '用于生成总结的模型（留空则用主模型）'),
}


async def get_config(key: str, default: str = '') -> str:
    """从 config 表读取配置值"""
    db = await get_db()
    try:
        cursor = await db.execute(
            'SELECT value FROM config WHERE key = ?', (key,)
        )
        row = await cursor.fetchone()
        return row['value'] if row else default
    finally:
        await db.close()


async def set_config(key: str, value: str, description: str = None) -> None:
    """设置配置项（存在则更新，不存在则插入）"""
    db = await get_db()
    try:
        await db.execute('''
            INSERT INTO config (key, value, description, updated_at) 
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET 
                value = excluded.value, 
                updated_at = datetime('now')
        ''', (key, value, description))
        await db.commit()
    finally:
        await db.close()


async def init_default_config():
    """初始化默认配置（仅写入不存在的 key）"""
    logger.info("正在初始化默认配置...")
    db = await get_db()
    try:
        for key, (value, desc) in DEFAULT_CONFIG.items():
            cursor = await db.execute(
                'SELECT key FROM config WHERE key = ?', (key,)
            )
            if not await cursor.fetchone():
                await db.execute(
                    'INSERT INTO config (key, value, description) VALUES (?, ?, ?)',
                    (key, value, desc)
                )
                logger.info(f"  写入默认配置: {key} = {value}")
        await db.commit()
        logger.info("默认配置初始化完成")
    finally:
        await db.close()
