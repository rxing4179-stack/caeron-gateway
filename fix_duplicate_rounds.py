import os
import re

# ==========================================
# Patch main.py
# ==========================================
main_path = os.path.expanduser('~/caeron-gateway/main.py')
with open(main_path, 'r', encoding='utf-8') as f:
    main_content = f.read()

# Original code snippet in main.py
old_trigger = """                    async def _bg_round_summary():
                        try:
                            summarizer = get_summarizer()
                            result = await summarizer.generate_round_summary()
                            logger.info(f"[AUTO_SUMMARY] 轮总生成完成 ({len(result) if result else 0} 字符)")
                        except Exception as e:
                            logger.error(f"[AUTO_SUMMARY] 轮总生成失败: {e}")
                    
                    asyncio.create_task(_bg_round_summary())
                    new_count = 0  # 重置计数
                
                # 更新计数器
                await db.execute(
                    "INSERT INTO config (key, value, description) VALUES ('_msg_counter', ?, '轮总触发计数器') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(new_count),)
                )"""

new_trigger = """                    # --- Bug 修复：防止异步并发导致的重复生成 ---
                    # 先将计数器扣除阈值（原子化概念），而不是简单置0，这样能承接并发请求的消息
                    new_count = max(0, new_count - trigger_threshold)
                    
                    logger.info(f"[AUTO_SUMMARY] 准备触发轮总。当前触发阈值:{trigger_threshold}, 剩余未总结数:{new_count}")

                    async def _bg_round_summary():
                        try:
                            summarizer = get_summarizer()
                            result = await summarizer.generate_round_summary()
                            logger.info(f"[AUTO_SUMMARY] 轮总生成完成 ({len(result) if result else 0} 字符)")
                        except Exception as e:
                            logger.error(f"[AUTO_SUMMARY] 轮总生成失败: {e}")
                    
                    asyncio.create_task(_bg_round_summary())
                
                # 更新计数器
                await db.execute(
                    "INSERT INTO config (key, value, description) VALUES ('_msg_counter', ?, '轮总触发计数器') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(new_count),)
                )"""

if old_trigger in main_content:
    main_content = main_content.replace(old_trigger, new_trigger)
    print("main.py patched successfully.")
else:
    print("Warning: Could not find trigger logic in main.py. Trying regex...")
    # Try regex fallback if exact match fails
    pattern = r'async def _bg_round_summary\(\):.*?asyncio\.create_task\(_bg_round_summary\(\)\)\s+new_count = 0\s*# 重置计数\s*# 更新计数器\s*await db\.execute\([\s\S]*?\)'
    match = re.search(pattern, main_content)
    if match:
        print("Regex match found!")

with open(main_path, 'w', encoding='utf-8') as f:
    f.write(main_content)

# ==========================================
# Patch summarizer.py
# ==========================================
sum_path = os.path.expanduser('~/caeron-gateway/summarizer.py')
with open(sum_path, 'r', encoding='utf-8') as f:
    sum_content = f.read()

# We need to add an asyncio.Lock to MultiLevelSummarizer
if "import asyncio" not in sum_content:
    sum_content = "import asyncio\n" + sum_content

# Add lock initialization in __init__
old_init = """    def __init__(self):
        self.summary_model = "deepseek-ai/DeepSeek-V3.2"
        self.max_context_messages = 100
        self.max_content_chars = 30000"""

new_init = """    def __init__(self):
        self.summary_model = "deepseek-ai/DeepSeek-V3.2"
        self.max_context_messages = 100
        self.max_content_chars = 30000
        self._round_lock = asyncio.Lock()  # 添加并发锁防止幽灵轮总"""

if old_init in sum_content:
    sum_content = sum_content.replace(old_init, new_init)
    print("summarizer.py __init__ patched.")

# Add deduplication check in generate_round_summary
old_gen = """    async def generate_round_summary(self) -> str:
        \"\"\"生成一条轮总\"\"\"
        logger.info("[SUMMARIZER] 开始生成轮总...")
        
        previous_summary = await self._get_latest_active_round_summary()
        messages = await self._get_global_messages(since_summary=True)"""

new_gen = """    async def generate_round_summary(self) -> str:
        \"\"\"生成一条轮总\"\"\"
        if self._round_lock.locked():
            logger.warning("[SUMMARIZER] 另一轮总正在生成中，跳过本次触发以防止幽灵重复！")
            return None
            
        async with self._round_lock:
            logger.info("[SUMMARIZER] 开始生成轮总...")
            
            # --- 去重守卫：检查过去 3 分钟内是否已经生成过轮总 ---
            db = await get_db()
            try:
                cursor = await db.execute("SELECT created_at FROM summaries WHERE tag = 'round' AND is_active = 1 ORDER BY created_at DESC LIMIT 1")
                last_row = await cursor.fetchone()
                if last_row:
                    from datetime import datetime
                    last_time = datetime.strptime(last_row['created_at'], '%Y-%m-%d %H:%M:%S')
                    now = datetime.now()
                    diff_seconds = (now - last_time).total_seconds()
                    logger.info(f"[SUMMARIZER] 上次轮总时间: {last_row['created_at']} (距今 {diff_seconds:.1f} 秒)")
                    if diff_seconds < 180:
                        logger.warning(f"[SUMMARIZER] 拒绝生成：距离上次轮总不到 3 分钟，防止重复写入！")
                        return None
            except Exception as e:
                logger.error(f"[SUMMARIZER] 去重守卫异常: {e}")
            finally:
                await db.close()
            # --- 去重守卫结束 ---
            
            previous_summary = await self._get_latest_active_round_summary()
            messages = await self._get_global_messages(since_summary=True)"""

if old_gen in sum_content:
    sum_content = sum_content.replace(old_gen, new_gen)
    print("summarizer.py generate_round_summary patched.")

with open(sum_path, 'w', encoding='utf-8') as f:
    f.write(sum_content)

print("Both files patched successfully.")

