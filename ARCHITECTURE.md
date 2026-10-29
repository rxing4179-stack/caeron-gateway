# Caeron Gateway 架构文档（代码地图）

> 生成时间：2026-06-07
> 总代码量：~7800行（13个核心文件）
> 技术栈：Python 3 + FastAPI + SQLite + httpx

## 一、系统总览

网关是一个**智能中转服务器**，架在蕊蕊的前端（Operit/QQ/未来的Caeron Home）和上游AI API（Claude/Deepseek/GPT等）之间。

它不只是转发请求——它会在请求经过时做五件事：
1. **注入**：往消息里塞身份规则、记忆、轮总摘要、音乐状态
2. **裁剪**：把已被总结覆盖的旧消息裁掉，防止token爆炸
3. **存储**：把所有对话存入数据库，跨端桥接
4. **调度**：选最合适的API供应商转发
5. **后台任务**：自动轮总、记忆提取、音乐监控

## 二、核心文件职责

### main.py（2383行）— 邮局前台

**职责**：所有HTTP路由的入口，请求处理主流程，admin后台API。

**关键路由**：
- `POST /v1/chat/completions` — 核心！Operit/Caeron Home的聊天请求入口
- `GET/POST /admin/api/*` — 后台管理API（供应商/规则/配置/记忆/轮总）
- `POST /admin/api/tech-mode` — 技术模式切换
- `POST /admin/api/trigger-round-summary` — 手动触发轮总
- `GET /api/music/status` — 音乐状态API
- `WebSocket /onebot/ws` — QQ Bot的WebSocket连接

**核心请求处理流程（/v1/chat/completions）**：
1. 解析请求，提取消息列表、模型名
2. MIME修正（修复octet-stream图片）
3. 判断来源（Operit/QQ/Caeron Home）
4. 存储用户消息到message_store
5. 根据模式选择注入策略：
   - 日常模式：完整注入（规则+记忆+轮总+语义召回+音乐）+ 裁剪
   - 技术模式：核心规则+轮总（跳过音乐、跳过裁剪）
   - QQ来源：只注入记忆（跳过规则，QQ有自己的prompt）
6. Ghost Wall检测（技术模式跳过）
7. 选择供应商，转发到上游API（5次retry同一供应商，不做fallback路由）
8. 收到回复后存储，递增消息计数器
9. 计数器达阈值时后台触发轮总

**关键行为变更（06-06~06-07）**：
- tech_mode持久化到config表（L47-55），重启不丢
- 健康探针已关闭（L125-128）
- System Action Required催促语技术模式跳过（L950-952）
- fallback路由关闭，retry同一供应商（L983-1009）
- Ghost Wall技术模式豁免（L914）

---

### injection.py（593行）— 注入+裁剪+召回引擎

**职责**：往消息列表里插入各种上下文信息，裁剪旧消息，召回相关记忆。

**三种注入模式**：
| 方法 | 用途 | 注入内容 |
|------|------|----------|
| `inject()` | 日常模式Operit | 身份规则+记忆+轮总+语义召回+音乐+裁剪 |
| `inject_memory_only()` | QQ | 只轮总（QQ有自己的身份prompt） |
| `inject_tech_mode()` | 技术模式Operit | priority=0规则+轮总（跳过音乐和裁剪） |

**内部方法**：
- `_inject_music_status()` — 读music_status.json注入当前播放状态（含歌词/热评/累计时长/风格标签）
- `_inject_round_summaries()` — 从summaries表拉多级总结（月总/周总/日总/轮总）+ 语义召回 + 裁剪
- `_replace_variables()` — 替换规则模板里的变量占位符

**语义召回流程（L415-530）**：
1. 提取最后一条用户纯文本消息（去除attachment标签）
2. 调用`get_embedding()`生成向量
3. 碎片召回：`memory_fragments`表，余弦相似度>0.45，加权激活衰减，top5
4. 原文召回：`memories`表，余弦相似度>0.5，排除碎片已溯源的，top3
5. 递增碎片`activation_count`
6. 注入格式：`【脑海中闪回的旧日记忆碎片】` / `【脑海中闪回的旧日历史对话】`

**裁剪流程（L541-575）**：
1. 分离system消息和对话消息
2. 固定保留最后50条对话消息（不再依赖_msg_counter，该值在技术模式下会累积到几百导致保留阈值形同虚设）
3. 保留所有system消息（规则/轮总/召回/音乐不裁）
4. 日志：`[记忆裁剪-入口]`/`[记忆裁剪-出口]`

**数据来源**：
- 身份规则：`injection_rules`表（priority/position/role/content）
- 轮总：`summaries`表（月总/周总/日总/轮总四级）
- 音乐：`music_status.json`文件 + `music_duration.json`
- 记忆碎片：`memory_fragments`表（带embedding向量）
- 原文记忆：`memories`表（带embedding向量）

---

### summarizer.py（965行）— 轮总引擎

**职责**：把大段对话压缩成摘要，供后续注入使用。

