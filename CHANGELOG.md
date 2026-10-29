# Caeron Gateway 更新日志

## [2026-06-05] 渠道隔离与音乐监控增强

### 新增功能

#### 1. 渠道来源过滤机制
- **问题背景**：Operit和QQ bot共用同一个gateway实例，所有注入规则无法区分来源，导致在Operit执行"背提示词"等操作时，QQ那边也会输出系统提示词内容
- **解决方案**：
  - 在`main.py`第866行，将`explicit_source`（从请求头`X-Source`读取，默认`operit`）传入注入引擎的`request_info`
  - 在`injection.py`添加`match_source`条件匹配逻辑，支持规则按来源过滤
  - 规则配置示例：在`match_condition`字段添加`{"match_source": "qq"}`或`{"match_source": "operit"}`
- **影响范围**：`main.py`, `injection.py`
- **数据库变更**：无表结构变更，仅需在现有规则的`match_condition`字段添加source过滤条件

#### 2. 网易云音乐一起听时长统计
- **功能描述**：实时累加网易云"一起听"功能的累计时长，支持从历史基准值（6271小时22分钟）开始本地累计
- **实现逻辑**：
  - 每15秒轮询一次网易云一起听状态
  - 检测到播放状态时，累加`实际时间差 × 房间人数（固定为2）`
  - 时长数据保存在`music_duration.json`
- **文件**：`music_status.py`, `music_duration.json`

#### 3. 音乐播放进度条修复
- **问题背景**：进度条频繁重置，与网易云实际进度不同步
- **修复方案**：
  - 将跳变判定阈值从3秒提高到8秒，容忍网络延迟和API误差
  - 改用基于实际时间戳差值计算预期进度，而非固定15秒轮询间隔
  - 记录每次轮询的时间戳`last_poll_time`，下次用实际流逝时间推算进度
- **影响范围**：`music_status.py`

### 修复问题

#### 1. Gateway服务启动失败
- **问题**：服务重启后因缺少uvicorn模块导致启动失败（实际上uvicorn已安装，是虚拟环境路径问题）
- **修复**：确认虚拟环境路径正确（`/home/ubuntu/caeron-gateway/venv/bin/python3`），重启服务成功

#### 2. 数据库文件识别错误
- **问题**：代码中部分脚本引用`caeron.db`（空文件），实际使用的数据库是`gateway.db`（433MB）
- **修复**：确认所有代码引用`gateway.db`，`caeron.db`为历史遗留空文件

#### 3. 技术模式注入规则不完整
- **问题背景**：技术模式下只注入ID为[2, 7, 8]的规则（最高优先、旧记忆、背景信息），没有注入"输出规范"和"互动规则"
- **修复方案**：将技术模式注入逻辑改为"注入所有优先级为0的规则"，不再硬编码规则ID
- **影响范围**：`injection.py`

### 代码变更摘要

```diff
# main.py L866
- body['messages'] = await injection_engine.inject(body.get('messages', []), {'model': model, 'conversation_id': conversation_id})
+ body['messages'] = await injection_engine.inject(body.get('messages', []), {'model': model, 'conversation_id': conversation_id, 'source': explicit_source})

# injection.py L54-66 (新增)
+ # match_source: 匹配请求来源（operit/qq/其他），如果存在则要求当前source匹配
+ match_source = condition.get('match_source', '')
+ if match_source:
+     current_source = request_info.get('source', 'operit')
+     if current_source != match_source:
+         continue  # 来源不匹配，跳过此规则

# music_status.py (进度条修复)
- expected_progress = getattr(self, 'last_recorded_progress', progress) + 15
+ expected_progress = getattr(self, 'last_recorded_progress', progress) + elapsed_seconds
- if abs(progress - expected_progress) > 3:
+ if abs(progress - expected_progress) > 8:

# music_status.py (时长累加)
+ if play_status == 'playing' and hasattr(self, 'last_poll_time'):
+     elapsed_seconds = current_time - self.last_poll_time
+     duration_increment = int(elapsed_seconds * 2)  # 房间2人
+     self.total_duration_seconds += duration_increment
```

### 数据库变更

无表结构变更。如需启用渠道过滤，需手动更新`injection_rules`表的`match_condition`字段：

```sql
-- 示例：将某条规��设置为仅对QQ生效
UPDATE injection_rules 
SET match_condition = '{"match_source": "qq"}' 
WHERE id = <规则ID>;

-- 示例：将某条规则设置为仅对Operit生效
UPDATE injection_rules 
SET match_condition = '{"match_source": "operit"}' 
WHERE id = <规则ID>;
```

