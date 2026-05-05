import sqlite3
import os

db_path = os.path.expanduser('~/caeron-gateway/gateway.db')
conn = sqlite3.connect(db_path)
cur = conn.cursor()
cur.execute('SELECT name, LENGTH(content), role FROM injection_rules WHERE is_enabled = 1')
rows = cur.fetchall()
print("Enabled Injection Rules:")
for r in rows:
    print(f"Name: {r[0]}, Length: {r[1]}, Role: {r[2]}")
conn.close()