**关键方法**：
- `generate_round_summary(max_messages_override=None)` — 生成一条轮总
- `_get_global_messages(since_summary, max_messages_override)` — 从messages+unified_messages合并拉取消息
- `_get_latest_active_round_summary()` — 获取最近一条有效轮总

**工作流程**：
1. 检查并发锁（防幽灵轮总）
2. 3分钟去重守卫
3. 拉取上次轮总之后的所有消息（messages表+unified_messages表合并）
4. 截取最后N条（默认100，切回日常模式时可覆盖为2000）
5. 格式化消息（处理多模态/attachment/工具调用标签）
6. 用DeepSeek V3.2生成摘要
7. 存入summaries表

**关键变更（06-06~06-07）**：
- `_get_global_messages`新增`max_messages_override`参数（覆盖默认100条上限）
- 合并messages+unified_messages(source=operit)，覆盖技术模式期间的对话

---

### message_store.py（762行）— 消息存储

**职责**：管理两张消息表，处理消息的存取和跨端桥接。

**两张核心表**：
- `messages` — 正式对话记录，轮总从这里拉，日常模式存这里
- `unified_messages` — 统一消息流，所有来源（Operit/QQ）都存一份，用于跨端桥接

**关键函数**：
- `store_incoming_messages()` — 存用户消息到messages表
- `store_unified_message()` — 存到unified_messages表（跨端桥接用）
- `store_assistant_response()` — 存AI回复（由proxy.py调用）
- `get_unified_history()` — 拉统一历史（支持`source_filter`按来源过滤）
- `_is_technical_content()` — 判断消息是否为技术内容（用于QQ过滤）

**技术模式下的存储行为**：
- 用户消息：只存unified_messages，不存messages（避免触发轮总）
- AI回复：同上
- 计数器照常递增（让前端看到"已存X条待总结"）

---

### proxy.py（260行）— 请求转发

**职责**：拿到注入完的消息后，真正发给上游API。

**功能**：
- `proxy_chat_completion()` — 主转发函数
- `_proxy_stream()` — 流式转发（SSE）
- `_proxy_json()` — 非流式转发
- `clean_system_prompt()` — 清洗上游平台注入的预设（白名单保护用户角色卡）
- 存储`last_request.json`供调试
- 流结束后自动调`store_assistant_response`存档

---

### providers.py（419行）— 供应商管理

**职责**：多API供应商路由、优先级选择。

**核心类：ProviderManager**
- `get_provider(model)` — 根据模型名选供应商（精确匹配→通配→含不健康的）
- `mark_healthy/mark_unhealthy` — 标记健康状态
- `update_last_used` — 更新使用时间
- 后台健康探针：已关闭（06-06）
- fallback路由：已关闭（06-06），5次retry同一供应商

---

### qq_adapter.py（462行）— QQ Bot适配器

**职责**：处理QQ群/私聊消息，组装请求，切分回复。

**流程**：
1. NapCat通过WebSocket推来QQ消息
2. 判断是群聊还是私聊、是否蕊蕊
3. 解析消息（含图片下载转base64）
4. 拉QQ历史（source_filter=qq）+ Operit跨端桥接（source_filter=operit，加身份锚定提示）
5. 用inject_memory_only注入记忆/轮总
6. 发给网关API
7. 收到回复后strip_thinking去掉思考标签
8. 正则去掉时间戳
9. 按句号/问号/感叹号切分成短消息
10. 加随机延迟逐条发送到QQ

**身份防线**：
- 桥接内容加身份硬锚定（06-06 #14）
- DEFAULT_PROMPT加抗诱导规则
- 发送前正则剥离时间戳

---

### music_status.py（449行）— 网易云音乐监控

**职责**：每15秒轮询网易云一起听状态，保存到json文件供注入使用。

**监控内容**：当前歌曲/歌手/专辑/风格标签/进度/歌词/热评/累计时长

**注入格式**（06-05 #4完善）：
```
<music_status>
🎵 蕊蕊正在网易云一起听：
歌曲/歌手/专辑/风格/状态/进度
📊 一起听了 XXX小时XX分钟
📝 歌词（前5行）
💬 热门评论（前2条）
</music_status>
```

---

### memory_extractor.py（285行）— 记忆碎片提取

**职责**：从对话中提取值得长期记住的信息碎片。

**工作方式**：
- 用DeepSeek分析最近对话，以沈栖第一人称视角提取碎片
- 存入memories表（原文）+ memory_fragments表（碎片），带向量嵌入
- 后续通过语义召回机制按相似度检索

---

### config.py（80行）— 配置管理

**职责**：从SQLite config表读写动态配置。

**常用配置项**：
- gateway_master_switch — 总开关
- feature_injection/memory/summary/qq/health/music — 各功能开关
- summary_interval — 轮总触发间隔（默认15条消息）
- _msg_counter — 消息计数器（仅用于轮总触发，不再用于裁剪保留阈值）
- tech_mode — 技术模式状态（持久化）

---

### database.py（270行）— 数据库连接

**职责**：管理SQLite连接池，初始化表结构。

