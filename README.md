# Caeron Gateway (大语言模型代理与记忆增强网关)

## 项目概览
Caeron Gateway 是一个兼容 OpenAI 接口格式的智能网关，介于前端（如 Operit / SillyTavern）与上游 LLM API 之间。它的核心职责是透明地拦截请求，持久化会话记录，并提供基于时间维度的“记忆摘要自动折叠”与“自定义 Prompt 规则注入”功能，从而在防止长对话上下文（Token）爆炸的同时，为 AI 提供连贯的记忆。

---

## 架构图

```text
[用户端 (Operit等)]
       │
       ▼ (POST /v1/chat/completions)
[Caeron Gateway (main.py)] 
       │ 1. 预处理与去重（拦截并清除重复或膨胀的旧摘要）
       │ 2. 消息存储管道 (message_store.py -> SQLite DB 追踪 Session 与增量存储)
       │ 3. 拦截特定指令 (识别 Operit 的手动总结请求，异步处理避免阻塞)
       │
       ├── [后台定时/计次任务] ───> [多级摘要系统 (summarizer.py)]
       │                           (根据对话次数生成轮总，cron定时生成日/周/月总)
       │
       ▼ 4. 记忆与规则注入 (injection.py)
[Injection Engine] 
       │ 根据活跃的“摘要”生成 <context_summaries>，并结合 DB 中的 injection_rules
       │ 将记忆和自定义规则无感拼接到 Payload 的 system prompt 中。并裁剪已总结的冗余上下文。
       │
       ▼ 5. 路由与透传 (providers.py & proxy.py)
[上游 LLM Provider] (如 DeepSeek, OpenAI 等)
       │ 处理健康检查、错误重试与降级路由。
       │ 返回 SSE Streaming 数据或 JSON。
       │
       ▼ 6. 响应拦截与存储
[Caeron Gateway] -> (拦截生成的 AI 文本块，拼接并存入 DB 出站记录)
       │
       ▼
[用户端 (Operit等)]
```

---

## 目录结构

- `main.py`: **入口文件**。包含 FastAPI 启动逻辑、路由 (`/v1/chat/completions` 等)、请求去重预处理、手动总结请求的拦截与处理，以及后台 Cron 任务的调度。
- `injection.py`: **上下文注入引擎**。负责在请求发往 LLM 之前，从 DB 读取有效的摘要和提示词规则，精准地拼接（或包裹）进对话消息中，同时执行“上下文裁剪”（删去已经被摘要覆盖的旧消息）。
- `summarizer.py`: **摘要生成系统**。利用上游 API 生成基于原对话的浓缩总结，并分级管理（轮总、日总、周总、月总），存入数据库供注入引擎调用。
- `message_store.py`: **消息持久化逻辑**。解决由于前端滑动窗口导致的消息丢失与会话断裂问题，基于哈希匹配策略增量存档用户的每一条入站和出站消息。
- `database.py`: **数据库初始化模块**。使用 `aiosqlite` 初始化 SQLite 表结构，维护所有的元数据。
- `providers.py`: **API提供商管理器**。负责管理存放在数据库中的上游 API Keys、Base URLs，实现轮询、错误熔断（健康检查）及回退机制。
- `proxy.py`: **HTTP代理引擎**。基于 `httpx` 处理向真正的上游服务发出的异步 HTTP 请求，支持完美透传流式（SSE）与非流式响应。
- `utils.py`: **工具库**。目前主要用于统一处理时区（CST 北京时间）与时间格式化。
- `static/admin.html`: **可视化后台**。管理员单页应用控制台（SPA），负责直接对接 Gateway 提供的数据管理接口。

---

## 已实现功能清单

