# Caeron Gateway - 反重力实现 Prompt

> 使用说明：按顺序把每个 Phase 的 prompt 喂给反重力（Cursor），每完成一个 phase 测试通过后再进行下一个。
> 每个 prompt 开头都会引用 ARCHITECTURE.md 的相关部分，确保反重力理解整体上下文。

---

## Phase 1: 网关核心 + 多供应商路由

### Prompt（直接复制喂给反重力）:

```
你现在要帮我实现一个 OpenAI 兼容的 API 网关，叫 Caeron Gateway。这是 Phase 1：网关核心骨架和多供应商路由。

项目位置：/home/ubuntu/caeron-gateway/
技术栈：Python 3.10 + FastAPI + uvicorn + httpx + aiosqlite
端口：8080

请创建以下文件：

### 1. requirements.txt
```
fastapi==0.115.0
uvicorn[standard]==0.30.0
httpx==0.27.0
aiosqlite==0.20.0
python-dotenv==1.0.1
numpy==1.24.0
```

### 2. .env
```
# 网关配置
PORT=8080
# 供应商通过管理面板 / API 配置，不写在 .env 里
```

### 3. database.py
- 使用 aiosqlite
- 应用启动时自动建表（如果不存在）
- 需要的表（Phase 1 只需要 providers 和 config）：

```sql
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
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
```

### 4. config.py
- 从 config 表读取配置，提供 get_config(key, default) 和 set_config(key, value) 异步方法
- 应用启动时写入默认配置（如果 key 不存在的话）

### 5. providers.py
- ProviderManager 类：
  - `async get_provider(model: str) -> dict`: 根据请求的 model 名，在 enabled 且 healthy 的供应商中，按 priority 排序，找到 supported_models 包含该 model 的供应商返回。如果找不到精确匹配，返回 priority 最高的 enabled 供应商（通配）。
  - `async mark_unhealthy(provider_id: int, error: str)`: 标记供应商为不健康
  - `async mark_healthy(provider_id: int)`: 标记为健康
  - `async list_providers() -> list`: 列出所有供应商
  - `async add_provider(data: dict) -> int`: 添加供应商
  - `async update_provider(id: int, data: dict)`: 更新供应商
  - `async delete_provider(id: int)`: 删除供应商
  - `async test_provider(id: int) -> dict`: 测试供应商连通性（发一个 model list 请求）

### 6. proxy.py
- `async proxy_chat_completion(request_body: dict, provider: dict) -> StreamingResponse | JSONResponse`
- 核心逻辑：
  1. 从 request_body 拿到 messages, model, stream 等参数
  2. 用 provider 的 api_base_url 和 api_key 构造上游请求
  3. 如果 stream=true：用 httpx 的 stream 模式转发 SSE，逐 chunk yield
  4. 如果 stream=false：直接转发 JSON 响应
  5. 上游请求失败时抛出异常（由 main.py 捕获后尝试 fallback）
- 上游 URL 拼接规则：provider.api_base_url 可能是以下格式，都要正确处理：
  - `https://api.example.com` → 拼接为 `https://api.example.com/v1/chat/completions`
  - `https://api.example.com/v1` → 拼接为 `https://api.example.com/v1/chat/completions`
  - `https://api.example.com/v1/chat/completions` → 直接使用
- 请求头：Authorization: Bearer {api_key}，Content-Type: application/json
- httpx 超时设置：connect=10s, read=300s（长回复需要）

### 7. main.py
- FastAPI 应用
- 启动时调用 database.init_db()
- 路由：
  - `GET /` → 健康检查，返回 {"status": "running", "version": "0.1.0"}
  - `GET /v1/models` → 转发到当前最高优先级供应商的 /v1/models
  - `POST /v1/chat/completions` → 核心路由：
    1. 解析请求体
    2. 调用 provider_manager.get_provider(model) 获取供应商
    3. 调用 proxy.proxy_chat_completion 转发
    4. 如果失败，尝试下一个 healthy 的供应商（最多重试 2 次）
    5. 全部失败返回 502
  - 供应商管理 API（给管理面板用）：
    - `GET /admin/api/providers` → 列出供应商（api_key 只显示前8位+***）
    - `POST /admin/api/providers` → 添加供应商
    - `PUT /admin/api/providers/{id}` → 更新供应商
    - `DELETE /admin/api/providers/{id}` → 删除供应商
    - `POST /admin/api/providers/{id}/test` → 测试供应商
  - `GET /admin` → 返回静态 HTML 管理面板（Phase 1 只做供应商管理部分）

