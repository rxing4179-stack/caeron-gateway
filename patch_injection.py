from utils import now_cst, today_cst_str
#!/usr/bin/env python3
"""Patch injection.py to add message trimming logic"""

import sys

with open('/home/ubuntu/caeron-gateway/injection.py', 'r') as f:
    lines = f.readlines()

# Replace lines 150-225 (0-indexed: 149-224)
new_function = '''    async def _inject_round_summaries(self, messages: list[dict], request_info: dict):
        """多级记忆注入：月总+周总+日总+轮总，按活跃状态自动切换，并裁剪已被总结覆盖的原始消息"""
        db = await get_db()
        try:
            parts = []
            has_round_summaries = False

            # 月总：所有活跃的（长期记忆）
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'monthly' AND is_active = 1 ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            if rows:
                for r in rows:
                    r = dict(r)
                    parts.append(f"- [月总] [{r['created_at']}] {r['content']}")

            # 周总
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'weekly' AND is_active = 1 ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            if rows:
                for r in rows:
                    r = dict(r)
                    parts.append(f"- [周总] [{r['created_at']}] {r['content']}")

            # 日总
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'daily' AND is_active = 1 ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            if rows:
                for r in rows:
                    r = dict(r)
                    parts.append(f"- [日总] [{r['created_at']}] {r['content']}")

            # 轮总：当天所有活跃的
            today = today_cst_str()
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at, '+8 hours') = ?
                   ORDER BY created_at ASC""",
                (today,)
            )
            rows = await cursor.fetchall()
            if rows:
                has_round_summaries = True
                total = len(rows)
                for idx, r in enumerate(rows, 1):
                    r = dict(r)
                    parts.append(f"- [轮总 #{idx}/{total}] [{r['created_at']}] {r['content']}")

            # 读取当前消息计数器（未被总结的新消息条数）
            unsummarized_count = 0
            if has_round_summaries:
                cursor = await db.execute("SELECT value FROM config WHERE key = '_msg_counter'")
                row = await cursor.fetchone()
                unsummarized_count = int(row['value']) if row else 0

        finally:
            await db.close()

        if not parts:
            logger.info(f"[记忆注入] 无任何活跃总结，跳过")
            return

        # === 裁剪已被总结覆盖的原始消息 ===
        if has_round_summaries:
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
            keep_count = unsummarized_count + buffer
            
            if len(dialog_indices) > keep_count and keep_count > 0:
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

        logger.info(f"[记忆注入] 注入 {len(parts)} 条多级总结")

'''

# lines 149 to 224 (0-indexed) = lines 150-225 (1-indexed)
before = lines[:149]
after = lines[225:]  # line 226 onwards (_replace_variables)

result = before + [new_function] + after

with open('/home/ubuntu/caeron-gateway/injection.py', 'w') as f:
    f.writelines(result)

print(f"Patched! Before: {len(lines)} lines, After: {len(result)} lines")
