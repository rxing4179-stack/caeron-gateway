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
                unhealthy_since TEXT,
                fail_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', '+8 hours'))
            )
        ''')

        # Migration: 为已有 providers 表添加新字段（忽略已存在的情况）
        for col, col_def in [('unhealthy_since', 'TEXT'), ('fail_count', 'INTEGER DEFAULT 0')]:
            try:
                await db.execute(f'ALTER TABLE providers ADD COLUMN {col} {col_def}')
            except Exception:
                pass  # 字段已存在则忽略

        # ==================== 配置表 ====================
        await db.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT,
                updated_at TEXT DEFAULT (datetime('now', '+8 hours'))
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
                created_at TEXT DEFAULT (datetime('now', '+8 hours')),
                updated_at TEXT DEFAULT (datetime('now', '+8 hours'))
            )
        ''')

        # ==================== 对话表 ====================
        await db.execute('''
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT UNIQUE NOT NULL,
                provider_id INTEGER,
                model TEXT,
                started_at TEXT DEFAULT (datetime('now', '+8 hours')),
                last_message_at TEXT,
                message_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now', '+8 hours'))
            )
        ''')

        # ==================== 消息表（原文存档） ====================
        await db.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                message_index INTEGER,
                token_count INTEGER,
                created_at TEXT DEFAULT (datetime('now', '+8 hours')),
                FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
            )
        ''')

        # ==================== 总结表（多层级） ====================
        await db.execute('''
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT,
                level TEXT NOT NULL,
                tag TEXT DEFAULT '',
                content TEXT NOT NULL,
                message_range_start INTEGER,
                message_range_end INTEGER,
                period_start TEXT,
                period_end TEXT,
                parent_summary_id INTEGER,
                is_active INTEGER DEFAULT 1,
                token_count INTEGER,
                created_at TEXT DEFAULT (datetime('now', '+8 hours')),
                FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id),
                FOREIGN KEY (parent_summary_id) REFERENCES summaries(id)
            )
        ''')

        # Migration: summaries表添加tag字段
        try:
            await db.execute("ALTER TABLE summaries ADD COLUMN tag TEXT DEFAULT ''")
        except Exception:
            pass  # 字段已存在则忽略

        # ==================== 记忆表（便签墙） ====================
        await db.execute('''
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT,
                sentiment TEXT DEFAULT 'neutral',
                intensity INTEGER DEFAULT 3,
                relationship_importance INTEGER DEFAULT 3,
                weight REAL DEFAULT 0.5,
                frequency INTEGER DEFAULT 1,
                is_core INTEGER DEFAULT 0,
                embedding BLOB,
                source_message_id INTEGER,
                source_summary_id INTEGER,
                last_accessed_at TEXT,
                created_at TEXT DEFAULT (datetime('now', '+8 hours')),
                updated_at TEXT DEFAULT (datetime('now', '+8 hours')),
                FOREIGN KEY (source_message_id) REFERENCES messages(id),
                FOREIGN KEY (source_summary_id) REFERENCES summaries(id)
            )
        ''')
        # Migration: memories 表添加 threshold_hours 列（状态便签用）
        try:
            await db.execute('ALTER TABLE memories ADD COLUMN threshold_hours INTEGER DEFAULT 24')
        except Exception:
            pass  # 列已存在则忽略

        # 初始化状态便签种子数据
        status_seeds = [
            ('氟伏沙明', 26),
            ('劳拉西泮', 26),
            ('丁螺环酮', 26),
            ('普瑞巴林', 26),
            ('思诺思', 26),
            ('信必可', 26),
            ('阿布西替尼', 26),
            ('洗澡', 60)
        ]
        for key, hours in status_seeds:
            cursor = await db.execute("SELECT id FROM memories WHERE category='status' AND content=?", (key,))
            if not await cursor.fetchone():
                await db.execute(
                    "INSERT INTO memories (content, category, threshold_hours, updated_at) VALUES (?, 'status', ?, NULL)",
                    (key, hours)
                )
        # ==================== 窗口表（手动分组） ====================
        await db.execute('''
            CREATE TABLE IF NOT EXISTS windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                color TEXT DEFAULT '#4a90d9',
                created_at TEXT DEFAULT (datetime('now', '+8 hours')),
                updated_at TEXT DEFAULT (datetime('now', '+8 hours'))
            )
        ''')

        # conversations 表添加 window_id 列（如果不存在）
        try:
            await db.execute('ALTER TABLE conversations ADD COLUMN window_id INTEGER REFERENCES windows(id)')
        except Exception:
            pass  # 列已存在则忽略

        # ==================== 索引 ====================
        await db.execute('CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_summaries_conversation ON summaries(conversation_id)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_summaries_level ON summaries(level)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_summaries_active ON summaries(is_active)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_memories_weight ON memories(weight DESC)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_memories_core ON memories(is_core)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)')

        await db.commit()
        logger.info("数据库初始化完成")
    finally:
        await db.close()