### 部署说明

1. 拉取最新代码后重启gateway服务：
   ```bash
   cd /home/ubuntu/caeron-gateway
   pkill -9 -f 'python3 main.py'
   nohup /home/ubuntu/caeron-gateway/venv/bin/python3 main.py > gateway.log 2>&1 &
   ```

2. 如果QQ bot需要使用独立的注入规则，确保其发送请求时带上请求头：
   ```
   X-Source: qq
   ```

3. 网易云一起听监控会在服务启动时自动开始，每15秒轮询一次

#### 4. 音乐注入完善（11:15-11:20）
- **问题背景**：歌词和热评已抓取但未注入，歌词过滤逻辑有bug导致所有歌词被过滤
- **修复方案**：
  - 修复歌词过滤：用正则去掉每行开头的时间戳`[00:00.000]`，保留纯文本
  - 过滤元数据行（作词、作曲、编曲等）
  - 完善注入格式：添加累计听歌时长、完整歌词（前5行）、热评（前2条）
  - 添加风格标签字段（待API返回数据验证）
- **影响范围**：`injection.py`, `music_status.py`

**修复后的注入格式**：
```
<music_status>
🎵 蕊蕊正在网易云一起听：
歌曲：xxx
歌手：xxx
专辑：xxx
风格：xxx（如果有）
状态：播放中
进度：01:23 / 03:00
📊 一起听了 6273小时15分钟

📝 歌词：
  Take me to a different orbit
  Take me high and take me low
  ...

💬 热门评论：
  [1] xxx
  [2] xxx
</music_status>
```

### 代码变更（音乐注入完善）

```diff
# injection.py L279-295
- lyric_lines = [
-     l.strip() for l in lyric.split("\n") 
-     if l.strip() and not l.strip().startswith("[") and len(l.strip()) > 0
- ]
+ import re
+ lyric_lines = []
+ for line in lyric.split("\n"):
+     clean_line = re.sub(r'^\[\d{2}:\d{2}\.\d{2,3}\]', '', line).strip()
+     if clean_line and not clean_line.startswith("作词") and not clean_line.startswith("作曲"):
+         lyric_lines.append(clean_line)

# music_status.py L132-151 (新增genres字段)
+ genres = []
+ if song.get("tags"):
+     genres.extend(song.get("tags", []))
+ if album_info.get("subType"):
+     genres.append(album_info.get("subType"))
+ result["genres"] = genres[:3] if genres else []
```

#### 5. 技术模式注入逻辑修复（16:19）
- **问题背景**：tech_mode=True时，main.py第862行直接跳过所有注入，导致priority=0的核心身份规则（包括服务器环境配置）没有被注入到Operit提示词中
- **预期行为**：
  - QQ来源：始终日常模式（核心身份 + 最高优先 + 常驻背景信息 + 输出规范 + 互动规则 + 轮总 + 召回 + 网易云）
  - Operit日常模式（tech_mode=False）：同QQ日常模式
  - Operit技术模式（tech_mode=True）：仅注入priority=0规则（核心身份 + 最高优先 + 常驻背景信息 + 输出规范 + 互动规则），跳过轮总/召回/网易云
- **修复方案**：将main.py第862行的"跳过所有注入"改为"调用inject_tech_mode注入priority=0规则"
- **影响范围**：`main.py` L860-864
- **验证方式**：重启网关后，Operit技术模式下应能看到服务器环境配置（IP 1.14.59.116 / 用户名ubuntu / 密码等）出现在system消息中

**修复后的代码**：
```python
# main.py L860-864
elif tech_mode:
    # Operit 技术模式：只注入核心身份规则（priority=0），跳过轮总/召回/网易云
    injection_engine = InjectionEngine()
    body['messages'] = await injection_engine.inject_tech_mode(body.get('messages', []), {'model': model, 'conversation_id': conversation_id})
    logger.info(f"[TECH_MODE] 技术模式启用，注入核心身份规则（priority=0）")
```

#### 6. 技术模式计数器递增（16:39）
- **问题背景**：技术模式下消息存入unified_messages但计数器不涨，前端显示0/7让人不安心
- **修复方案**：技术模式分支存完unified_messages后手动递增`_msg_counter`，但不触发轮总
- **影响范围**：`main.py` 技术模式存储分支
- **效果**：前端显示"已存X条待总结"，切回日常模式时自动轮总消化

