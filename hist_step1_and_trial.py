import asyncio
import json
import sqlite3
import re
import os
import time
from datetime import datetime

DB_PATH = "/home/ubuntu/caeron-gateway/gateway.db"
JSON_PATH = "/home/ubuntu/caeron-gateway/data/hist_chats_filtered.json"
STATE_PATH = "/home/ubuntu/caeron-gateway/data/hist_import_state.json"

def patch_gateway_queries():
    p1 = "/home/ubuntu/caeron-gateway/message_store.py"
    with open(p1, "r", encoding="utf-8") as f:
        s = f.read()
    s_new = s.replace(
        "SELECT conversation_id, created_at FROM messages WHERE role = 'user' AND conversation_id NOT LIKE 'qq-%' ORDER BY id DESC LIMIT 1",
        "SELECT conversation_id, created_at FROM messages WHERE role = 'user' AND conversation_id NOT LIKE 'qq-%' AND conversation_id NOT LIKE 'hist_%' ORDER BY created_at DESC, id DESC LIMIT 1"
    ).replace(
        "SELECT conversation_id FROM messages WHERE role = 'user' AND content = ? AND conversation_id NOT LIKE 'qq-%' ORDER BY id DESC LIMIT 1",
        "SELECT conversation_id FROM messages WHERE role = 'user' AND content = ? AND conversation_id NOT LIKE 'qq-%' AND conversation_id NOT LIKE 'hist_%' ORDER BY created_at DESC, id DESC LIMIT 1"
    ).replace(
        "SELECT id FROM memories WHERE category = 'dialogue' ORDER BY id DESC LIMIT 1",
        "SELECT id FROM memories WHERE category = 'dialogue' ORDER BY created_at DESC, id DESC LIMIT 1"
    ).replace(
        "SELECT id, content FROM memories WHERE category = 'dialogue' ORDER BY id DESC LIMIT 8",
        "SELECT id, content FROM memories WHERE category = 'dialogue' ORDER BY created_at DESC, id DESC LIMIT 8"
    )
    if s_new != s:
        with open(p1, "w", encoding="utf-8") as f:
            f.write(s_new)
        print("[PATCH] Updated message_store.py ORDER BY clauses.")

    p2 = "/home/ubuntu/caeron-gateway/proactive.py"
    with open(p2, "r", encoding="utf-8") as f:
        s2 = f.read()
    s2_new = s2.replace(
        "SELECT content FROM unified_messages \n            WHERE role = 'user' \n            ORDER BY id DESC LIMIT 20",
        "SELECT content FROM unified_messages \n            WHERE role = 'user' \n            ORDER BY timestamp DESC, id DESC LIMIT 20"
    )
    if s2_new != s2:
        with open(p2, "w", encoding="utf-8") as f:
            f.write(s2_new)
        print("[PATCH] Updated proactive.py ORDER BY clause.")