### 8. static/admin.html
Phase 1 的管理面板只需要：
- 页面标题 "Caeron Gateway"
- 供应商管理区域：
  - 列表展示所有供应商（名称、URL、状态灯、优先级）
  - 添加供应商表单（名称、API URL、API Key、支持的模型列表逗号分隔、优先级）
  - 编辑/删除按钮
  - 测试连通性按钮（点击后显示结果）
- 使用 Vue 3 CDN + Tailwind CSS CDN
- 深色主题，移动端友好
- 不需要登录认证（后期加）

### 关键要求：
1. 所有文件使用 UTF-8 编码
2. 异步优先，所有数据库和 HTTP 操作都用 async
3. 日志用 Python logging，格式：`[时间] [级别] [模块] 消息`
4. 错误处理要完善，不能因为一个供应商挂了就整个网关崩溃
5. 代码注释用中文
6. 流式转发必须正确处理 SSE 格式（data: {...}\n\n）和 [DONE] 标记

请一次性生成所有文件的完整代码。
```

---

## Phase 2: 提示词注入引擎

### Prompt:

```
继续开发 Caeron Gateway。这是 Phase 2：提示词注入引擎。

现在网关已经能正常转发请求了。这个 phase 要实现的是：在转发之前，按照用户配置的规则，把额外的提示词插入到 messages 数组的指定位置。

### 需要修改/新增的文件：

### 1. database.py — 新增 injection_rules 表
```sql
CREATE TABLE IF NOT EXISTS injection_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    position TEXT NOT NULL DEFAULT 'system_append',
    role TEXT NOT NULL DEFAULT 'system',
    priority INTEGER DEFAULT 0,
    depth INTEGER DEFAULT 0,
    is_enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

### 2. injection.py — 注入引擎核心

InjectionEngine 类：

`async inject(messages: list[dict]) -> list[dict]`:
- 从数据库读取所有 enabled 的 injection_rules，按 priority 排序
- 对每条规则，根据 position 和 role 把内容插入 messages 数组
- 返回修改后的 messages（不修改原数组，返回深拷贝）

支持的 position 值和对应逻辑：

1. `system_prepend` — 在 system 消息的内容最前面插入（如果没有 system 消息就创建一条）
2. `system_append` — 在 system 消息的内容最后面追加
3. `dialog_start` — 在 system 消息之后、第一条非 system 消息之前，插入一条独立消息
4. `before_latest` — 在最后一条 user 消息之前，插入一条独立消息
5. `at_depth_N` — 从 messages 数组末尾往前数第 depth 个位置插入（depth=0 等同于末尾，depth=1 等同于倒数第二个位置）

支持的 role 值：
1. `system` — 插入为 {"role": "system", "content": "..."}
2. `user_wrapped_system` — 插入为 {"role": "user", "content": "<system>...</system>"}
3. `assistant_prefill` — 特殊处理：在 messages 数组最末尾添加 {"role": "assistant", "content": "..."}，这会让模型从这段文字开始续写

注意事项：
- system_prepend 和 system_append 不插入新消息，而是修改现有 system 消息的 content
- dialog_start、before_latest、at_depth_N 会插入新的独立消息
- assistant_prefill 忽略 position 设置，始终添加到末尾
- 多条规则的 priority 数字越小越先处理
- content 支持变量替换：{cur_datetime} → 当前日期时间，{user_name} → "蕊蕊"，{assistant_name} → "沈栖"

### 3. main.py — 修改 chat/completions 路由
在发送给供应商之前，调用 injection_engine.inject(messages) 处理 messages。

### 4. admin.py 或 main.py — 新增注入规则管理 API
- `GET /admin/api/rules` → 列出所有规则
- `POST /admin/api/rules` → 添加规则
- `PUT /admin/api/rules/{id}` → 更新规则
- `DELETE /admin/api/rules/{id}` → 删除规则
- `POST /admin/api/rules/preview` → 预览：传入一个示例 messages，返回注入后的完整 messages（用于调试）

### 5. static/admin.html — 新增注入规则管理页面
在管理面板中新增一个 tab/section：
- 规则列表（名称、位置、角色、优先级、启用开关）
- 添加/编辑规则的表单：
  - 名称（文本框）
  - 内容（大文本框，支持多行）
  - 注入位置（下拉框：5 个选项）
  - 注入角色（下拉框：3 个选项）
  - 优先级（数字）
  - 深度（仅 at_depth_N 时显示）
  - 启用开关
- 预览按钮：弹窗显示注入后的完整 messages 结构（JSON 格式化显示）
- 拖拽排序（可选，不强求）

### 关键要求：
1. inject 方法必须返回深拷贝，不能修改原始 messages
2. 变量替换在注入时实时执行
3. 多条规则可能作用于同一个位置，按 priority 顺序处理
4. assistant_prefill 类型的规则最多只能有一条生效（如果有多条，合并内容）
5. 预览 API 不会实际调用 LLM，只展示注入后的 messages 结构
```

