import sqlite3
import os

db_path = os.path.expanduser('~/caeron-gateway/gateway.db')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Find duplicate round summaries (same prompt/text pattern) active
cur.execute("SELECT id, created_at FROM summaries WHERE tag = 'round' AND is_active = 1 ORDER BY created_at DESC")
rows = cur.fetchall()

print(f"Total active round summaries: {len(rows)}")
for r in rows:
    print(f"ID={r['id']}, created_at={r['created_at']}")

# We don't want to blindly archive everything.
# Just archive anything with ID 189 if it exists and is active.
cur.execute("SELECT id FROM summaries WHERE tag = 'round' AND id = 189 AND is_active = 1")
r189 = cur.fetchone()
if r189:
    cur.execute("UPDATE summaries SET is_active = 0 WHERE id = 189")
    conn.commit()
    print("Archived ID 189.")

conn.close()