def step1_import_raw_messages():
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        convs = json.load(f)

    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cur = conn.cursor()

    # Create index on memories(created_at DESC) if not exists
    cur.execute("CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC)")

    # Check if already imported
    cur.execute("SELECT COUNT(*) FROM conversations WHERE conversation_id LIKE 'hist_%'")
    existing_cnt = cur.fetchone()[0]
    if existing_cnt > 0:
        print(f"[STEP 1] Already imported {existing_cnt} hist conversations, skipping raw insert.")
        conn.close()
        return

    # Create dedicated archive window in windows table
    cur.execute("SELECT id FROM windows WHERE name LIKE '历史归档窗%' LIMIT 1")
    w_row = cur.fetchone()
    if w_row:
        hist_win_id = w_row[0]
    else:
        cur.execute(
            "INSERT INTO windows (name, description, color, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("历史归档窗 (2025.05-2026.03)", "ChatGPT二号与Rikkahub时期历史对话归档 (273窗)", "#9b87f5", "2025-05-17 03:56:25", "2026-03-22 11:30:22")
        )
        hist_win_id = cur.lastrowid

    conv_rows = []
    msg_rows = []
    unified_rows = []

    for c in convs:
        cid = c["conversation_id"]
        title = c["title"]
        msgs = c["messages"]
        started_at = c["started_at"]
        last_at = c["last_message_at"]
        model = c.get("model") or "gpt-4o"

        conv_rows.append((cid, None, model, started_at, last_at, len(msgs), 0, started_at, hist_win_id))

        for idx, m in enumerate(msgs):
            role = m["role"]
            content = m["content"] or ""
            created_at = m["created_at"]
            char_len = len(content)
            msg_rows.append((cid, role, content, idx, char_len, created_at))
            meta = json.dumps({"conversation_id": cid, "title": title, "model": m.get("model", model)}, ensure_ascii=False)
            unified_rows.append((created_at, "hist_import", title, role, content, char_len, meta))

    cur.executemany(
        """INSERT INTO conversations 
           (conversation_id, provider_id, model, started_at, last_message_at, message_count, is_active, created_at, window_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        conv_rows
    )
    cur.executemany(
        """INSERT INTO messages 
           (conversation_id, role, content, message_index, token_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        msg_rows
    )
    cur.executemany(
        """INSERT INTO unified_messages 
           (timestamp, source, source_context, role, content, char_count, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        unified_rows
    )
    conn.commit()
    conn.close()
    print(f"[STEP 1] Successfully inserted {len(conv_rows)} conversations, {len(msg_rows)} messages, and {len(unified_rows)} unified_messages (window_id={hist_win_id}).")

def clean_msg_for_summary(content: str) -> str:
    if not content:
        return ""
    # Strip [分支 k/N] prefix
    content = re.sub(r'^\[分支 \d+/\d+\]\s*', '', content)
    # Strip <thinking>...</thinking>
    content = re.sub(r'<thinking>[\s\S]*?</thinking>\s*', '', content)
    # Strip leading timestamp header if present
    return content.strip()

def get_clean_messages_by_date():
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        convs = json.load(f)
    by_date = {}
    for c in convs:
        msgs = c["messages"]
        for i, m in enumerate(msgs):
            # Skip superseded Rikkahub reroll branch if next message is same role and starts with [分支 k/N]
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
                "raw_content": m["content"],
                "created_at": m["created_at"]
            })
    for day in by_date:
        by_date[day].sort(key=lambda x: x["created_at"])
    return by_date

async def run_trial_days(trial_days):
    import sys
    sys.path.insert(0, "/home/ubuntu/caeron-gateway")
    from summarizer import get_summarizer, LEVEL_CONFIG
    from embedding import get_embedding
    from memory_extractor import extract_fragments
    from milestone_extractor import extract_milestones
    from database import get_db

    summarizer = get_summarizer()
    by_date = get_clean_messages_by_date()

    # Load or init checkpoint state
    state = {"completed_days": {}}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass

    for day in trial_days:
        day_msgs = by_date.get(day, [])
        if not day_msgs:
            print(f"[TRIAL] No messages on {day}, skipping.")
            continue

        print(f"\n==================== Processing Day {day} ({len(day_msgs)} clean messages) ====================")

        # 1. Chunk day_msgs into rounds of ~60 messages and generate round summaries
        CHUNK_SIZE = 60
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
            raw_round = await summarizer._call_llm(cfg["prompt"], "\n".join(user_parts), cfg["max_tokens"], timeout=120.0)
            if not raw_round:
                print(f"[WARN] Round {c_idx+1}/{len(chunks)} for {day} failed, retrying once...")
                await asyncio.sleep(5)
                raw_round = await summarizer._call_llm(cfg["prompt"], "\n".join(user_parts), cfg["max_tokens"], timeout=120.0)

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

            # Save archived round summary (is_active=0 so historical rounds don't pollute live context)
            db = await get_db()
            try:
                cur = await db.execute(
                    """INSERT INTO summaries 
                       (conversation_id, level, tag, content, message_range_start, message_range_end,
                        period_start, period_end, is_active, token_count, category, valence, arousal, anchor, created_at)
                       VALUES ('_global_', 'round', 'round', ?, 0, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
                    (content, len(chunk), p_start, p_end, len(content), category, valence, arousal, anchor, p_end)
                )
                r_id = cur.lastrowid
                await db.commit()
            finally:
                await db.close()

            round_records.append({
                "id": r_id,
                "content": content,
                "created_at": p_end,
                "category": category,
                "valence": valence,
                "arousal": arousal,
                "anchor": anchor
            })
            print(f"  [Round {c_idx+1}/{len(chunks)}] id={r_id}, range={p_start[11:16]}~{p_end[11:16]}, cat={category}, val={valence}, aro={arousal}, anchor={anchor}")

        # 2. Generate Daily Summary from the day's round summaries using exact DAILY_SUMMARY_PROMPT
        lines = [f"日期: {day}", f"当天轮总（{len(round_records)}条）："]
        for rr in round_records:
            lines.append(f"[{rr['created_at']}] {rr['content']}")
        lines.append("\n请用200字以内总结今天的全部事实。")

        d_cfg = LEVEL_CONFIG["daily"]
        daily_text = await summarizer._call_llm(d_cfg["prompt"], "\n".join(lines), d_cfg["max_tokens"], timeout=180.0)
        if not daily_text:
            await asyncio.sleep(5)
            daily_text = await summarizer._call_llm(d_cfg["prompt"], "\n".join(lines), d_cfg["max_tokens"], timeout=180.0)

        # Save daily summary (is_active=0 so historical daily doesn't inject into '本周记忆·日度', while ready for weekly/monthly rollup)
        db = await get_db()
        try:
            cur = await db.execute(
                """INSERT INTO summaries 
                   (conversation_id, level, tag, content, message_range_start, message_range_end,
                    period_start, period_end, is_active, token_count, created_at)
                   VALUES ('_global_', 'daily', 'daily', ?, 0, ?, ?, ?, 0, ?, ?)""",
                (daily_text, len(day_msgs), f"{day} 00:00:00", f"{day} 23:59:59", len(daily_text or ""), f"{day} 23:59:00")
            )
            daily_id = cur.lastrowid
            await db.commit()
        finally:
            await db.close()

        print(f"  [Daily Summary Saved] id={daily_id}:\n{daily_text}\n")

        # 3. Pair User+Assistant turns -> insert into memories (category='dialogue', anchored to historical timestamp!) + Embedding + Extract Fragments & Milestones
        pairs = []
        i = 0
        while i < len(day_msgs):
            if day_msgs[i]["role"] == "user":
                u_msg = day_msgs[i]
                a_msg = day_msgs[i + 1] if (i + 1 < len(day_msgs) and day_msgs[i + 1]["role"] == "assistant") else None
                if a_msg:
                    pairs.append((u_msg, a_msg))
                    i += 2
                    continue
            i += 1

        print(f"  [Dialogue Pairs] Found {len(pairs)} (User, Assistant) pairs on {day}. Generating Embeddings & extracting memories...")
        sem = asyncio.Semaphore(6)

        async def embed_and_insert_pair(u_m, a_m):
            async with sem:
                combined = f"User: {u_m['content']}\nAssistant: {a_m['content']}"
                emb = await get_embedding(combined)
                emb_json = json.dumps(emb) if emb else None
                ts = a_m["created_at"]
                db_conn = await get_db()
                try:
                    c_cur = await db_conn.execute(
                        """INSERT INTO memories (content, category, embedding, created_at, updated_at)
                           VALUES (?, 'dialogue', ?, ?, ?)""",
                        (combined, emb_json, ts, ts)
                    )
                    m_id = c_cur.lastrowid
                    await db_conn.commit()
                    return (m_id, combined, ts)
                finally:
                    await db_conn.close()

        inserted_pairs = await asyncio.gather(*(embed_and_insert_pair(u, a) for u, a in pairs))
        inserted_pairs.sort(key=lambda x: x[2])

        # Run extract_fragments & extract_milestones in 8-turn windows (matching message_store.py)
        extracted_frags_count = 0
        extracted_miles_count = 0
        for w_idx in range(0, len(inserted_pairs), 8):
            batch = inserted_pairs[w_idx:w_idx + 8]
            dialogue_text = ""
            dialogue_ids = []
            for idx_in_b, (m_id, comb_text, ts) in enumerate(batch):
                dialogue_text += f"[轮次{idx_in_b}] (memories_id={m_id})\n{comb_text}\n\n"
                dialogue_ids.append(m_id)
            last_ts = batch[-1][2]

            frags = await extract_fragments(dialogue_text, dialogue_ids)
            if frags:
                extracted_frags_count += len(frags)
            miles = await extract_milestones(dialogue_text, dialogue_ids, source_timestamp=last_ts)
            if miles:
                extracted_miles_count += len(miles)

        # Ensure created_at on memory_fragments and memory_milestones is anchored to historical source_timestamp
        db_fix = await get_db()
        try:
            await db_fix.execute(
                "UPDATE memory_fragments SET created_at = source_timestamp WHERE source_timestamp LIKE ? AND created_at != source_timestamp",
                (f"{day}%",)
            )
            await db_fix.execute(
                "UPDATE memory_milestones SET created_at = source_timestamp WHERE source_timestamp LIKE ? AND created_at != source_timestamp",
                (f"{day}%",)
            )
            await db_fix.commit()
        finally:
            await db_fix.close()

        print(f"  [Memory Extraction Done for {day}] dialogue_memories={len(inserted_pairs)}, valid_fragments={extracted_frags_count}, milestones={extracted_miles_count}")

        state["completed_days"][day] = {
            "messages": len(day_msgs),
            "rounds": len(round_records),
            "daily_id": daily_id,
            "dialogue_pairs": len(inserted_pairs),
            "fragments": extracted_frags_count,
            "milestones": extracted_miles_count,
            "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    patch_gateway_queries()
    step1_import_raw_messages()
    # First 3 real chat days (2025-05-18 only had 1 knowledge-cutoff test turn)
    asyncio.run(run_trial_days(["2025-05-17", "2025-05-19", "2025-05-20"]))