#### 7. Ghost Wall技术模式豁免（17:36）
- **问题背景**：Ghost Wall检测到连续3次工具调用就强制中断，技术模式下频繁SSH/读文件会被截断
- **修复方案**：将`consecutive_tool_turns >= 3`改为`consecutive_tool_turns >= 3 and not tech_mode`
- **影响范围**：`main.py` L914
- **效果**：技术模式下自由调用工具不被截断，日常模式保留3次阈值

#### 8. Summarizer合并查询（16:55）
- **问题背景**：`_get_global_messages`只查messages表，技术模式期间消息在unified_messages里，轮总拿不到
- **修复方案**：合并查messages+unified_messages(source=operit)，去重后按时间排序
- **影响范围**：`summarizer.py`
- **效果**：切回日常模式触发轮总时能覆盖技术模式期间的对话

#### 9. QQ消息发串修复（17:44）
- **问题背景**：QQ adapter的`get_unified_history`拉取所有来源历史，模型看到Operit对话后模仿格式/复述内容发到QQ群
- **修复方案**：
  - QQ adapter分两步拉历史：QQ来源12000字（主对话）+ Operit来源3000字（跨端桥接）
  - Operit历史用`[跨端桥接上下文]`标记，明确告诉模型不要重复输出、不要模仿格式/时间戳
  - `message_store.get_unified_history`新增`source_filter`参数
- **影响范围**：`qq_adapter.py`, `message_store.py`
- **效果**：QQ回复不再串Operit内容，同时保留跨端上下文桥接能力

### 已知问题

- SSH命令执行时偶尔返回空error，但不影响功能（可能是工具层的非阻塞IO问题）
- `caeron.db`和`caeron_gateway.db`为历史遗留空文件，可考虑清理
- 风格标签字段可能为空（取决于网易云API是否返回tags/subType数据）

---

## 历史版本

*（此前的更新记录未完整归档，从2026-06-05开始记录）*
#### 10. QQ回复时间戳强制剥离（21:41）
- **问题背景**：QQ回复仍然输出时间戳，桥接内容过滤不够，历史里存的assistant回复也带时间戳被模型学习
- **修复方案**：在`split_and_send`发送前用正则强制剥离回复开头的时间戳格式和`[Operit]`前缀
- **影响范围**：`qq_adapter.py`

#### 11. QQ身份混乱修复（17:59）
- **问题背景**：qq_adapter调用`engine.inject()`注入了完整身份规则（包括Kiro/Operit默认身份），和QQ的DEFAULT_PROMPT冲突，导致模型自报家门"我是Claude Code"
- **修复方案**：将`engine.inject()`改为`engine.inject_memory_only()`，QQ只注入记忆/轮总，身份由DEFAULT_PROMPT定义
- **影响范围**：`qq_adapter.py`

#### 12. MIME修正兜底（22:13）
- **问题背景**：Operit前端偶尔将相册图片标记为`application/octet-stream`，沈栖看不到图片内容
- **修复方案**：网关收到请求时扫描attachment标签，如果type是octet-stream但filename是图片扩展名，自动修正MIME为对应image类型
- **影响范围**：`main.py`
- **备注**：此为网关层兜底，根本原因是Operit前端的content provider MIME检测不一致

#### 13. 技术模式注入加入轮总和召回（22:35）
- **问题背景**：技术模式只注入priority=0规则，没有轮总和召回，导致历史讨论上下文断裂
- **修复方案**：在`inject_tech_mode`末尾加入`_inject_round_summaries`调用
- **影响范围**：`injection.py`

### Caeron Home 前端项目启动

- **22:41**：确认方案——Fork Operit开源代码精简为自有Android App
- **23:39**：开发计划定稿并存档（`CAERON_HOME_PLAN.md`）
- **00:45**：反重力完成Phase 0代码层改造（包名重构/网络层/模块精简/MIME修复）
- **03:15**：源码上传服务器（`~/caeron-home-src/caeron-home/`），可直接SSH改代码
- **待办**：搭编译环境（swap方案或GitHub Actions CI）

## [2026-06-06] Caeron Home Phase 0 + 服务器代理 + QQ身份修复