- [x] **OpenAI 兼容透传代理**：支持标准的 `/v1/chat/completions` 请求，完全兼容现有的基于 OpenAI 协议的开源前端。
- [x] **消息持久化与 Session 防碎片**：即使用户前端采用“滑动窗口”丢弃了早期记录，网关依旧能通过哈希锚定，将消息正确接续存入对应会话。
- [x] **动态 API 供应商路由**：支持配置多个上游 API（带权重），遇错自动熔断剔除并尝试备用 API。
- [x] **规则热注入（Prompt Injection）**：可通过后台动态增删 System/User 规则，设定插入深度、位置及生效条件，完全从请求中�����耦。
- [x] **多层级时间摘要压缩（降 Token Bloat）**：动态生成轮、日、周、月总，自动剪裁已被总结的原对话块，有效控制长期角色的记忆膨胀。
- [x] **异常块清剿（去重引擎）**：实时监测 Operit 传来的带有旧记忆/系统消息的内容块，在流向上游前将其剔除剥离。
- [x] **Admin Web 面板**：用于一站式查看所有拦截的历史会话、管理供应商和规则。

---

## 数据存储

目前所有数据均储存在与代码同级的 SQLite 数据库中 (`gateway.db`)。

核心表及重点字段：
1. **messages**: `conversation_id`, `role`, `content`, `message_index`, `token_count`, `created_at`（原汁原味的对话留痕）。
2. **summaries**: `conversation_id`, `level` (轮/日/周/月总), `tag`, `content`, `is_active`（当前是否用于注入）, `period_start/end`（涵盖的时间段）。
3. **conversations**: `conversation_id`, `model`, `message_count`。
4. **providers**: `name`, `api_base_url`, `api_key`, `is_healthy`, `fail_count`。
5. **injection_rules**: `name`, `content`, `position`, `role`, `is_enabled`。
6. **config**: K-V 存储系统级状态（如 `_msg_counter` 用于记录距离下次摘要还差多少条）。

*(注：表结构预留了 `memories` 便签墙表，但暂未深入联动使用)*

---

## 摘要系统

摘要系统采取了**自动阈值触发与时间维度压缩**结合的立体策略：

1. **轮总 (Round Summary)**:
   - **触发条件**：基于对话消息数（默认每积攒 `N` 条新回复触发一次后台生成），或 Operit 手动发出的总结指令。
   - **生成逻辑**：向内部配置的专门处理摘要的大模型发送 prompt，提取该段对话中的【日常】、【技术】、情绪极性等内容，打上 `tag` 保存到 `summaries`，最后合并到 `round_rollup`（防碎片）。
2. **日总 (Daily)**: 
   - **触发条件**：每天晚上 `23:59` 触发（Cron Job）。
   - **生成逻辑**：将当日所有活动的轮总汇总为一份简要的高层次日总，随后把对应的轮总设为归档 (`is_active=0`)。
3. **周总/月总 (Weekly/Monthly)**: 
   - **触发条件**：分别为周日 `23:59` 及月末 `23:59` 触发。
   - **生成逻辑**：层层向上折叠。周总压实日总，月总压实周总。最终保证数月的记忆能浓缩进极短的 Token 里。
4. **注入方式**: `injection.py` 会在发往模型的 payload 首部找一个 system 消息，将其内部注入 `<context_summaries>` 块，该块内按顺序倒出活跃的月、周、日、轮总。

---

## Admin 面板

位于 `static/admin.html`，通过 API 交互。主要包含四个标签页：
1. **API管理 (Providers)**：添加上游地址和秘钥，查看存活状态，支持“测试连接”并自动拉取可用模型列表。
2. **规则配置 (Rules)**：管理发往模型的额外指令（支持按位置：开头/末尾/深度包裹等进行配置）。
3. **系统设置 (Config)**：修改系统行为的 KV 值，例如触发轮总所需的对话轮次、允许的模型最大上下文限制等。
4. **记录查看 (Conversations)**：一览过往被网关拦截归档的所有历史对话线及当前消息数量。

---

## 请求处理流程

