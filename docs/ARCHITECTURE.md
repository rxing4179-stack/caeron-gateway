# Caeron Gateway - 架构总览

## 项目定位
为 Operit AI 客户端定制的 OpenAI 兼容中转网关，核心功能：
1. 多供应商 LLM 路由（自动 fallback）
2. 提示词注入引擎（5种位置 × 2种角色）
3. 记忆系统（提取 + 向量检索 + 情绪权重 + 热度衰减）
4. 滚动总结系统（每15条 → 日 → 周 → 月）
5. Web 管理面板（手机浏览器友好）

## 技术栈
- **后端**: Python 3.10 + FastAPI + uvicorn
- **数据库**: SQLite + sqlite-vss（向量检索）或 numpy 余弦相似度
- **前端**: 单文件 HTML（内嵌 Vue3 CDN + Tailwind CSS CDN）
- **部署**: systemd 服务，直接运行在腾讯云轻量 2C2G Ubuntu 22.04
- **嵌入模型**: 外部 API（硅基流动免费额度 / 或复用主模型供应商的 embedding 端点）

## 服务器环境
- IP: 1.14.59.116
- OS: Ubuntu 22.04
- CPU: 2核 / RAM: 2GB / Disk: 50GB SSD
- 已有服务: Notion MCP (Node.js, port 3001) — 不动
- Python: 3.10.12 已安装
- 网关端口: 8080

## 整体数据流

```
┌─────────┐     POST /v1/chat/completions     ┌──────────────────┐
│ Operit  │ ──────────────────────────────────→│  Caeron Gateway  │
│ Client  │ ←──────────────────────────────────│  (FastAPI:8080)  │
└─────────┘     stream / non-stream response   └────────┬─────────┘
                                                        │
                                          ┌─────────────┼─────────────┐
                                          │             │             │
                                          ▼             ▼             ▼
                                    ┌──────────┐ ┌───────────┐ ┌──────────┐
                                    │ 注入引擎 │ │ 记忆系统  │ │ 总结系统 │
                                    └──────────┘ └───────────┘ └──────────┘
                                          │             │             │
                                          └─────────────┼─────────────┘
                                                        │
                                                        ▼
                                               ┌──────────────┐
                                               │  供应商路由   │
                                               │  (多API Key)  │
                                               └──────┬───────┘
                                                       │
                                          ┌────────────┼────────────┐
                                          ▼            ▼            ▼
                                    ┌──────────┐ ┌──────────┐ ┌──────────┐
                                    │ 供应商 A │ │ 供应商 B │ │ 供应商 C │
                                    │ (主力)   │ │ (备用1)  │ │ (备用2)  │
                                    └──────────┘ └──────────┘ └──────────┘
```

## 请求处理流程（每次 /v1/chat/completions 调用）

```
1. 接收请求 → 解析 messages 数组
2. 记忆检索 → 用最新 user 消息做向量搜索，取 top-K 相关记忆
3. 总结注入 → 根据时间窗口选择注入哪些总结层级
4. 提示词注入 → 按规则把各条 prompt 插入 messages 的指定位置
5. 构造最终 messages → 发给供应商
6. 流式/非流式转发响应
7. 异步后处理：
   a. 累计消息计数器 +1
   b. 如果计数器 % 15 == 0 → 触发段总结
   c. 提取记忆碎片（每 3 轮检查一次）
   d. 存储本轮对话到原始消息表
```

## 模块划分

### Module 1: 网关核心 + 供应商路由
- `main.py` — FastAPI 应用入口，路由定义
- `providers.py` — 多供应商管理，优先级选择，自动 fallback，健康检查
- `proxy.py` — 请求转发，流式/非流式处理

### Module 2: 提示词注入引擎
- `injection.py` — 注入规则管理，messages 数组操作
- 支持 5 种注入位置:
  - `system_prepend` — system prompt 最前
  - `system_append` — system prompt 最后
  - `dialog_start` — system 之后第一条消息
  - `before_latest` — 最新 user 消息之前
  - `at_depth_N` — 从底部数第 N 条位置
- 支持 2 种注入角色:
  - `system` — 作为 system 消息注入
  - `user_wrapped_system` — role=user 但内容包裹在 `<system>` 标签中
  - `assistant_prefill` — 预填充到 assistant 回复开头

### Module 3: 记忆系统
- `memory.py` — 记忆 CRUD，向量检索，热度管理
- `memory_extractor.py` — 从对话中提取记忆碎片（调用小模型）
- 记忆属性: content, tier(1-4), category, polarity(-1~1), energy(-1~1), heat, is_anchor, embedding, timestamps
- 热度系统: 时间衰减(半衰期) + 召回加热 + 情绪权重
- 检索: 向量相似度 × 热度权重，返回 top-K

### Module 4: 滚动总结系统
- `summarizer.py` — 段总结 / 日总结 / 周总结 / 月总结
- 每 15 条消息 → 段总结（调用主模型）
- 每日结束 → 日总结（从所有段总结压缩）
- 每周日 → 周总结（从 7 份日总结压缩）
- 每月底 → 月总结（从所有日总结压缩）
- 注入策略:
  - 当天: 段总结原文
  - 最近 7 天: 日总结
  - 7 天~1 月: 周总结
  - 1 月以上: 月总结
