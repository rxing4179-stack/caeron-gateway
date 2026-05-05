import sqlite3
import os

db_path = os.path.expanduser('~/caeron-gateway/gateway.db')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("==== Recent Round Summaries ====")
cur.execute("SELECT id, is_active, created_at, SUBSTR(content, 1, 60) as content FROM summaries WHERE tag = 'round' ORDER BY id DESC LIMIT 10")
for r in cur.fetchall():
    print(dict(r))

print("\n==== Config table ====")
cur.execute("SELECT key, value FROM config WHERE key IN ('_msg_counter')")
for r in cur.fetchall():
    print(dict(r))

print("\n==== Latest Messages ====")
cur.execute("SELECT id, created_at, role, SUBSTR(content, 1, 40) as content FROM messages ORDER BY id DESC LIMIT 5")
for r in cur.fetchall():
    print(dict(r))

conn.close()

