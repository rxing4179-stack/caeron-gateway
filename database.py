"""
Caeron Gateway - 数据库管理模块
使用 aiosqlite 管理 SQLite 数据库连接和表初始化
"""

import aiosqlite
import logging
import os

logger = logging.getLogger(__name__)

# 数据库文件路径
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gateway.db')


async def get_db():
    """获取数据库连接"""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    # 启用 WAL 模式提升并发性能
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def init_db():
    """初始化数据库，创建所有表（如果不存在）"""
    logger.info("正在初始化数据库...")
    db = await get_db()
    try:
        # ==================== 供应商表 ====================
        await db.execute('''
            CREATE TABLE IF NOT EXISTS providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                supported_models TEXT DEFAULT '[]',
                priority INTEGER DEFAULT 0,
                is_enabled INTEGER DEFAULT 1,
                is_healthy INTEGER DEFAULT 1,
                last_error TEXT,
                last_used_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')

        # ==================== 配置表 ====================
        await db.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        ''')

        # ==================== 提示词注入规则表 ====================
        await db.execute('''
            CREATE TABLE IF NOT EXISTS injection_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                position TEXT NOT NULL DEFAULT 'system_append',
                role TEXT NOT NULL DEFAULT 'system',
                priority INTEGER DEFAULT 0,
                depth INTEGER DEFAULT 0,
                match_condition TEXT DEFAULT '{}',
                is_enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        ''')

        await db.commit()
        logger.info("数据库初始化完成")
    finally:
        await db.close()