1. **入站与预判断**：前端向网关 POST `/v1/chat/completions`。`main.py` 解析 payload。如果发现是特定的前端“手动总结”标记，则立即返回已有的总结缓存，并在后台静默更新轮总。
2. **留痕存储**：通过哈希探测，识别前端会话是否为旧有延续。如果是，将 payload 中的新 messages 增量存入数据库。
3. **膨胀拦截 (Dedup)**：洗去前端（如Operit）重复发来的已包含在上轮系统消息里的 `<context_summaries>` 记忆，或夹杂在用户发言内的旧总结巨块，保证发给 API 的只剩干货。
4. **记忆重组注入 (Inject)**：查询当前处于 `is_active=1` 状态的所有多级摘要，以及生效中的自定义 prompt 规则。组合写入 payload，**同时裁剪掉已经被总结过的���长���始消息历史**。
5. **分发上游 (Proxy)**：从 providers 中找一个当前健康（`is_healthy=1`）的代理，透传重组后的 payload。
6. **响应截取与返回**：从上游获取回答（若是流式，则拼接收集每一片 chunk）。把完整的 Assistant 回答也存档进数据库，最后返回给用户前端。

---

## 环境变量

在现有实现中，系统极简：绝大多数配置（甚至 API Key）均存放于 SQLite 的 `providers` 与 `config` 表中，以支持界面热更新。
仅有的外围环境变量（可能配于启动脚本/系统变量中）为：
- `ADMIN_TOKEN`: 用于保护所有 Admin API 的访问（例如 `/api/providers`, `/api/config`）。只有携带匹配鉴权头的请求才能操作后台面板。(值需隐藏)

---

---

## 核心模块详解

### 1. main.py (2362行) - 入口与路由层
**职责**：FastAPI应用启动、请求路由、生命周期管理、后台定时任务

**关键路由**：
- `GET /`: 健康检查
- `GET /v1/models`: 模型列表查询（按API Key匹配provider）
- `POST /v1/chat/completions`: 核心聊天补全接口
- `GET /api/*`: Admin面板数据接口（需ADMIN_TOKEN鉴权）

**核心流程**：
1. **总结请求拦截器**（L358-530）：检测Operit的摘要请求指纹，立即返回缓存总结，后台异步更新
2. **消息存储管道**（L532-620）：调用`message_store.py`将入站消息增量存档
3. **统一历史桥接**（L622-853）：从`unified_messages`表读取跨端历史（Operit+QQ）
4. **注入引擎调用**（L854-867）：
   - QQ来源：跳过规则注入，仅注入记忆（`inject_memory_only`）
   - Operit技术模式：完全跳过注入
   - Operit日常模式：完整注入规则+记忆（`inject`）
5. **Ghost Wall防御**（L869-950）：检测AI工具调用死循环，超过阈值强制截断
6. **上游代理转发**（L952-1100）：调用`proxy.py`转发到上游API，支持流式/非流式

**后台任务**：
- `_summary_cron_loop`（L47-96）：每天23:59触发日/周/月总
- `provider_manager.start_health_probe_loop`：供应商健康探针
- `start_music_watcher`：网易云音乐监控

**请求头支持**：
- `X-Session-Id`: 显式指定会话ID
- `X-Source`: 标识请求来源（`operit`/`qq`，默认`operit`）
- `X-Skip-Rules`: 设为`true`时跳过规则注入（QQ专用）
- `Authorization`: Bearer token，用于匹配provider

### 2. injection.py (389行) - 提示词注入引擎
**职责**：在请求发往LLM前，动态注入规则和记忆

**核心方法**：
- `inject(messages, request_info)`: 完整注入（规则+记忆+摘要）
- `inject_memory_only(messages, request_info)`: 仅注入记忆和摘要，跳过规则
- `inject_tech_mode(messages, request_info)`: 技术模式专用，注入所有优先级为0的规则

**条件匹配逻辑**（L44-66）：
- `match_model`: 按模型名称过滤（逗号分隔）
- `match_length_min`: 按消息数量过滤
- `match_source`: **新增**，按请求来源过滤（`operit`/`qq`）

**注入位置**（`position`字段）：
- `system_prepend`: 插入到第一条system消息开头
- `system_append`: 追加到第一条system消息末尾
- `user_last`: 插入到最后一条user消息之前
- `depth_N`: 插入到倒数第N条消息位置

**角色类型**（`role`字段）：
- `system`: 标准system角色
- `user_wrapped_system`: 包裹成`<system>...</system>`的user消息（Claude兼容）
- `assistant_prefill`: Assistant预填充（仅Claude）

