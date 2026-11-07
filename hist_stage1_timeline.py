import asyncio
import json
import sqlite3
import re
import os
import calendar
from datetime import datetime, timedelta

DB_PATH = "/home/ubuntu/caeron-gateway/gateway.db"
JSON_PATH = "/home/ubuntu/caeron-gateway/data/hist_chats_filtered.json"
STAGE1_LOG_STATE = "/home/ubuntu/caeron-gateway/data/hist_stage1_state.json"

def patch_milestone_extractor():
    p = "/home/ubuntu/caeron-gateway/milestone_extractor.py"
    with open(p, "r", encoding="utf-8") as f:
        s = f.read()
    s_new = s.replace('"max_tokens": 512', '"max_tokens": 2000')
    if s_new != s:
        with open(p, "w", encoding="utf-8") as f:
            f.write(s_new)
        print("[PATCH] Updated milestone_extractor.py prune_monthly_milestones max_tokens: 512 -> 2000")
    else:
        print("[PATCH] milestone_extractor.py max_tokens already 2000")

    # Sync any 2025-05 timestamps from trial run
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cur = conn.cursor()
    cur.execute("UPDATE memory_fragments SET created_at = source_timestamp WHERE source_timestamp < '2026-04-01' AND created_at != source_timestamp")
    cur.execute("UPDATE memory_milestones SET created_at = source_timestamp WHERE source_timestamp < '2026-04-01' AND created_at != source_timestamp")
    conn.commit()
    conn.close()

def clean_msg_for_summary(content: str) -> str:
    if not content:
        return ""
    content = re.sub(r'^\[分支 \d+/\d+\]\s*', '', content)
    content = re.sub(r'<thinking>[\s\S]*?</thinking>\s*', '', content)
    return content.strip()

def get_clean_messages_by_date():
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        convs = json.load(f)
    by_date = {}
    for c in convs:
        msgs = c["messages"]
        for i, m in enumerate(msgs):
            if i + 1 < len(msgs):
                nxt = msgs[i + 1]
                if nxt["role"] == m["role"] and re.match(r'^\[分支 \d+/\d+\]', nxt.get("content") or ""):
                    continue
            day = m["created_at"][:10]
            by_date.setdefault(day, []).append({
                "conversation_id": c["conversation_id"],
                "title": c["title"],
                "role": m["role"],
                "content": clean_msg_for_summary(m["content"]),
                "created_at": m["created_at"]
            })
    for day in by_date:
        by_date[day].sort(key=lambda x: x["created_at"])
    return by_date