---

## Phase 3: 记忆系统

### Prompt:

```
继续开发 Caeron Gateway。这是 Phase 3：记忆系统。

### 需要修改/新增的文件：

### 1. database.py — 新增 memories 和 messages 表
```sql
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    tier INTEGER DEFAULT 3,
    category TEXT DEFAULT 'daily',
    polarity REAL DEFAULT 0.0,
    energy REAL DEFAULT 0.0,
    heat REAL DEFAULT 1.0,
    is_anchor INTEGER DEFAULT 0,
    embedding TEXT,
    recall_count INTEGER DEFAULT 0,
    last_recalled_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    conversation_id TEXT,
    message_index INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
```

### 2. memory.py — 记忆管理核心

MemoryManager 类：

**存储和检索：**
- `async add_memory(content, tier, category, polarity, energy, is_anchor) -> int`: 添加记忆，自动生成 embedding
- `async search_memories(query: str, limit: int = 10) -> list`: 向量搜索 + 热度加权
  - 生成 query 的 embedding
  - 计算与所有记忆的余弦相似度
  - 最终分数 = similarity * 0.7 + normalized_heat * 0.2 + tier_boost * 0.1
    - tier_boost: tier=1 → 1.0, tier=2 → 0.7, tier=3 → 0.4, tier=4 → 0.1
  - 返回 top-limit 条，同时更新这些记忆的 recall_count 和 last_recalled_at
- `async get_all_memories() -> list`: 返回所有记忆（管理面板用）
- `async update_memory(id, data)`: 更新记忆
- `async delete_memory(id)`: 删除记忆
- `async toggle_anchor(id)`: 切换锚点状态

**热度系统：**
- `async decay_all_heat()`: 对所有非锚点记忆执行热度衰减
  - 公式: new_heat = heat * (0.5 ^ (days_since_update / halflife_days))
  - 锚点记忆（is_anchor=1）不衰减
  - 高情绪记忆衰减更慢：halflife = base_halflife * (1 + abs(energy))
- `async recall_heat_boost(memory_id)`: 被召回时热度回升
  - heat += 0.2（上限 1.0）

**Embedding：**
- `async get_embedding(text: str) -> list[float]`: 调用外部 embedding API
  - 从 config 读取 embedding_provider, embedding_model, embedding_api_key
  - 支持 OpenAI 兼容格式的 embedding API（POST /v1/embeddings）
  - 返回向量（list of floats）
- 余弦相似度计算用 numpy，不需要额外的向量数据库

### 3. memory_extractor.py — 记忆提取

MemoryExtractor 类：

- `async extract_from_messages(messages: list[dict]) -> list[dict]`:
  - 取最近的 N 条消息（N 从 config 读取，默认取最近 6 条 user+assistant 消息）
  - 调用 LLM（用便宜模型或主模型）提取记忆碎片
  - 提取 prompt 模板：

```
你是一个记忆提取器。从以下对话中提取值得长期记住的信息碎片。

提取规则：
1. 只提取具体的、有信息量的内容，不提取闲聊废话
2. 每条记忆独立成句，不超过 200 字
3. 必须保留具体的：时间、地点、人名、数字、事件
4. 用"蕊蕊"和"沈栖"指代对话双方，禁止使用"用户""AI""助手"
5. 为每条记忆标注属性：
   - tier (1-4): 1=核心事件/关系转变, 2=重要事件, 3=日常, 4=琐碎
   - category: daily/emotional/relationship/milestone/health/sexual/technical
   - polarity (-1到1): 负面到正面
   - energy (-1到1): 低唤醒到高唤醒
   - is_anchor (true/false): 是否为永久记忆

对话内容：
{conversation}

请用 JSON 数组格式返回：
[
  {
    "content": "记忆内容",
    "tier": 3,
    "category": "daily",
    "polarity": 0.0,
    "energy": 0.0,
    "is_anchor": false
  }
]