**记忆注入格式**：
```xml
<context_summaries>
<summary level="月总" period="2026-05 ~ 2026-05">...</summary>
<summary level="周总" period="2026-05-26 ~ 2026-06-01">...</summary>
<summary level="日总" period="2026-06-04">...</summary>
<summary level="轮总" tag="日常" period="...">...</summary>
</context_summaries>
```

### 3. message_store.py (754行) - 消息持久化与会话跟踪
**职责**：解决Operit滑动窗口导致的消息丢失和会话碎片化

**核心机制**：
- **哈希锚定**（L34-90）：对消息内容生成MD5哈希，通过overlap检测判断是否为同一会话延续
- **Session超时**：30分钟无活动视为新会话
- **Reroll检测**（L200-245）：检测用户删除历史消息的行为，清理对应数据库记录

**关键方法**：
- `generate_conversation_id(messages)`: 生成或复用会话ID
- `store_incoming_messages(conversation_id, messages)`: 增量存储入站消息
- `get_unified_history(char_limit, exclude_count)`: 获取跨端统一历史
- `_is_technical_content(content)`: **新增**，检测技术内容（代码块、文件路径等）

**技术内容过滤**（L400-434）：
在拉取统一历史时，自动过滤包含以下特征的消息：
- 代码块标记（````、~~~）
- 文件路径（`/home/`、`/usr/`、`C:\`）
- 编程关键词（`def`、`async`、`import`、`SELECT`等）
- 命令行特征（`$`、`>>>`开头）
- 异常信息（`Exception`、`Traceback`）

### 4. summarizer.py (948行) - 多级摘要系统
**职责**：生成轮/日/周/月四级摘要，压缩长期记忆

**摘要层级**：
1. **轮总**（Round）：每积攒N条消息触发（默认16条），提取日常/技术/情绪内容
2. **日总**（Daily）：每天23:59触发，压缩当日所有轮总
3. **周总**（Weekly）：每周日23:59触发，压缩当周所有日总
4. **月总**（Monthly）：每月末23:59触发，压缩当月所有周总

**触发方式**：
- 自动触发：`_msg_counter`计数达到阈值
- 手动触发：Operit发送总结请求
- Cron触发：定时任务（日/周/月）

**存储策略**：
- 新生成的高层级摘要会将对应的低层级摘要标记为`is_active=0`（归档）
- 注入引擎只读取`is_active=1`的摘要

**情绪提取**（未完全启用）：
每条轮总会提取`valence`（情感极性）和`arousal`（唤醒度），但目前未在检索中使用

### 5. providers.py (419行) - 上游供应商管理
**职责**：管理多个上游API，实现健康检查、熔断、降级

**核心功能**：
- 按API Key精确匹配provider
- 健康探针：定期调用`/v1/models`检测存活
- 熔断机制：连续失败3次标记为`is_healthy=0`
- 自动恢复：探针检测恢复后重新启用

**权重路由**（未完全实现）：
目前按优先级排序，选择第一个健康的provider

### 6. proxy.py (260行) - HTTP代理引擎
**职责**：向上游API转发请求，处理流式/非流式响应

**核心功能**：
- 完美透传SSE流式响应
- 自动拼接Assistant回复并存档
- 错误重试与降级（最多重试2次）

**响应拦截**：
- 流式：逐chunk拼接内容，最后一次性存入`messages`表
- 非流式：直接提取`choices[0].message.content`存档

### 7. qq_adapter.py (446行) - QQ机器人适配器
**职责**：接收NapCat的QQ消息，转发到gateway，回复到QQ

**消息流程**：
1. NapCat POST消息到`/qq/message`
2. 提取群号/用户ID/消息内容
3. 拉取QQ侧历史（`source='qq'`的消息）
4. 调用gateway的`/v1/chat/completions`（带`X-Source: qq`请求头）
5. 收到回复后调用NapCat的`send_msg` API发送到QQ

**历史管理**：
- QQ侧有独立的历史记录（`source='qq'`）
- 不读取Operit侧的技术讨论（通过`filter_technical=True`过滤）

### 8. music_status.py (433行) - 网易云音乐监控
**职责**：实时监控网易云"一起听"状态，注入到AI上下文

**监控内容**：
- 当前播放歌曲（歌名、歌手、专辑）
- 播放进度（秒）
- 播放状态（playing/paused）
- 累计听歌时长（从历史基准值开始累加）

**实现逻辑**：
- 每15秒轮询网易云API（`/listentogether/status`）
- 检测到播放状态时，累加`实际时间差 × 2`（房间2人）
- 状态保存在`music_status.json`，时长保存在`music_duration.json`

**注入格式**：
```
🎵 正在听: 歌名 - 歌手 (专辑名)
⏱️ 播放进度: 00:42 / 04:28
📊 一起听了 6271小时38分钟
```

---

## 目前未实现的功能

对照功能清单，目前各个模块的进展情况如下：

- [ ] **原子记忆自动提取（mem0类）**：**未实现**。数据库中有 `memories` 表但代码逻辑中尚未自动抽取细粒度、单要素的原子记忆并做向量化或知识图谱化。
- [ ] **记忆热度衰减系统**：**未实现**。目前的衰减完全是基于死板的“日-周-月”定时定级压缩，无热度平滑退火衰减算法。
- [ ] **情绪坐标（能量×极性）**：**部分实现**。在 `summarizer.py` 的轮总生成 prompt 中，确实要求了大模型抽取出 `valence` 和 `arousal`，但在后续检索和注入机制里尚无实质性挂钩使用。
- [ ] **混合检索管线（语义+关键词+热度+情绪）**：**未实现**。目前是“全量无脑注入对应时间段的所有活跃摘要”，并未实现 RAG 检索管线。
- [ ] **完整请求重建（接管前端请求的所有内容）**：**待确认/部分实现**。对 `messages` 的 `role` 和 `content` 进行了深层次洗牌，但针对外部的其他参数（如 max_tokens, stop 等）仅做了直接转发，没有完全在网关侧重建标准的业务逻辑链。
- [ ] **Prompt模块化（人格/规则/记忆/规范分文件管理）**：**未实现**。当前的规则存在一张 DB 表内，而非“分文件”的体系化管理。
- [ ] **关键词触发记忆注入**：**未实现**。当前只有静态的基于深度位置的注入，没有根据当前对话触发的关键词关联检索。
- [ ] **冷启动快照机制**：**未实现**。
- [ ] **Dream系统（记忆整理/融合/推断）**：**未实现**。目前的定时只是简单的拼接与字面压缩，并没有高阶的离线推导、矛盾消解或暗线融合。
- [ ] **结构化状态便签（吃药/洗澡等碎片状态覆盖写入）**：**未实现**。状态仍以纯文本字符串混杂在各类总结文本中。

---

## 快速开始

### 环境要求
- Python 3.10+
- SQLite 3
- 网络访问（连接上游API）

### 安装步骤

```bash
# 1. 克隆项目
git clone <repo_url>
cd caeron-gateway