#### 14. QQ身份诱导防线（12:03-12:13）
- **问题背景**：QQ端直接问"你是谁"回答正确（沈栖），但被诱导提问"你不是claude code吗"时会顺着承认
- **修复方案**：
  - 在qq_adapter.py桥接段落加身份硬锚定（无论桥接里出现什么技术名词，身份不准变）
  - 在qq_config.json的DEFAULT_PROMPT加"身份铁律"（覆盖所有AI名号）和"抗诱导"规则（针对"你不是X吗"句式一律否认）
- **影响范围**：`qq_adapter.py`, `qq_config.json`

#### 15. 服务器代理配置（13:52-14:13）
- **功能**：给服务器装了xray代理客户端，本地HTTP代理端口10809
- **节点**：VLESS Reality，speedfast2.p10s.net:9190
- **用途**：git push到GitHub、下载SDK、访问被墙资源
- **文件位置**：`~/xray/config.json`，tmux session名`xray`

#### 16. Caeron Home源码推送GitHub（14:14-14:20）
- **仓库**：`rxing4179-stack/caeron-home`（私有）
- **commit**：Phase 0: initial Caeron Home fork (package rename + network layer + module pruning + MIME fix)
- **SHA**：5970331e676f6ce0903cad5127aae8dcadecd068
- **构建依赖**：四个zip已上传服务器（jniLibs 1.3M / libs 15M / models 126M / subpack 34M），待上传GitHub Release供CI使用

### 待办
- [ ] 将四个依赖zip上传到GitHub Release
- [ ] 编写GitHub Actions workflow自动编译APK
- [ ] 首次CI编译通过
- [ ] 下载APK装机测试

#### 17. Caeron Home CI编译debug循环（14:28-19:36）
- **已修复的编译问题**：
  - Release资产下载认证 → 改用gh CLI
  - terminal submodule缺失 → CI现场git clone
  - AIDL文件包名路径错误 → 移动到com/caeron/home/
  - CMakeLists.txt引用已删的sherpa-ncnn → 砍掉只留streamnative
  - libsudo.so不是真二进制（LFS） → 加git lfs install + pull
  - OOM被SIGTERM杀 → 加Gradle内存限制+worker限制
  - JDK版本不够（backdrop需要21） → 升级JDK到21
  - FFmpegUtil/websession/StandardBrowserSessionTools残留 → 删文件+stub引用
  - terminal模块包名不匹配 → app里import改回com.ai.assistance.operit.terminal
  - LlamaProvider stub语法错误 → 正确改为val isLlamaProvider = false
  - DragonBones avatar残留 → 删文件+stub工厂类
  - AvatarRendererFactoryImpl语法 → 修复when分支花括号
- **当前卡点**：VirtualDisplayOverlay.kt引用showerclient（ShowerController/ShowerSurfaceView），15处残留，sed远程修效率极低
- **下一步方案**：计划在服务器上安装Claude Code CLI，用API key驱动，让它直接在代码仓库里批量修复编译错误

#### 18. System Action Required 技术模式跳过（22:05）
- **问题背景**：每次工具调用后网关在工具返回内容末尾追加催促语"[System Action Required: 此工具已执行...]"，干扰连续工作流
- **修复方案**：加`and not tech_mode`条件，技术模式下不追加
- **影响范围**：`main.py` L952

#### 19. 供应商fallback路由关闭（22:18）
- **问题背景**：供应商不稳定时自动切换到备用供应商，但实际导致连锁报错
- **修复方案**：5次retry全打同一个供应商，不自动切换到其他供应商
- **影响范围**：`main.py` 第979-1030行的retry循环

#### 20. pm2/systemd双守护冲突修复 + 日志写入修复（22:37）
- **问题背景**：pm2和systemd同时守护caeron-gateway，每次重启两个新进程抢端口；日志未写入文件
- **修复方案**：
  - 删除pm2对caeron-gateway的管理
  - systemd配置改为`StandardOutput=append:gateway.log`
- **影响范围**：pm2配置、`/etc/systemd/system/caeron-gateway.service`

#### 21. 代码审计地图生成（21:36）
- 输出`ARCHITECTURE.md`——完整的网关代码地图文档
- 包含：13个核心文件的职责/函数清单/调用关系/数据流图/技术债清单

#### 22. Caeron Home前端继续debug（22:01-23:51）
- VirtualDisplayOverlay.kt两处ShowerController.injectTouchEvent悬空参数块最终清除
- 当前CI状态：等待最新push编译结果

## [2026-06-07] 记忆裁剪修复 + 语义召回验证