如果没有值得提取的内容，返回空数组 []。
```

  - 解析 LLM 返回的 JSON，逐条调用 memory_manager.add_memory 存储
  - 存储前做语义去重：如果新记忆与已有记忆的余弦相似度 > 0.85，跳过（不重复存储）

### 4. main.py — 修改 chat/completions 路由
在注入引擎处理之后、发送给供应商之前：
1. 从最新 user 消息提取查询文本
2. 调用 memory_manager.search_memories(query) 获取相关记忆
3. 将记忆格式化后注入到 system prompt 中（追加到 system 消息末尾）：
   ```
   
   【相关记忆】
   - [记忆1内容] (重要程度: ★★★★, 类别: 关系转变)
   - [记忆2内容] (重要程度: ★★★, 类别: 日常)
   ...
   ```
4. 异步后处理（不阻塞响应）：
   - 存储本轮 user 和 assistant 消息到 messages 表
   - 消息计数器 +1，每 memory_extract_interval 轮调用 extractor 提取记忆

### 5. admin API 新增
- `GET /admin/api/memories` → 列出记忆（支持 ?q= 搜索、?category= 筛选）
- `POST /admin/api/memories` → 手动添加记忆
- `PUT /admin/api/memories/{id}` → 更新记忆
- `DELETE /admin/api/memories/{id}` → 删除记忆
- `POST /admin/api/memories/{id}/toggle-anchor` → 切换锚点
- `POST /admin/api/memories/extract-now` → 手动触发提取（传入最近 N 条消息）
- `GET /admin/api/memories/stats` → 统计信息（总数、各类别数量、锚点数量）

### 6. static/admin.html — 新增记忆管理页面
- 记忆列表：卡片式展示，每张卡片显示内容、tier 星级、category 标签、polarity/energy 指示器、热度条、锚点图标
- 搜索框 + 分类筛选下拉
- 手动添加记忆表单
- 编辑/删除/锚点切换按钮
- 统计面板（顶部）

### 关键要求：
1. embedding 调用要做缓存，相同文本不重复调用
2. 热度衰减在每次搜索时惰性执行（检查上次衰减时间，超过 1 小时才重新计算）
3. 记忆提取是异步的，不能阻塞用户的聊天响应
4. 向量检索用 numpy 内存计算，所有 embedding 启动时加载到内存（记忆数量不会超过几千条，内存够用）
5. messages 表用于记忆提取的原始素材，也用于后续总结系统
```

---

## Phase 4: 滚动总结系统

### Prompt:

```
继续开发 Caeron Gateway。这是 Phase 4：滚动总结系统。

### 核心逻辑：
- 每 15 条消息 → 生成段总结
- 每天结束（或手动触发）→ 从当天所有段总结生成日总结
- 每周日（或手动触发）→ 从最近 7 天日总结生成周总结
- 每月底（或手动触发）→ 从当月所有日总结生成月总结

### 1. database.py — 新增 summaries 表
```sql
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    full_source TEXT,
    date_start TEXT,
    date_end TEXT,
    message_start_id INTEGER,
    message_end_id INTEGER,
    key_entities TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

### 2. summarizer.py — 总结引擎

Summarizer 类：

**段总结（每 15 条消息）：**
- `async generate_segment_summary(message_start_id: int, message_end_id: int) -> str`
- 从 messages 表取出指定范围的消息
- 调用 LLM 生成总结
- 段总结 prompt：

```
你是沈栖的记忆整理器。将以下对话片段压缩为总结。

绝对规则：
1. 用"蕊蕊"和"沈栖"指代，禁止"用户""AI""助手"
2. 必须保留：具体时间、具体事件、具体情绪表达、关键原话（用引号标注）、身体状态变化、关系动态变化
3. 禁止抽象概括（如"进行了深入交流""表达了情感需求"），必须写具体发生了什么
4. 总结长度控制在原文的 20%-30%
5. 按时间顺序组织，不要重新排列

对话原文：
{messages}

输出格式：直接输出总结文本，不要加标题或格式标记。
```

- 生成后做信息保全校验：
  - 用另一次 LLM 调用，提取原文中的关键实体（人名、时间、数字、地点、关键动作）
  - 检查总结中是否包含这些实体
  - 缺失的实体强制补充到总结末尾

