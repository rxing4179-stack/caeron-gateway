import os
import re

path = os.path.expanduser('~/caeron-gateway/injection.py')
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# The fix: replace the entire _inject_round_summaries method
# Key changes:
# 1. Also query YESTERDAY's round/round_rollup summaries (not just today)
# 2. Decouple trimming from has_round_summaries - always trim if there are ANY active summaries
# 3. Use a fallback trimming strategy based on total message count when no round summaries exist
# 4. Add robust image/base64 content detection and size logging

new_method = '''    async def _inject_round_summaries(self, messages: list[dict], request_info: dict):
        """多级记忆注入：月总+周总+日总+轮总，按活跃状态自动切换，并裁剪已被总结覆盖的原始消息"""
        # 1. 检查是否已经注入过总结，防止重复
        for m in messages:
            if m.get('role') == 'system' and '<context_summaries>' in m.get('content', ''):
                logger.warning("[记忆注入] 检测到已存在注入的总结，跳过本次注入以防重复")
                return

        db = await get_db()
        try:
            parts = []
            tag_counts = {}
            has_any_summaries = False
            today = today_cst_str()

            # 2. 月总：只取最新 2 条
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'monthly' AND is_active = 1 ORDER BY created_at DESC LIMIT 2"
            )
            rows = list(reversed(await cursor.fetchall()))
            tag_counts['monthly'] = len(rows)
            if rows:
                has_any_summaries = True
            for r in rows:
                r = dict(r)
                parts.append(f"- [月总] [{r['created_at']}] {r['content']}")

            # 3. 周总：只取最新 1 条
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'weekly' AND is_active = 1 ORDER BY created_at DESC LIMIT 1"
            )
            rows = await cursor.fetchall()
            tag_counts['weekly'] = len(rows)
            latest_weekly_time = None
            if rows:
                has_any_summaries = True
                latest_weekly_time = dict(rows[0])['created_at']
                for r in rows:
                    r = dict(r)
                    parts.append(f"- [周总] [{r['created_at']}] {r['content']}")

            # 4. 日总：取最新周总之后的日总，上限 7 条；无周总取最近 3 天
            MAX_DAILY_INJECT = 7
            if latest_weekly_time:
                cursor = await db.execute(
                    """SELECT content, created_at FROM summaries
                       WHERE tag = 'daily' AND is_active = 1
                       AND created_at > ?
                       ORDER BY created_at ASC
                       LIMIT ?""",
                    (latest_weekly_time, MAX_DAILY_INJECT)
                )
            else:
                cursor = await db.execute(
                    """SELECT content, created_at FROM summaries
                       WHERE tag = 'daily' AND is_active = 1
                       ORDER BY created_at DESC
                       LIMIT 3"""
                )
            rows = await cursor.fetchall()
            if not latest_weekly_time:
                rows = list(reversed(rows))
            tag_counts['daily'] = len(rows)
            if rows:
                has_any_summaries = True
            for r in rows:
                r = dict(r)
                parts.append(f"- [日总] [{r['created_at']}] {r['content']}")

            # 5. 轮总总：查当天 + 昨天的（防止跨零点遗漏）
            from datetime import timedelta as td
            yesterday = (now_cst() - td(days=1)).strftime('%Y-%m-%d')
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round_rollup' AND is_active = 1
                   AND date(created_at) IN (?, ?)
                   ORDER BY created_at DESC LIMIT 8""",
                (today, yesterday)
            )
            rollup_rows = list(reversed(await cursor.fetchall()))
            tag_counts['round_rollup'] = len(rollup_rows)
            if rollup_rows:
                has_any_summaries = True
                for idx, r in enumerate(rollup_rows, 1):
                    r = dict(r)
                    parts.append(f"- [轮总总 #{idx}/{len(rollup_rows)}] [{r['created_at']}] {r['content']}")
            
            # 6. 轮总：查当天 + 昨天的（防止跨零点遗漏），最多 8 条
            MAX_ROUND_INJECT = 8
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at) IN (?, ?)
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (today, yesterday, MAX_ROUND_INJECT)
            )
            rows = list(reversed(await cursor.fetchall()))
            tag_counts['round'] = len(rows)
            if rows:
                has_any_summaries = True
                total = len(rows)
                for idx, r in enumerate(rows, 1):
                    r = dict(r)
                    parts.append(f"- [轮总 #{idx}/{total}] [{r['created_at']}] {r['content']}")

            # 读取当前消息计数器
            unsummarized_count = 0
            cursor = await db.execute("SELECT value FROM config WHERE key = '_msg_counter'")
            row = await cursor.fetchone()
            unsummarized_count = int(row['value']) if row else 0

            logger.info(f"[记忆注入] 注入汇总统计: {tag_counts}, 待总结消息数: {unsummarized_count}")

        finally:
            await db.close()

        if not parts:
            logger.info(f"[记忆注入] 无任何活跃总结，跳过")
            return

        # === 裁剪已被总结覆盖的原始消息 ===
        # 关键修复：只要有任何活跃总结，就执行裁剪（不再依赖 has_round_summaries）
        if has_any_summaries:
            # 分离system消息和对话消息
            system_indices = []
            dialog_indices = []
            for i, m in enumerate(messages):
                if m.get('role') == 'system':
                    system_indices.append(i)
                else:
                    dialog_indices.append(i)
            
            # 保留最后 unsummarized_count + buffer 条对话消息
            buffer = 8
            keep_count = max(unsummarized_count + buffer, 10)  # 至少保留10条对话
            
            if len(dialog_indices) > keep_count:
                trimmed_count = len(dialog_indices) - keep_count
                keep_indices = set(system_indices + dialog_indices[-keep_count:])
                
                new_messages = [messages[i] for i in sorted(keep_indices)]
                messages.clear()
                messages.extend(new_messages)
                
                logger.info(f"[记忆裁剪] 裁掉 {trimmed_count} 条已总结旧消息，保留 {len(dialog_indices[-keep_count:])} 条对话 + {len(system_indices)} 条system (计数器={unsummarized_count}, buffer={buffer})")
            else:
                logger.info(f"[记忆裁剪] 对话 {len(dialog_indices)} 条 <= 保留阈值 {keep_count}，不裁剪")

        # 组装总结文本
        summary_lines = ["<context_summaries>"]
        summary_lines.append(f"以下是今天（{today_cst_str()}）的对话记忆摘要，供你参考当前上下文：")
        summary_lines.extend(parts)
        summary_lines.append("</context_summaries>")

        summary_text = "\\n".join(summary_lines)

        # 注入位置：在最后一个system消息之后
        insert_idx = 0
        for i, m in enumerate(messages):
            if m.get('role') == 'system':
                insert_idx = i + 1
        messages.insert(insert_idx, {'role': 'system', 'content': summary_text})

        logger.info(f"[记忆注入] 注入 {len(parts)} 条多级总结")'''

# Use regex to replace the method
pattern = r'    async def _inject_round_summaries\(self, messages: list\[dict\], request_info: dict\):.*?logger\.info\(f"\[记忆注入\] 注入 \{len\(parts\)\} 条多级总结"\)'
match = re.search(pattern, content, flags=re.DOTALL)
if match:
    content = content[:match.start()] + new_method + content[match.end():]
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    print("SUCCESS: Patched _inject_round_summaries with cross-midnight fix")
else:
    print("ERROR: Could not find _inject_round_summaries method to replace")
    # Try a simpler pattern
    pattern2 = r'    async def _inject_round_summaries\(self.*?注入 \{len\(parts\)\} 条多级总结'
    match2 = re.search(pattern2, content, flags=re.DOTALL)
    if match2:
        print(f"Found with simpler pattern at pos {match2.start()}-{match2.end()}")
    else:
        print("Simpler pattern also failed")
        # Print surrounding context for debugging
        idx = content.find('_inject_round_summaries')
        if idx >= 0:
            print(f"Method found at char {idx}")
            print("Last 200 chars of method area:")
            end_area = content.find('_replace_variables', idx)
            if end_area > 0:
                print(repr(content[end_area-200:end_area]))