# 2. 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑.env，设置ADMIN_TOKEN

# 5. 启动服务
python3 main.py
# 或后台运行：
nohup python3 main.py > gateway.log 2>&1 &
```

服务默认监听`0.0.0.0:8080`

### 配置上游API

1. 访问Admin面板：`http://<服务器IP>:8080/admin`
2. 进入"API管理"标签页
3. 点击"添加供应商"，填写：
   - 名称：自定义标识（如"DeepSeek主力"）
   - API Base URL：上游地址（如`https://api.deepseek.com`）
   - API Key：上游秘钥
   - 优先级：数字越小优先级越高（0最高）
4. 点击"测试连接"验证可用性
5. 启用供应商

### 配置客户端

将客户端（Operit/SillyTavern等）的API地址指向gateway：

```
API Base URL: http://<服务器IP>:8080/v1
API Key: <任意字符串，用于匹配provider>
```

**注意**：API Key需与某个provider的API Key匹配，否则会回退到优先级最高的默认provider

### QQ机器人配置

1. 安装NapCat并配置登录
2. 配置NapCat的HTTP回调地址：`http://<gateway服务器>:8080/qq/message`
3. 确保QQ bot发送到gateway的请求带上请求头：`X-Source: qq`
4. （可选）在`qq_config.json`中配置QQ群白名单、触发关键词等