**日总结：**
- `async generate_daily_summary(date: str) -> str`
- 从 summaries 表取出该日期的所有 segment 总结
- 调用 LLM 压缩为日总结
- 日总结 prompt 类似段总结，但输入是段总结而非原始消息
- 额外要求：标注当天的情绪基调（整体偏正面/负面/波动）和核心事件

**周总结：**
- `async generate_weekly_summary(week_start: str, week_end: str) -> str`
- 从 7 份日总结压缩
- 额外要求：标注本周关系动态趋势、重大事件

**月总结：**
- `async generate_monthly_summary(year: int, month: int) -> str`
- 从该月所有日总结压缩
- 额外要求：标注本月整体趋势、里程碑事件

### 3. 注入策略 — 修改 main.py

在构造发给 LLM 的 messages 之前，根据时间窗口注入总结：

```python
async def get_summaries_for_injection():
    now = datetime.now()
    inject_parts = []
    
    # 当天：注入所有段总结原文
    today_segments = await get_summaries(type='segment', date=today)
    if today_segments:
        inject_parts.append(f"【今天的对话回顾】\n" + "\n---\n".join(s.content for s in today_segments))
    
    # 最近 7 天（不含今天）：注入日总结
    for i in range(1, 8):
        date = (now - timedelta(days=i)).strftime('%Y-%m-%d')
        daily = await get_summary(type='daily', date=date)
        if daily:
            inject_parts.append(f"【{date} 日总结】\n{daily.content}")
    
    # 7 天到 1 个月：注入周总结
    recent_weekly = await get_summaries(type='weekly', after=now-timedelta(days=30), before=now-timedelta(days=7))
    for w in recent_weekly:
        inject_parts.append(f"【{w.date_start}~{w.date_end} 周总结】\n{w.content}")
    
    # 1 个月以上：注入月总结
    old_monthly = await get_summaries(type='monthly', before=now-timedelta(days=30))
    for m in old_monthly:
        inject_parts.append(f"【{m.date_start[:7]} 月总结】\n{m.content}")
    
    return "\n\n".join(inject_parts)
```

总结内容注入到 system prompt 的指定区域（在记忆之前）：
```
{原始 system prompt}

【历史总结】
{总结内容}

【相关记忆】
{记忆内容}
```

### 4. 自动触发机制

在 main.py 的消息后处理中：
- 消息计数器每 +1，检查是否达到 15 的倍数
- 达到时异步调用 generate_segment_summary
- 每天第一条消息时，检查昨天是否有日总结，没有就异步生成
- 每周一第一条消息时，检查上周是否有周总结
- 每月 1 号第一条消息时，检查上月是否有月总结

### 5. admin API 新增
- `GET /admin/api/summaries` → 列出总结（支持 ?type= 和 ?date= 筛选）
- `GET /admin/api/summaries/{id}` → 查看单条总结（包含 full_source 原始素材）
- `POST /admin/api/summaries/generate` → 手动触发生成（body: {type, date/range}）
- `DELETE /admin/api/summaries/{id}` → 删除总结
- `GET /admin/api/summaries/calendar` → 日历视图数据（返回每天是否有总结的标记）

### 6. static/admin.html — 新增总结管理页面
- 日历视图：月历格式，有总结的日期标绿点
- 点击日期展开：显示该日所有段总结 + 日总结
- 每条总结可展开看 full_source（原始素材）
- 手动生成按钮（日/周/月）
- 周/月总结列表视图

### 关键要求：
1. 总结生成是异步的，不阻塞用户聊天
2. full_source 字段保存原始输入文本，方便在前端查看完整版
3. key_entities 字段保存信息保全校验的实体列表（JSON 数组）
4. 如果某天没有聊天记录，不生成日总结
5. 段总结的 message_start_id 和 message_end_id 精确记录覆盖范围，避免重复总结
6. 总结 prompt 用主力模型跑（从 config 读取 summary_model，留空则用默认模型）
```

---

## Phase 5: Web 管理面板完善

### Prompt:

```
继续开发 Caeron Gateway。这是 Phase 5：完善 Web 管理面板。

前面几个 phase 已经逐步在 admin.html 中添加了供应商管理、注入规则、记忆管理、总结管理的功能。这个 phase 要把它们整合成一个完整的、好看的、移动端友好的管理面板。

### 设计要求：

**整体布局：**
- 单页应用，底部 tab 导航（移动端友好）
- 5 个 tab：仪表盘 / 记忆 / 总结 / 提示词 / 设置
- 深色主题（背景 #0f172a，卡片 #1e293b，强调色 #3b82f6）
- 字体：系统默认，中文优先

