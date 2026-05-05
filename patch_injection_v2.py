import os
import re

path = os.path.expanduser('~/caeron-gateway/injection.py')
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Replace _inject_round_summaries method
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
            has_round_summaries = False
            today = today_cst_str()

            # 2. 月总：只取最新 2 条
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'monthly' AND is_active = 1 ORDER BY created_at DESC LIMIT 2"
            )
            rows = list(reversed(await cursor.fetchall()))
            tag_counts['monthly'] = len(rows)
            for r in rows:
                r = dict(r)
                parts.append(f"- [月总] [{r['created_at']}] {r['content']}")
                logger.info(f"[记忆注入] 加载月总: {r['created_at']} ({r['content'][:50]}...)")

            # 3. 周总：只取最新 1 条
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'weekly' AND is_active = 1 ORDER BY created_at DESC LIMIT 1"
            )
            rows = await cursor.fetchall()
            tag_counts['weekly'] = len(rows)
            latest_weekly_time = None
            if rows:
                latest_weekly_time = dict(rows[0])['created_at']
                for r in rows:
                    r = dict(r)
                    parts.append(f"- [周总] [{r['created_at']}] {r['content']}")
                    logger.info(f"[记忆注入] 加载周总: {r['created_at']} ({r['content'][:50]}...)")

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
            for r in rows:
                r = dict(r)
                parts.append(f"- [日总] [{r['created_at']}] {r['content']}")
                logger.info(f"[记忆注入] 加载日总: {r['created_at']} ({r['content'][:50]}...)")

            # 5. 轮总总：当天最新的 8 条
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round_rollup' AND is_active = 1
                   AND date(created_at) = ?
                   ORDER BY created_at DESC LIMIT 8""",
                (today,)
            )
            rollup_rows = list(reversed(await cursor.fetchall()))
            tag_counts['round_rollup'] = len(rollup_rows)
            if rollup_rows:
                has_round_summaries = True
                for idx, r in enumerate(rollup_rows, 1):
                    r = dict(r)
                    parts.append(f"- [轮总总 #{idx}/{len(rollup_rows)}] [{r['created_at']}] {r['content']}")
                    logger.info(f"[记忆注入] 加载轮总总 #{idx}: {r['created_at']} ({r['content'][:50]}...)")
            
            # 6. 轮总：当天最新的 8 条
            MAX_ROUND_INJECT = 8
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at) = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (today, MAX_ROUND_INJECT)
            )
            rows = list(reversed(await cursor.fetchall()))
            tag_counts['round'] = len(rows)
            if rows:
                has_round_summaries = True
                total = len(rows)
                for idx, r in enumerate(rows, 1):
                    r = dict(r)
                    parts.append(f"- [轮总 #{idx}/{total}] [{r['created_at']}] {r['content']}")
                    logger.info(f"[记忆注入] 加载轮总 #{idx}: {r['created_at']} ({r['content'][:50]}...)")

            # 读取当前消息计数器
            unsummarized_count = 0
            if has_round_summaries:
                cursor = await db.execute("SELECT value FROM config WHERE key = '_msg_counter'")
                row = await cursor.fetchone()
                unsummarized_count = int(row['value']) if row else 0

            logger.info(f"[记忆注入] 注入汇总统计: {tag_counts}, 待总结消息数: {unsummarized_count}")

        finally:
            await db.close()'''

pattern = r'    async def _inject_round_summaries\(self, messages: list\[dict\], request_info: dict\):.*?finally:\s+await db\.close\(\)'
content = re.sub(pattern, new_method, content, flags=re.DOTALL)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Patched injection.py successfully")