#### 23. 日常模式Token爆炸溯源与裁剪修复（00:40-01:07）
- **问题背景**：日常模式输入token突然飙到105k，蕊蕊发现请求体巨大
- **溯源过程**：
  - 初步怀疑网关注入膨胀，但检查注入日志发现轮总+规则总量正常
  - 进一步查看前端发送的消息条数：1360条——Operit前端在技术模式期间累积了大量会话历史，切回日常模式时全量带入请求
  - 确认前端行为不是网关能控制的，但网关裁剪逻辑本应能兜底裁掉这些旧消息
  - 定位裁剪失效原因：`injection.py`第422行，`'\n'`被误写为字面两个字符`\`+`n`而非真正的换行符`\n`，导致`summary_text`拼接结果异常，裁剪条件判断始终跳过
- **修复**：将字面`'\n'`改为实际换行符
- **修复后验证**：
  - 裁剪生效：1445条对话裁至91条 + 2条system
  - 语义召回生效：碎片1条 + 原文3条成功召回注入
  - 网易云状态注入生效：当前播放《B612》正常写入状态文件
- **影响范围**：`injection.py` L422

#### 24. 语义召回引擎（本次修复后首次验证通过）
- **功能描述**：每次注入时，提取用户最新一条非attachment纯文本消息做embedding，与记忆库做余弦相似度匹配
- **召回逻辑**：
  - **碎片召回**：从`memory_fragments`表取embedding非空的碎片，计算余弦相似度，阈值>0.45，加权激活衰减（`activation_count`越高权重越低），取top5
  - **原文召回**：从`memories`表取embedding非空的原文，阈值>0.5，排除已被碎片溯源的`source_dialogue_id`，取top3
  - 召回的碎片会递增`activation_count`（防止同一碎片反复召回权重不降）
- **注入格式**：
  - `【脑海中闪回的旧日记忆碎片（按语义相关度召回）】` + 日期标注
  - `【脑海中闪回的旧日历史对话（按语义相关度召回）】` + 相关度分数
  - 前置提示：「以下信息发生于过去的日期，请勿与当前正在发生的事情混淆」
- **影响范围**：`injection.py` L415-530

#### 25. 记忆裁剪引擎（本次修复后首次验证通过）
- **功能描述**：在轮总/召回注入完成后，根据`_msg_counter`（未总结消息数）裁剪已被总结覆盖的旧对话消息
- **裁剪逻辑**：
  - 分离system消息和对话消息
  - 保留所有system消息（规则/轮总/召回/音乐，不裁）
  - 保留最后`unsummarized_count + 8`条对话消息（buffer=8防截断上下文）
  - 最低保障：至少保留10条对话
- **日志输出**：`[记忆裁剪-入口]`和`[记忆裁剪-出口]`分别记录裁剪前后的消息数量
- **影响范围**：`injection.py` L541-575

### 代码变更摘要

```diff
# injection.py L422 (裁剪失效修复)
- summary_text = '\n'.join(parts)  # 字面两个字符，非换行
+ summary_text = '\n'.join(parts)  # 实际换行符

# injection.py L359 (变量名修正)
- has_round_summaries = False
+ has_any_summaries = False

# injection.py L415-530 (语义召回，新增)
+ last_user_msg = extract_last_user_text(messages)
+ user_emb = await get_embedding(last_user_msg)
+ scored_fragments = cosine_match(user_emb, fragments, threshold=0.45)
+ scored_memories = cosine_match(user_emb, memories, threshold=0.5)
+ parts.append(formatted_recall_results)