async def run_stage1():
    import sys
    sys.path.insert(0, "/home/ubuntu/caeron-gateway")
    from summarizer import get_summarizer, LEVEL_CONFIG

    summarizer = get_summarizer()
    by_date = get_clean_messages_by_date()
    all_days = sorted(d for d in by_date.keys() if d != "2025-05-18")

    # Check which days already have daily summaries in DB
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cur = conn.cursor()
    cur.execute("SELECT substr(period_start, 1, 10) FROM summaries WHERE level = 'daily' AND period_start < '2026-04-01'")
    done_days = set(r[0] for r in cur.fetchall() if r[0])
    conn.close()

    todo_days = [d for d in all_days if d not in done_days]
    print(f"[STAGE 1] Total valid historical days: {len(all_days)}, already done: {len(done_days)}, remaining: {len(todo_days)}", flush=True)

    db_write_lock = asyncio.Lock()
    day_sem = asyncio.Semaphore(6)  # 6 concurrent days to avoid SiliconFlow rate limits
    completed_counter = 0

    async def call_llm_with_retry(prompt, user_text, max_tokens, timeout=150.0):
        for attempt in range(8):
            res = await summarizer._call_llm(prompt, user_text, max_tokens, timeout=timeout)
            if res:
                return res
            await asyncio.sleep(5 * (attempt + 1))
        return None

    async def process_one_day(day: str):
        nonlocal completed_counter
        async with day_sem:
            day_msgs = by_date[day]
            CHUNK_SIZE = 65
            chunks = [day_msgs[i:i + CHUNK_SIZE] for i in range(0, len(day_msgs), CHUNK_SIZE)]
            round_records = []
            prev_round_text = None

            for c_idx, chunk in enumerate(chunks):
                formatted_lines = []
                total_chars = 0
                for msg in chunk:
                    c_text = msg["content"]
                    if len(c_text) > 1500:
                        c_text = c_text[:800] + "\n...[内容截断]...\n" + c_text[-400:]
                    role_label = "蕊蕊" if msg["role"] == "user" else "沈栖"
                    line = f"[{msg['created_at']}] {role_label}: {c_text}"
                    total_chars += len(line)
                    if total_chars > 28000:
                        formatted_lines.append("...[更早的消息已省略]...")
                        break
                    formatted_lines.append(line)

                user_parts = []
                if prev_round_text:
                    user_parts.append(f"上轮总结：{prev_round_text}\n")
                user_parts.append(f"本轮消息（{len(chunk)}条）：\n")
                user_parts.append("\n".join(formatted_lines))
                user_parts.append("\n请严格按[正文][情绪效价][情绪强度][记忆锚点][任务状态]五字段格式输出，正文按【日常】【技术】【学习】分段，只输出涉及的类别。")

                cfg = LEVEL_CONFIG["round"]
                raw_round = await call_llm_with_retry(cfg["prompt"], "\n".join(user_parts), cfg["max_tokens"])
                content = raw_round or "本轮无有效对话内容。"
                m_content = re.search(r'\[正文\]\s*(.+?)(?=\n\[|$)', content, re.DOTALL)
                if m_content:
                    content = m_content.group(1).strip()

                cats = [k for k in ["日常", "技术", "学习"] if f"【{k}】" in content]
                category = ",".join(cats) if cats else "日常"

                valence, arousal, anchor = None, None, None
                m_val = re.search(r'\[(?:情绪效价|valence)\]\s*(-?[\d.]+)', raw_round or "")
                if m_val:
                    try: valence = max(-1.0, min(1.0, float(m_val.group(1))))
                    except: pass
                m_aro = re.search(r'\[(?:情绪强度|arousal)\]\s*([\d.]+)', raw_round or "")
                if m_aro:
                    try: arousal = max(0.0, min(1.0, float(m_aro.group(1))))
                    except: pass
                m_anc = re.search(r'\[(?:记忆锚点|anchor)\]\s*(.+?)(?=\n|$)', raw_round or "")
                if m_anc:
                    anchor = m_anc.group(1).strip()[:50]

                prev_round_text = content
                p_start = chunk[0]["created_at"]
                p_end = chunk[-1]["created_at"]

                round_records.append({
                    "content": content,
                    "msg_count": len(chunk),
                    "p_start": p_start,
                    "p_end": p_end,
                    "category": category,
                    "valence": valence,
                    "arousal": arousal,
                    "anchor": anchor
                })

            # Generate Daily Summary
            if len(round_records) == 1 and len(day_msgs) <= 15:
                # Still run DAILY_SUMMARY_PROMPT so format is strictly [MM月DD日]
                pass
            lines = [f"日期: {day}", f"当天轮总（{len(round_records)}条）："]
            for rr in round_records:
                lines.append(f"[{rr['p_end']}] {rr['content']}")
            lines.append("\n请用200字以内总结今天的全部事实。")

            d_cfg = LEVEL_CONFIG["daily"]
            daily_text = await call_llm_with_retry(d_cfg["prompt"], "\n".join(lines), d_cfg["max_tokens"])
            if not daily_text:
                daily_text = f"[{day[5:7]}月{day[8:10]}日]\n\n" + "\n".join(rr["content"] for rr in round_records)[:280]

            # Save rounds + daily atomically under lock
            async with db_write_lock:
                conn = sqlite3.connect(DB_PATH, timeout=60.0)
                cur = conn.cursor()
                for rr in round_records:
                    cur.execute(
                        """INSERT INTO summaries 
                           (conversation_id, level, tag, content, message_range_start, message_range_end,
                            period_start, period_end, is_active, token_count, category, valence, arousal, anchor, created_at)
                           VALUES ('_global_', 'round', 'round', ?, 0, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
                        (rr["content"], rr["msg_count"], rr["p_start"], rr["p_end"], len(rr["content"]),
                         rr["category"], rr["valence"], rr["arousal"], rr["anchor"], rr["p_end"])
                    )
                cur.execute(
                    """INSERT INTO summaries 
                       (conversation_id, level, tag, content, message_range_start, message_range_end,
                        period_start, period_end, is_active, token_count, created_at)
                       VALUES ('_global_', 'daily', 'daily', ?, 0, ?, ?, ?, 0, ?, ?)""",
                    (daily_text, len(day_msgs), f"{day} 00:00:00", f"{day} 23:59:59", len(daily_text), f"{day} 23:59:00")
                )
                conn.commit()
                conn.close()

            completed_counter += 1
            if completed_counter % 10 == 0 or completed_counter == len(todo_days):
                print(f"[DAILY PROGRESS] {completed_counter}/{len(todo_days)} finished (latest: {day}, rounds={len(round_records)})", flush=True)

    await asyncio.gather(*(process_one_day(d) for d in todo_days))
    print("[STAGE 1 - DAILY] All 298 historical days Round + Daily summaries completed!", flush=True)

    # ==================== STEP 2B: WEEKLY SUMMARIES ====================
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """SELECT id, content, substr(period_start, 1, 10) as day_str, created_at
           FROM summaries
           WHERE level = 'daily' AND period_start >= '2025-05-01' AND period_start < '2026-04-01'
           ORDER BY period_start ASC"""
    )
    all_dailies = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT substr(period_start, 1, 10) FROM summaries WHERE level = 'weekly' AND period_start < '2026-04-01'")
    existing_weeks = set(r[0] for r in cur.fetchall() if r[0])
    conn.close()

    # Group dailies by natural Monday..Sunday week
    weeks_map = {}
    for d_row in all_dailies:
        dt = datetime.strptime(d_row["day_str"], "%Y-%m-%d")
        monday = dt - timedelta(days=dt.weekday())
        sunday = monday + timedelta(days=6)
        w_key = (monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d"))
        weeks_map.setdefault(w_key, []).append(d_row)

    sorted_weeks = sorted(weeks_map.keys())
    todo_weeks = [w for w in sorted_weeks if w[0] not in existing_weeks]
    print(f"[STAGE 1 - WEEKLY] Total weeks: {len(sorted_weeks)}, remaining: {len(todo_weeks)}", flush=True)

    week_sem = asyncio.Semaphore(8)

    async def process_one_week(w_start, w_end):
        async with week_sem:
            d_list = weeks_map[(w_start, w_end)]
            lines = [f"周期: {w_start} ~ {w_end}", f"本周日总（{len(d_list)}条）："]
            for ds in d_list:
                lines.append(f"[{ds['day_str']}] {ds['content']}")
            lines.append("\n请用300字以内总结本周的全部事实。")
            w_cfg = LEVEL_CONFIG["weekly"]
            w_text = await call_llm_with_retry(w_cfg["prompt"], "\n".join(lines), w_cfg["max_tokens"])
            if not w_text:
                w_text = f"[{w_start[5:]} ~ {w_end[5:]}]\n" + "\n".join(ds["content"] for ds in d_list)[:380]

            async with db_write_lock:
                conn = sqlite3.connect(DB_PATH, timeout=60.0)
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO summaries
                       (conversation_id, level, tag, content, message_range_start, message_range_end,
                        period_start, period_end, is_active, token_count, created_at)
                       VALUES ('_global_', 'weekly', 'weekly', ?, 0, ?, ?, ?, 0, ?, ?)""",
                    (w_text, len(d_list), f"{w_start} 00:00:00", f"{w_end} 23:59:59", len(w_text), f"{w_end} 23:59:30")
                )
                conn.commit()
                conn.close()
            print(f"  [Weekly Saved] {w_start} ~ {w_end} ({len(d_list)} days)", flush=True)

    await asyncio.gather(*(process_one_week(ws, we) for ws, we in todo_weeks))
    print("[STAGE 1 - WEEKLY] All historical Weekly summaries completed!", flush=True)

    # ==================== STEP 2C: MONTHLY SUMMARIES (2025-05 to 2026-03) ====================
    target_months = [
        "2025-05", "2025-06", "2025-07", "2025-08", "2025-09",
        "2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03"
    ]
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT substr(period_start, 1, 7) FROM summaries WHERE level = 'monthly' AND period_start < '2026-04-01'")
    existing_months = set(r[0] for r in cur.fetchall() if r[0])
    conn.close()

    for ym in target_months:
        if ym in existing_months:
            print(f"  [Monthly] {ym} already exists, skipping.", flush=True)
            continue
        y_int, m_int = int(ym[:4]), int(ym[5:7])
        _, last_day = calendar.monthrange(y_int, m_int)
        m_start = f"{ym}-01"
        m_end = f"{ym}-{last_day:02d}"

        conn = sqlite3.connect(DB_PATH, timeout=60.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """SELECT id, content, period_start, period_end, created_at FROM summaries
               WHERE level = 'weekly'
               AND (date(period_end) BETWEEN ? AND ? OR date(period_start) BETWEEN ? AND ?)
               ORDER BY period_start ASC""",
            (m_start, m_end, m_start, m_end)
        )
        w_rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        lines = [f"月份: {ym}", f"本月周总（{len(w_rows)}条）："]
        for ws in w_rows:
            lines.append(f"[{ws['period_start'][:10]}~{ws['period_end'][:10]}] {ws['content']}")
        lines.append("\n请用400字以内总结本月的全部事实。")

        m_cfg = LEVEL_CONFIG["monthly"]
        m_text = await call_llm_with_retry(m_cfg["prompt"], "\n".join(lines), m_cfg["max_tokens"])
        # Per Shen Qi's instruction: keep 2026-03 monthly active (is_active=1), archive earlier months once quarterly is built
        is_act = 1 if ym == "2026-03" else 0

        conn = sqlite3.connect(DB_PATH, timeout=60.0)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO summaries
               (conversation_id, level, tag, content, message_range_start, message_range_end,
                period_start, period_end, is_active, token_count, created_at)
               VALUES ('_global_', 'monthly', 'monthly', ?, 0, ?, ?, ?, ?, ?, ?)""",
            (m_text, len(w_rows), f"{m_start} 00:00:00", f"{m_end} 23:59:59", is_act, len(m_text or ""), f"{m_end} 23:59:45")
        )
        conn.commit()
        conn.close()
        print(f"  [Monthly Saved] {ym} (is_active={is_act}, chars={len(m_text or '')})", flush=True)

    # ==================== STEP 2D: QUARTERLY SUMMARIES (2025-Q2, 2025-Q3, 2025-Q4, 2026-Q1) ====================
    quarters = [
        ("2025-Q2", "2025-04-01", "2025-06-30"),
        ("2025-Q3", "2025-07-01", "2025-09-30"),
        ("2025-Q4", "2025-10-01", "2025-12-31"),
        ("2026-Q1", "2026-01-01", "2026-03-31"),
    ]
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT substr(period_start, 1, 7) FROM summaries WHERE level = 'quarterly' AND period_start < '2026-04-01'")
    existing_q_starts = set(r[0] for r in cur.fetchall() if r[0])
    conn.close()

    for q_name, q_start, q_end in quarters:
        if q_start[:7] in existing_q_starts:
            print(f"  [Quarterly] {q_name} already exists, skipping.", flush=True)
            continue
        conn = sqlite3.connect(DB_PATH, timeout=60.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """SELECT id, content, period_start, period_end FROM summaries
               WHERE level = 'monthly'
               AND date(period_start) BETWEEN ? AND ?
               ORDER BY period_start ASC""",
            (q_start, q_end)
        )
        m_rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        lines = [f"季度: {q_name} ({q_start} ~ {q_end})", f"本季月总（{len(m_rows)}条）："]
        for ms in m_rows:
            m_tag = ms["period_start"][:7]
            lines.append(f"[{m_tag}月总] {ms['content']}")
        lines.append("\n请用600-800字总结本季度的宏观核心事实与里程碑。")

        q_cfg = LEVEL_CONFIG["quarterly"]
        q_text = await call_llm_with_retry(q_cfg["prompt"], "\n".join(lines), q_cfg["max_tokens"])

        conn = sqlite3.connect(DB_PATH, timeout=60.0)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO summaries
               (conversation_id, level, tag, content, message_range_start, message_range_end,
                period_start, period_end, is_active, token_count, created_at)
               VALUES ('_global_', 'quarterly', 'quarterly', ?, 0, ?, ?, ?, 1, ?, ?)""",
            (q_text, len(m_rows), f"{q_start} 00:00:00", f"{q_end} 23:59:59", len(q_text or ""), f"{q_end} 23:59:59")
        )
        conn.commit()
        conn.close()
        print(f"  [Quarterly Saved] {q_name} (is_active=1, chars={len(q_text or '')})", flush=True)

    print("\n================ STAGE 1 COMPLETE! ================", flush=True)

if __name__ == "__main__":
    patch_milestone_extractor()
    asyncio.run(run_stage1())