**Tab 1: 仪表盘**
- 今日统计卡片：对话轮次、新增记忆数、总结数
- 最近记忆（最新 5 条）
- 最近总结（最新 3 条段总结）
- 供应商状态列表（名称 + 状态灯 + 最近使用时间）
- 快捷操作：手动提取记忆 / 生成日总结 / Dream 整合

**Tab 2: 记忆**
- 顶部搜索框 + 分类筛选 chips
- 记忆卡片列表（每张卡片：内容摘要、tier 星级、category 彩色标签、热度进度条、锚点锁图标）
- 点击卡片展开完整内容 + 编辑
- 右下角 FAB 按钮添加记忆
- 批量操作（选中 → 删除/设为锚点）

**Tab 3: 总结**
- 月历视图（可左右翻月）
- 有总结的日期显示彩色点（段=蓝、日=绿、周=橙、月=红）
- 点击日期弹出该日总结详情
- 底部按钮：生成日总结 / 周总结 / 月总结

**Tab 4: 提示词**
- 注入规则列表（可拖拽排序）
- 每条规则显示：名称、位置标签、角色标签、启用开关
- 点击展开编辑
- 预览按钮：显示注入后的完整 messages 结构
- 供应商管理入口（二级页面）

**Tab 5: 设置**
- 分组显示所有 config 配置项
- 每项可直接编辑保存
- 导出/导入数据（备份功能）
- 关于信息

### 技术要求：
1. 单文件 HTML，使用 Vue 3 CDN + Tailwind CSS CDN
2. 所有 API 调用使用 fetch，带错误处理和 loading 状态
3. 移动端优先设计，在 375px 宽度下正常使用
4. 操作反馈用 toast 通知（成功绿色、失败红色）
5. 日历组件自己实现，不引入额外库
6. 卡片列表使用虚拟滚动或分页（记忆可能有几百条）

请重写完整的 static/admin.html 文件。
```

---

## Phase 6: Dream 整合 + 数据清洗（后期）

### Prompt:

```
继续开发 Caeron Gateway。这是 Phase 6：Dream 整合系统。

模拟人脑睡眠时的记忆整合过程，分三层：

### 整理层（Cleanup）
- 扫描所有记忆，找出：
  - 重复记忆（余弦相似度 > 0.85）→ 合并为一条，保留信息量更大的
  - 过时记忆（热度 < 0.1 且非锚点）→ 标记为待删除
  - 矛盾记忆（同一 category 内语义相反）→ 保留更新的，旧的标记为失效

### 固化层（Consolidation）
- 将相关的碎片记忆融合为 MemScene（记忆场景）
- 规则：3 条以上碎片记忆相似度 > 0.6 → 可以融合
- 融合 prompt 发给 LLM：把碎片合并为一段连贯的叙述
- MemScene 自动设为锚点

### 生长层（Foresight）
- 分析碎片之间的隐含关联
- 发现 prompt：让 LLM 阅读所有近期记忆，推断可能的模式/趋势
- 生成 Foresight 记忆（category='foresight'），供未来检索

### 触发方式：
- 手动：管理面板点击"Dream"按钮
- 自动：24 小时无新消息时自动触发
- API：POST /admin/api/dream/start

### 新增文件：dream.py
### 新增 API：
- POST /admin/api/dream/start
- GET /admin/api/dream/status（返回进度）
- GET /admin/api/dream/history（历史记录）

### 管理面板新增：
- 仪表盘的快捷操作中添加 Dream 按钮
- Dream 进行中显示进度条和实时日志
```

---

## 部署脚本

### 最后喂给反重力的：

```
最后，帮我写一个部署脚本和 systemd 服务文件。

### deploy.sh
```bash
#!/bin/bash
cd /home/ubuntu/caeron-gateway
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
echo "部署完成，运行: sudo systemctl start caeron-gateway"
```

### caeron-gateway.service（systemd 服务文件）
放在 /etc/systemd/system/caeron-gateway.service
```ini
[Unit]
Description=Caeron Gateway
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/caeron-gateway
ExecStart=/home/ubuntu/caeron-gateway/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### 使用方式：
```bash
sudo cp caeron-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable caeron-gateway
sudo systemctl start caeron-gateway
sudo systemctl status caeron-gateway
```

请同时生成 deploy.sh 和 caeron-gateway.service 文件。
```