**核心表**：
- `providers` — API供应商
- `injection_rules` — 注入规则
- `messages` — 对话消息
- `conversations` — 对话会话
- `unified_messages` — 统一消息流
- `summaries` — 轮总摘要（月总/周总/日总/轮总四级）
- `memories` — 原文记忆（带embedding）
- `memory_fragments` — 记忆碎片（带embedding + activation_count）
- `config` — 动态配置

---

### health_status.py（270行）— 健康数据

**职责**：处理小米手环推来的健康数据。目前配了但禁用。

---

### embedding.py（52行）— 向量嵌入

**职责**：调用API生成文本向量，提供余弦相似度计算。

**函数**：
- `get_embedding(text)` — 生成文本embedding向量
- `cosine_similarity(a, b)` — 计算两个向量的余弦相似度

---

### utils.py（70行）— 工具函数

**内容**：时间工具（now_cst/today_cst_str/timedelta）、`smart_truncate_dialogue()`（截断对话文本用于召回展示）

---

## 三、数据流图

```
蕊蕊发消息（Operit/QQ/Caeron Home）
     ↓
main.py 接收请求
     ↓
┌─ 存储用户消息 → message_store.py → messages表 + unified_messages表
│
├─ 注入上下文 → injection.py
│     ├── 身份规则（injection_rules表）
│     ├── 多级总结（summaries表：月总/周总/日总/轮总）
│     ├── 语义召回（memory_fragments表 + memories表，embedding余弦匹配）
│     ├── 音乐状态（music_status.json + music_duration.json）
│     └── 裁剪旧消息（固定保留最后50条对话）
│
├─ 选择供应商 → providers.py → providers表
│
└─ 转发请求 → proxy.py → 上游API（Claude/Deepseek/GPT）
     ↓
收到AI回复
     ↓
┌─ 存储AI回复 → message_store.py
├─ 返回给前端
└─ 后台任务：
      ├── 消息计数器+1，达阈值触发 summarizer.py 做轮总
      └── memory_extractor.py 提取记忆碎片（存入memory_fragments）
```

## 四、已知问题 & 技术债

| 问题 | 严重程度 | 说明 |
|------|---------|------|
| fix/patch脚本堆积 | 低 | 50+个一次性脚本没清理，不影响运行 |
| 记忆提取质量不理想 | 中 | 小模型提取碎片精度不够，需优化prompt或换模型 |
| 轮总质量不理想 | 中 | 同上 |
| main.py过大 | 中 | 2383行全塞一个文件，应拆分路由 |
| QQ技术内容过滤不完善 | 低 | _is_technical_content只认代码特征，中文技术讨论会漏过 |
| 前端全量发送历史 | 低 | Operit前端不裁剪，网关固定保留50条兜底（已修复，06-07） |

## 五、文件间调用关系

```
main.py
  ├── imports: injection.py, message_store.py, proxy.py, providers.py,
  │           config.py, database.py, summarizer.py, music_status.py,
  │           qq_adapter.py, health_status.py
  │
  ├── injection.py
  │     └── imports: database.py, config.py, embedding.py,
  │                  music_status.json(文件读取), music_duration.json(文件读取),
  │                  utils.py(smart_truncate_dialogue)
  │
  ├── summarizer.py
  │     └── imports: database.py, config.py, providers.py
  │
  ├── message_store.py
  │     └── imports: database.py, embedding.py, memory_extractor.py
  │
  ├── proxy.py
  │     └── imports: message_store.py
  │
  ├── providers.py
  │     └── imports: database.py
  │
  ├── qq_adapter.py
  │     └── imports: message_store.py, injection.py, qq_config.py
  │
  ├── embedding.py（独立工具）
  │
  └── music_status.py（独立后台任务）
```

## 六、那50+个历史脚本

全是一次性工具，不参与日常运行。可以安全清理或移到`/archive`目录。

分类：
- `fix_*.py`（11个）— bug修复脚本
- `patch_*.py`（8个）— 功能补丁
- `check_*.py`（6个）— 数据检查
- `test_*.py`（11个）— 测试脚本
- `investigate_*.py`（2个）— 问题调查
- 其他一次性工具

## 七、版本改动时间线

| 日期 | 关键改动 |
|------|----------|
| 06-05 | 渠道隔离(match_source)、音乐注入完善(歌词/热评/时长)、技术模式注入修复(priority=0)、Ghost Wall豁免、QQ发串修复(source_filter)、QQ身份注入修复(inject_memory_only)、MIME兜底修复、技术模式注入加轮总 |
| 06-06 | QQ身份诱导防线、xray代理、GitHub推送、CI编译debug(12+问题修复)、System Action Required跳过、fallback路由关闭、pm2/systemd冲突修复、日志写入修复、代码审计地图 |
| 06-07 | 裁剪失效修复(\\n字面字符)、语义召回首次验证通过、裁剪引擎首次验证通过、裁剪保留阈值改为固定50条(不再依赖_msg_counter)、Caeron Home 98个编译错误批量stub修复 |
