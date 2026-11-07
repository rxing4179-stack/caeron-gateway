import sqlite3

DB_PATH = "/home/ubuntu/caeron-gateway/gateway.db"
conn = sqlite3.connect(DB_PATH, timeout=60.0)
cur = conn.cursor()

# 1. Remove fallback rounds and dailies that hit transient API rate limit during 2025-10-16 ~ 2025-12-10
cur.execute("SELECT DISTINCT substr(period_start, 1, 10) FROM summaries WHERE content LIKE '%本轮无有效对话内容%'")
bad_days = [r[0] for r in cur.fetchall() if r[0]]
print(f"[REPAIR] Cleaning {len(bad_days)} days that hit API rate limit ({bad_days[0] if bad_days else ''} ~ {bad_days[-1] if bad_days else ''})...")

for d in bad_days:
    cur.execute("DELETE FROM summaries WHERE level IN ('round', 'daily') AND substr(period_start, 1, 10) = ?", (d,))

# 2. Remove weekly summaries overlapping 2025-10-13 ~ 2025-12-14, monthly 2025-10..12, and quarterly 2025-Q4 so they regenerate cleanly
cur.execute("DELETE FROM summaries WHERE level = 'weekly' AND period_start >= '2025-10-13' AND period_start <= '2025-12-15'")
cur.execute("DELETE FROM summaries WHERE level = 'monthly' AND substr(period_start, 1, 7) IN ('2025-10', '2025-11', '2025-12')")
cur.execute("DELETE FROM summaries WHERE level = 'quarterly' AND substr(period_start, 1, 7) = '2025-10'")
conn.commit()
conn.close()
print("[REPAIR] Cleaned Q4 fallback rows. Ready to re-run hist_stage1_timeline.py for the 55 days.")

# 3. Also optimize milestone_extractor.py timeout from 30.0 to 120.0 and raise intensity threshold for batch pruning
p = "/home/ubuntu/caeron-gateway/milestone_extractor.py"
with open(p, "r", encoding="utf-8") as f:
    s = f.read()
s_new = s.replace("timeout=30.0", "timeout=120.0").replace("len(active_list) >= 15", "len(active_list) >= 50")
if s_new != s:
    with open(p, "w", encoding="utf-8") as f:
        f.write(s_new)
    print("[PATCH] Updated milestone_extractor.py timeout=120.0 and batch threshold=50.")