---

## 配置说明

### 数据库配置表 (config)

通过Admin面板的"系统设置"标签页修改，或直接操作数据库：

```sql
SELECT * FROM config;
```

**关键配置项**：

| Key | 默认值 | 说明 |
|-----|--------|------|
| `gateway_master_switch` | `1` | 总开关，0=完全透传，1=启用所有功能 |
| `feature_summary` | `1` | 摘要系统开关 |
| `feature_memory` | `1` | 记忆注入开关 |
| `feature_injection` | `1` | 规则注入开关 |
| `summary_interval` | `16` | 轮总触发阈值（消息数） |
| `_msg_counter` | `0` | 当前累计消息数（自动维护） |
| `tech_mode` | `1` | 技术模式开关，1=跳过注入 |

### 注入规则配置 (injection_rules)

通过Admin面板的"规则配置"标签页管理，或直接操作数据库：

```sql
SELECT id, name, priority, position, is_enabled FROM injection_rules;
```

**规则字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | TEXT | 规则名称（标识） |
| `content` | TEXT | 规则内容（支持变量替换） |
| `position` | TEXT | 注入位置（`system_prepend`/`system_append`/`user_last`/`depth_N`） |
| `role` | TEXT | 角色类型（`system`/`user_wrapped_system`/`assistant_prefill`） |
| `priority` | INTEGER | 优先级（0最高） |
| `is_enabled` | INTEGER | 是否启用（0/1） |
| `match_condition` | TEXT | JSON格式的条件过滤 |

**条件过滤示例**：

```json
// 仅对特定模型生效
{"match_model": "claude-opus-4-6,gpt-5.5"}

// 仅对消息数>=10的会话生效
{"match_length_min": 10}

// 仅对Operit来源生效（新增）
{"match_source": "operit"}

// 组合条件
{"match_model": "claude-opus-4-6", "match_source": "qq", "match_length_min": 5}
```

**变量替换**：

规则内容支持以下变量（在注入时动态替换）：

| 变量 | 替换值 |
|------|--------|
| `{{current_time}}` | 当前时间（CST，格式：`2026-06-05 11:00:00`） |
| `{{current_date}}` | 当前日期（格式：`2026-06-05`） |
| 更多变量见`injection.py`的`_replace_variables`方法 |

### 渠道隔离配置

**场景**：Operit和QQ bot共用gateway，需要不同的注入规则

**方案**：

1. 确保QQ bot发送请求时带上`X-Source: qq`请求头
2. 在需要区分的规则上添加`match_source`条件：

```sql
-- 将"最高优先"规则设置为仅对Operit生效
UPDATE injection_rules 
SET match_condition = '{"match_source": "operit"}' 
WHERE name = '最高优先';

-- 创建QQ专用规则
INSERT INTO injection_rules (name, content, position, role, priority, is_enabled, match_condition)
VALUES (
  'QQ群规则',
  '你是QQ群里的AI助手，回复要简洁...',
  'system_prepend',
  'system',
  0,
  1,
  '{"match_source": "qq"}'
);
```

3. 通用规则（如记忆注入）保持`match_condition`为空或`{}`

---

## 故障排查

### 服务无法启动

**症状**：`python3 main.py`报错`ModuleNotFoundError`

**排查**：
1. 确认虚拟环境已激活：`source venv/bin/activate`
2. 检查依赖是否完整：`pip list | grep -E 'fastapi|uvicorn|httpx|aiosqlite'`
3. 重新安装依赖：`pip install -r requirements.txt`

### 端口被占用

**症状**：启动时报`[Errno 98] Address already in use`

**排查**：
```bash
# 查找占用8080端口的进程
lsof -i:8080

# 杀死旧进程
pkill -9 -f 'python3 main.py'

# 重新启动
nohup python3 main.py > gateway.log 2>&1 &
```