- 信息保全: 压缩后自动校验关键实体/数字/时间点是否保留

### Module 5: Web 管理面板
- `admin.py` — 管理 API 路由
- `static/admin.html` — 单文件前端（Vue3 + Tailwind CDN）
- 功能: 记忆管理、总结浏览(日历视图)、提示词规则编辑、供应商管理、设置

### Module 6: Dream 整合（后期）
- `dream.py` — 碎片整合、矛盾检测、前瞻推断
- 手动或定时触发

## 数据库表设计 (SQLite)

### providers — 供应商
```sql
CREATE TABLE providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                    -- 供应商名称
    api_base_url TEXT NOT NULL,            -- API 地址
    api_key TEXT NOT NULL,                 -- API Key
    supported_models TEXT DEFAULT '[]',    -- JSON 数组，支持的模型列表
    priority INTEGER DEFAULT 0,           -- 优先级，数字越小越优先
    is_enabled INTEGER DEFAULT 1,         -- 是否启用
    is_healthy INTEGER DEFAULT 1,         -- 健康状态
    last_error TEXT,                       -- 最近错误信息
    last_used_at TEXT,                     -- 最近使用时间
    created_at TEXT DEFAULT (datetime('now'))
);
```

### injection_rules — 注入规则
```sql
CREATE TABLE injection_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                    -- 规则名称
    content TEXT NOT NULL,                 -- 注入内容
    position TEXT NOT NULL DEFAULT 'system_append',  -- 注入位置
    role TEXT NOT NULL DEFAULT 'system',   -- 注入角色
    priority INTEGER DEFAULT 0,           -- 排序优先级
    depth INTEGER DEFAULT 0,              -- at_depth_N 时的 N 值
    is_enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

### memories — 记忆
```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    tier INTEGER DEFAULT 3,               -- 1-4 重要程度
    category TEXT DEFAULT 'daily',         -- 分类
    polarity REAL DEFAULT 0.0,            -- 情绪极性 -1~1
    energy REAL DEFAULT 0.0,              -- 情绪能量 -1~1
    heat REAL DEFAULT 1.0,                -- 热度值
    is_anchor INTEGER DEFAULT 0,          -- 锚点记忆
    embedding TEXT,                        -- JSON 数组存储向量
    recall_count INTEGER DEFAULT 0,        -- 被召回次数
    last_recalled_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

### messages — 原始消息
```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,                    -- user / assistant / system
    content TEXT NOT NULL,
    conversation_id TEXT,                  -- 对话标识
    created_at TEXT DEFAULT (datetime('now'))
);
```

### summaries — 总结
```sql
CREATE TABLE summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,                    -- segment / daily / weekly / monthly
    content TEXT NOT NULL,                 -- 总结内容
    full_source TEXT,                      -- 原始来源文本（完整版）
    date_start TEXT,                       -- 覆盖起始日期
    date_end TEXT,                         -- 覆盖结束日期
    message_start_id INTEGER,             -- 段总结: 起始消息 ID
    message_end_id INTEGER,               -- 段总结: 结束消息 ID
    key_entities TEXT,                     -- JSON: 关键实体校验列表
    created_at TEXT DEFAULT (datetime('now'))
);
```

### config — 动态配置
```sql
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
```

## 配置项（config 表默认值）

| key | default | description |
|-----|---------|-------------|
| summary_interval | 15 | 每多少条消息生成段总结 |
| memory_extract_interval | 3 | 每多少轮提取记忆 |
| max_memories_inject | 10 | 每次最多注入多少条记忆 |
| memory_heat_halflife_days | 14 | 热度半衰期（天） |
| anchor_auto_threshold | 0.8 | 自动升级为锚点的能量阈值 |
| embedding_provider | siliconflow | 向量嵌入供应商 |
| embedding_model | BAAI/bge-large-zh-v1.5 | 嵌入模型 |
| embedding_api_key | | 嵌入 API Key |
| summary_model | | 用于生成总结的模型（留空则用主模型） |
| summary_prompt_segment | (见下文) | 段总结 prompt 模板 |
| summary_prompt_daily | (见下文) | 日总结 prompt 模板 |
| summary_prompt_weekly | (见下文) | 周总结 prompt 模板 |
| summary_prompt_monthly | (见下文) | 月总结 prompt 模板 |

## 文件结构

```
caeron-gateway/
├── main.py                  # FastAPI 入口
├── providers.py             # 供应商路由
├── proxy.py                 # 请求转发
├── injection.py             # 提示词注入引擎
├── memory.py                # 记忆系统
├── memory_extractor.py      # 记忆提取
├── summarizer.py            # 滚动总结
├── dream.py                 # Dream 整合（后期）
├── database.py              # SQLite 连接和初始化
├── config.py                # 配置管理
├── admin.py                 # 管理 API
├── static/
│   └── admin.html           # 管理面板前端
├── requirements.txt
├── .env
├── gateway.db               # SQLite 数据库文件
└── docs/
    ├── ARCHITECTURE.md       # 本文件
    └── PROMPTS.md            # 给反重力的实现 prompt
```
