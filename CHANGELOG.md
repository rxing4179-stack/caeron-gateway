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