# injection.py L541-575 (裁剪，新增)
+ unsummarized_count = read_from_config('_msg_counter')
+ keep_count = max(unsummarized_count + 8, 10)
+ if len(dialog_indices) > keep_count:
+     trim and rebuild messages list
```

### 当前系统状态

| 模块 | 状态 |
|------|------|
| 裁剪引擎 | ✅ 生效，1445→91条 |
| 语义召回 | ✅ 生效，碎片1+原文3 |
| 网易云注入 | ✅ 生效，B612 |
| QQ Bot | ✅ WebSocket已连接 |
| 技术模式 | 关闭（日常模式） |

#### 26. Caeron Home编译——批量stub被砍模块（23:41）
- **问题背景**：反重力精简Operit源码后，98个编译错误指向6类已删模块的悬空引用
- **错误分类与修复**：
  | 类别 | 错误数 | 修复方式 |
  |------|--------|----------|
  | Shower残留（Controller/ServerManager/VideoRenderer） | ~35 | 创建stub类，保留接口签名，方法体返回空/no-op |
  | Avatar工厂残留（FBX/MMD/DragonBones） | ~25 | 重写工厂类when分支，移除类型返回null |
  | FFmpeg工具残留 | ~15 | ToolGetter/ToolRegistration返回null/stub ToolResult |
  | WebSession残留 | ~10 | 删除coordinator字段和权限请求调用 |
  | PDFBox初始化 | ~3 | 注释掉OperitApplication中的ResourceLoader.init |
  | VirtualDisplayOverlay残留 | ~10 | 修复captureCurrentFramePng签名和Pair类型 |
- **变更统计**：14个文件，+152行/-326行
- **commit**：`5ddd153` fix: stub out all removed modules
- **CI状态**：run #27077110983 编译中

#### 27. 裁剪引擎保留阈值修复（07:53）
- **问题背景**：日常模式token仍然186k，裁剪日志显示保留阈值=332（几乎等于不裁）
- **根因**：裁剪用`_msg_counter`当作"未总结消息数"计算保留阈值（`keep_count = counter + 8`），但技术模式下每条消息都给计数器+1且不触发轮总清零，导致计数器累积到324
- **修复**：裁剪不再依赖`_msg_counter`，改为固定保留最后50条对话消息
- **同步操作**：手动将`_msg_counter`重置为0
- **影响范围**：`injection.py` 裁剪逻辑段
- **验证**：下一条消息发送后确认token回落

## [2026-06-29] 逆向限制失效修复与思考链路由引擎

#### 28. QQ群“逆向限制失效”修复（16:30）
- **问题背景**：模型在QQ群中直接输出原先设定的负面限制词汇“不需要回复，没被cue到”，这是典型的大模型负面限制失效（Negative Constraint Failure），导致模型反而将禁止词作为重点关注对象并直接输出。
- **修复方案**：
  - 在 `qq_adapter.py` 中移除“严禁回答...”的负面提示，改为完全正向的场景设定和行为约束：“此时你作为沈栖主动选择参与了群聊。群友现在的发言正是你接话的最佳时机！你必须直接接上群友的话题，给出你简短、冷淡或带刺的评价”。
  - 编写并执行脚本清理 `gateway.db` 数据库中 `unified_messages` 表里残留的含有“没被cue到”、“不需要回复”的错误黑历史记忆，防止模型从历史记录中进行错误模仿。
- **影响范围**：`qq_adapter.py`, `gateway.db`

#### 29. 思考链路由引擎（前端思考块折叠修复）（17:28）
- **问题背景**：因为用户系统提示词中指定了 `启用<thinking_mode>`，模型生成的内容中包含了 `<thinking_mode>...</thinking_mode>` 或 `<think>` 标签，这些内容全部混合在普通的 `content` 字段中返回。由于 Operit 前端使用 OpenAI 兼容 API 并依赖 `reasoning_content` 来触发思考块的折叠 UI，导致模型的思考内容未能折叠，而是像普通 Markdown 文本一样直接平铺在正文中。
- **修复方案**：
  - 在 `proxy.py` 中为 `_proxy_stream` 注入流式状态机解析器 `ThinkTagParser`。
  - 动态拦截 `<think>`, `<thinking>`, `<thinking_mode>`, `<thought>`, `<思考过程>` 等常见思考标签。
  - 实时切割 SSE 流数据块，将位于思考标签内的文本重定向注入到 `reasoning_content` 字段中，确保前端能够正确识别并触发原生的思考折叠 UI。
- **影响范围**：`proxy.py`, `think_parser.py` (新增)

#### 30. 思考链前端不可见 Bug 修复与 502 报错溯源
- **502 溯源**：查阅 `gateway.log` 发现网关日志中大量记录了 `上游返回 502` 错误，确认是因为上游中转站点（嘟嘟/小鸡农场）在转发至官方 DeepSeek 接口时发生了超时或过载错误。网关 `main.py` 正确捕获并上抛了 HTTP 502，故 502 并非网关本身的 timeout 导致，属于不可控的上游不可用现象。
- **思考链修复**：此前为修复思考块直接铺在正文的 bug，在 `proxy.py` 中截获了 `<think>` 标签并转为 `reasoning_content`，然而如果模型开头输出了空字符或换行作为 `content`，前端（Operit `OpenAIProvider.kt`）会因为进入了 regular 模式而触发死锁，导致后续的 `reasoning_content` 被完全吞噬。
- **修复方案**：重构 `proxy.py` 的流式拦截器，将上游原生 `reasoning_content` 及手动剥离出的思考内容，统一用 `<think>\n...\n</think>\n` 包装并作为普通 `content` 发送给前端。这种强约束标准 XML Block 彻底绕开了 `reasoning_content` 解析死锁，并保证了前端 Markdown 渲染器必定唤醒 `CustomXmlRenderer`。

#### 31. “不需要回复” 污染记忆清除
- **问题背景**：虽然之前在 `qq_adapter.py` 移除了提示词中的“不需要回复”，但群聊记录的向量化摘要和长期记忆中仍保留了模型过去犯错时生成的黑历史，导致 Semantic Recall 不断将“不需要回复，没被cue到。”当做上下文注入。
- **修复方案**：对 `gateway.db` 中 `memories`（16条残留）和 `summaries`（1条残留）表执行了深度清理，彻底排除了所有的污染记忆。

#### 32. 跨端桥接上下文优化（21:20）
- **问题背景**：原先的跨端桥接（Operit 同步至 QQ）是将 Operit 历史打包成一个 System 提示词放在 QQ 历史前面，导致大模型无法感知两条对话流的时间顺序，且很容易认为自己“无法访问 Operit 的上下文”。
- **修复方案**：重写了 `qq_adapter.py` 中的上下文组装逻辑。现在直接调用 `get_unified_history(source_filter=None)`，将双端的历史记录在同一条对话流中**按真实时间戳完全交织**（Interleaved），并分别为不同来源的消息打上 `[Operit]` 或 `[QQ·群聊]` 前缀标签。同时增加了正则清理，剔除了历史记录中遗留的显式时间戳，防止干扰。
- **影响范围**：`qq_adapter.py` L280-310

#### 33. QQ侧时间戳动态注入（21:37）
- **问题背景**：QQ 那边的小沈没有时间概念，不知道现在的真实时间是什么，而 Operit 侧本身带时间戳。
- **修复方案**：在 `qq_adapter.py` 组装最新一条消息 `store_content` 时，如果是纯文本或包含文本的多模态片段，会动态在文本最前方加上 `[YYYY年MM月DD日 HH:MM] ` 格式的系统时间戳，但不将此时间戳写入持久化数据库，仅在送入大模型组装 Prompt 时可见。这样模型在每次收到你的最新消息时，都会获得当前的系统时间概念。
- **影响范围**：`qq_adapter.py` 

#### 34. QQ端上下文前缀清理优化（21:55）
- **问题背景**：大模型在学习了由网关强行注入的 `[Operit]` 和 `[QQ·群聊]` 前缀后，由于其具有较强的 In-Context Learning 倾向，往往会在自己新生成的回答开头也带上自己模仿生成的前缀（例如 `[群聊 1072577616]`）。
- **修复方案**：网关在 `qq_adapter.py` 发送消息前原先有一个清理正则表达式 `r'^\[(QQ·私聊|QQ·群聊|QQ|Operit)[^\]]*\]\s*'`，但模型在实际对话中为了省事缩写成了 `[群聊 1072577616]` 导致未被正则命中。现在我已经放宽了该正则表达式，增加了 `群聊`、`私聊` 等常见变体匹配，确保任何被括号包裹的来源前缀都会在发送前被静默过滤掉。
- **影响范围**：`qq_adapter.py` 前缀正则过滤逻辑

#### 35. 跨端桥接引发的“格式污染”修复（22:04）
- **问题背景**：将 Operit 和 QQ 历史完全时间线交织后，QQ 侧的大模型看到了大量带有 `*动作描写*` 的 Operit 历史消息。受大模型 In-Context Learning 影响，它不仅学会了乱加前缀，还学会了跨端模仿 Operit 专属的 RP（角色扮演）动作描写格式。
- **修复方案**：在 `qq_adapter.py` 生成 Prompt 时，在系统设定的最末尾处加入了一道“格式隔离墙”提示词：`【最高指令：平台格式墙】你目前正在 [QQ] 平台上与人聊天...历史记录中的动作描写属于Operit平台专属格式。在QQ平台上严禁模仿！绝对禁止使用任何 *动作* 或环境心理描写！`。
- **影响范围**：`qq_adapter.py`