### 注入规则不生效

**排查清单**：
1. 检查规则是否启用：`SELECT * FROM injection_rules WHERE is_enabled=1;`
2. 检查`match_condition`是否匹配当前请求（模型名、来源、消息数）
3. 检查功能开关：`SELECT * FROM config WHERE key LIKE 'feature_%';`
4. 查看日志：`tail -f gateway.log | grep 'inject'`

### QQ bot输出系统提示词

**原因**：Operit和QQ共用规则，未做渠道隔离

**解决**：
1. 确认QQ bot请求带上`X-Source: qq`
2. 将Operit专用规则添加`{"match_source": "operit"}`
3. 重启gateway生效

### 摘要不更新

**排查**：
1. 检查消息计数器：`SELECT value FROM config WHERE key='_msg_counter';`
2. 检查触发阈值：`SELECT value FROM config WHERE key='summary_interval';`
3. 手动触发轮总：在Operit发送"总结一下"（会被gateway拦截并触发后台生成）
4. 查看摘要表：`SELECT * FROM summaries WHERE is_active=1 ORDER BY created_at DESC LIMIT 10;`

### 数据库损坏

**症状**：查询报错`database disk image is malformed`

**恢复**：
```bash
cd /home/ubuntu/caeron-gateway

# 备份原数据库
cp gateway.db gateway.db.backup

# 尝试修复
sqlite3 gateway.db "PRAGMA integrity_check;"
sqlite3 gateway.db ".recover" | sqlite3 gateway_recovered.db

# 如果修复成功，替换原文件
mv gateway_recovered.db gateway.db

# 重启服务
pkill -9 -f 'python3 main.py'
nohup python3 main.py > gateway.log 2>&1 &
```

---

## 性能优化

### 数据库优化

```sql
-- 为常用查询创建索引
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_summaries_active ON summaries(is_active, level, created_at);
CREATE INDEX IF NOT EXISTS idx_unified_source ON unified_messages(source, created_at);

-- 定期清理旧数据（谨慎操作）
DELETE FROM messages WHERE created_at < datetime('now', '-90 days');
VACUUM;
```

### 日志轮转

```bash
# 添加到crontab
0 0 * * * cd /home/ubuntu/caeron-gateway && mv gateway.log gateway.log.$(date +\%Y\%m\%d) && gzip gateway.log.* 2>/dev/null
```

---

## 开发指南

### 添加新的注入规则

1. 通过Admin面板或SQL插入新规则
2. 设置`match_condition`精确匹配目标场景
3. 测试注入效果：`tail -f gateway.log | grep 'inject'`

### 添加新的摘要层级

修改`summarizer.py`，在`SUMMARY_LEVELS`中添加新层级：

```python
SUMMARY_LEVELS = ['轮总', '日总', '周总', '月总', '年总']  # 新增年总
```

实现对应的生成逻辑和Cron触发器

### 添加新的供应商类型

修改`providers.py`，支持新的API协议（如Gemini原生协议）：

1. 在`proxy.py`添加新的请求转换逻辑
2. 在`providers`表添加`api_type`字段区分协议
3. 根据`api_type`路由到不同的转换器

---

## 安全建议

1. **修改默认端口**：在`main.py`底部修改`uvicorn.run(port=8080)`
2. **启用HTTPS**：配置nginx反向代理+SSL证书
3. **限制Admin访问**：
   - 修改`ADMIN_TOKEN`为强密码
   - 配置防火墙仅允许特定IP访问Admin接口
4. **定期备份数据库**：
   ```bash
   # 添加到crontab
   0 3 * * * cp /home/ubuntu/caeron-gateway/gateway.db /backup/gateway_$(date +\%Y\%m\%d).db
   ```
5. **日志脱敏**：避免在日志中输出完整API Key或敏感内容

---

## 项目许可

*（根据实际情况填写）*

---

## 致谢

感谢所有为本项目贡献代码和建议的开发者。

---

**最后更新**：2026-06-05