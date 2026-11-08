
import asyncio, sqlite3, json
from datetime import datetime
import sys
sys.path.insert(0, "/home/ubuntu/caeron-gateway")
from memory_extractor import extract_fragments
from milestone_extractor import extract_milestones

DB_PATH = "/home/ubuntu/caeron-gateway/gateway.db"
CANDIDATES_MD_PATH = "/home/ubuntu/caeron-gateway/data/core_memory_candidates.md"

async def topup_jan_and_regenerate_md():
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # Fetch all 2026-01 and 2026-03-21..22 dialogue memories
    cur.execute(
        """SELECT id, content, created_at FROM memories
           WHERE category = 'dialogue'
           AND (created_at LIKE '2026-01%' OR created_at LIKE '2026-03-21%' OR created_at LIKE '2026-03-22%')
           ORDER BY created_at ASC"""
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    batches = [rows[i:i+8] for i in range(0, len(rows), 8)]
    for b in batches:
        d_text = ""
        d_ids = []
        for idx, r in enumerate(b):
            d_text += f"[轮次{idx}] (memories_id={r['id']})\n{r['content']}\n\n"
            d_ids.append(r["id"])
        last_ts = b[-1]["created_at"]
        await extract_fragments(d_text, d_ids)
        await extract_milestones(d_text, d_ids, source_timestamp=last_ts)
        await asyncio.sleep(0.5)

    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("UPDATE memory_fragments SET created_at = source_timestamp WHERE source_timestamp < '2026-04-01' AND created_at != source_timestamp")
    cur.execute("UPDATE memory_milestones SET created_at = source_timestamp WHERE source_timestamp < '2026-04-01' AND created_at != source_timestamp")
    for m_tag in ["2026-01", "2026-03"]:
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
    print(f"FINAL_STATS: frags={len(hist_frags)}, miles={len(hist_miles)}")

asyncio.run(topup_jan_and_regenerate_md())
