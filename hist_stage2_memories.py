import asyncio
import json
import sqlite3
import re
import os
from datetime import datetime

DB_PATH = "/home/ubuntu/caeron-gateway/gateway.db"
JSON_PATH = "/home/ubuntu/caeron-gateway/data/hist_chats_filtered.json"
STAGE2_STATE_PATH = "/home/ubuntu/caeron-gateway/data/hist_stage2_state.json"
CANDIDATES_MD_PATH = "/home/ubuntu/caeron-gateway/data/core_memory_candidates.md"

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

async def run_stage2():
    import sys
    sys.path.insert(0, "/home/ubuntu/caeron-gateway")
    from embedding import get_embedding
    from memory_extractor import extract_fragments
    from milestone_extractor import extract_milestones
    from summarizer import get_summarizer
    from database import get_db

    by_date = get_clean_messages_by_date()
    all_days = sorted(d for d in by_date.keys() if d != "2025-05-18")

    state = {"completed_days": ["2025-05-17", "2025-05-19", "2025-05-20"]}
    if os.path.exists(STAGE2_STATE_PATH):
        try:
            with open(STAGE2_STATE_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if "completed_days" in loaded:
                    state["completed_days"] = list(set(state["completed_days"] + loaded["completed_days"]))
        except Exception:
            pass

    done_set = set(state["completed_days"])
    todo_days = [d for d in all_days if d not in done_set]
    print(f"[STAGE 2] Total days: {len(all_days)}, already done: {len(done_set)}, remaining: {len(todo_days)}", flush=True)

    emb_sem = asyncio.Semaphore(10)
    ext_sem = asyncio.Semaphore(6)
    db_lock = asyncio.Lock()

    for idx_d, day in enumerate(todo_days, 1):
        day_msgs = by_date[day]
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

        if not pairs:
            done_set.add(day)
            continue

        async def embed_pair(u_m, a_m):
            async with emb_sem:
                combined = f"User: {u_m['content']}\nAssistant: {a_m['content']}"
                emb = await get_embedding(combined)
                emb_json = json.dumps(emb) if emb else None
                ts = a_m["created_at"]
                return (combined, emb_json, ts)

        embedded_list = await asyncio.gather(*(embed_pair(u, a) for u, a in pairs))

        # Batch insert into memories
        inserted_pairs = []
        async with db_lock:
            conn = sqlite3.connect(DB_PATH, timeout=60.0)
            cur = conn.cursor()
            for combined, emb_json, ts in embedded_list:
                cur.execute(
                    """INSERT INTO memories (content, category, embedding, created_at, updated_at)
                       VALUES (?, 'dialogue', ?, ?, ?)""",
                    (combined, emb_json, ts, ts)
                )
                inserted_pairs.append((cur.lastrowid, combined, ts))
            conn.commit()
            conn.close()

        # Run fragment + milestone extraction in 8-turn batches concurrently
        batches = [inserted_pairs[w:w + 8] for w in range(0, len(inserted_pairs), 8)]

        async def extract_batch(batch):
            async with ext_sem:
                dialogue_text = ""
                dialogue_ids = []
                for idx_in_b, (m_id, comb_text, ts) in enumerate(batch):
                    dialogue_text += f"[轮次{idx_in_b}] (memories_id={m_id})\n{comb_text}\n\n"
                    dialogue_ids.append(m_id)
                last_ts = batch[-1][2]
                frags = await extract_fragments(dialogue_text, dialogue_ids)
                miles = await extract_milestones(dialogue_text, dialogue_ids, source_timestamp=last_ts)
                return (len(frags) if frags else 0, len(miles) if miles else 0)

        results = await asyncio.gather(*(extract_batch(b) for b in batches))
        f_cnt = sum(r[0] for r in results)
        m_cnt = sum(r[1] for r in results)

        # Anchor created_at to historical source_timestamp
        async with db_lock:
            conn = sqlite3.connect(DB_PATH, timeout=60.0)
            cur = conn.cursor()
            cur.execute(
                "UPDATE memory_fragments SET created_at = source_timestamp WHERE source_timestamp LIKE ? AND created_at != source_timestamp",
                (f"{day}%",)
            )
            cur.execute(
                "UPDATE memory_milestones SET created_at = source_timestamp WHERE source_timestamp LIKE ? AND created_at != source_timestamp",
                (f"{day}%",)
            )
            conn.commit()
            conn.close()

        done_set.add(day)
        state["completed_days"] = sorted(done_set)
        with open(STAGE2_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

        print(f"[STAGE 2] ({idx_d}/{len(todo_days)}) {day}: pairs={len(pairs)}, frags={f_cnt}, milestones={m_cnt}", flush=True)


    # Keep top 15 highest-intensity milestones per historical month (< 2026-04)
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT month_tag FROM memory_milestones WHERE month_tag < '2026-04' AND is_active = 1")
    h_months = [r[0] for r in cur.fetchall() if r[0]]
    for m_tag in h_months:
        cur.execute(
            """UPDATE memory_milestones SET is_active = 0
               WHERE month_tag = ? AND is_active = 1
               AND id NOT IN (
                   SELECT id FROM memory_milestones
                   WHERE month_tag = ? AND is_active = 1
                   ORDER BY intensity DESC, id DESC LIMIT 15
               )""",
            (m_tag, m_tag)
        )
    conn.commit()
    conn.close()

    # Generate core.md candidate list markdown
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """SELECT id, content, tags, source_timestamp FROM memory_fragments
           WHERE is_active = 1 AND source_timestamp < '2026-04-01'
           ORDER BY tags ASC, source_timestamp ASC"""
    )
    hist_frags = [dict(r) for r in cur.fetchall()]
    cur.execute(
        """SELECT id, category, event_anchor, subconscious_residue, intensity, source_timestamp, month_tag
           FROM memory_milestones
           WHERE is_active = 1 AND month_tag < '2026-04'
           ORDER BY month_tag ASC, intensity DESC, source_timestamp ASC"""
    )
    hist_miles = [dict(r) for r in cur.fetchall()]
    conn.close()

    md_lines = [
        "# 《core.md 历史核心记忆新增候选清单》(2025.05 ~ 2026.03)",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 记忆碎片总数：{len(hist_frags)} 条 | 活跃情感里程碑总数：{len(hist_miles)} 条",
        "",
        "## 一、 经过 L1+L2 双重防幻觉校验的事实记忆碎片（按分类汇总）",
        ""
    ]
    by_tag = {}
    for hf in hist_frags:
        by_tag.setdefault(hf["tags"] or "未分类", []).append(hf)
    for t_name, items in by_tag.items():
        md_lines.append(f"### 【{t_name}】（共 {len(items)} 条）")
        for it in items:
            md_lines.append(f"- `[{it['source_timestamp'][:10]}]` {it['content']} *(id={it['id']})*")
        md_lines.append("")

    md_lines.append("## 二、 2025.05 ~ 2026.03 月度优胜情感里程碑（Top 15/月）")
    md_lines.append("")
    by_m = {}
    for hm in hist_miles:
        by_m.setdefault(hm["month_tag"], []).append(hm)
    for m_tag in sorted(by_m.keys()):
        md_lines.append(f"### 📅 {m_tag}（共 {len(by_m[m_tag])} 条）")
        for hm in by_m[m_tag]:
            md_lines.append(f"- **[烈度 {hm['intensity']} | {hm['category']} | {hm['source_timestamp'][:16]}]**")
            md_lines.append(f"  - **事件锚点**：{hm['event_anchor']}")
            md_lines.append(f"  - **潜意识印记**：{hm['subconscious_residue']}")
        md_lines.append("")

    with open(CANDIDATES_MD_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
    print(f"[STAGE 2 COMPLETE] Saved candidate list to {CANDIDATES_MD_PATH}!", flush=True)

if __name__ == "__main__":
    asyncio.run(run_stage2())
