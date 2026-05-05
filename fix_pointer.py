import os

sum_path = os.path.expanduser('~/caeron-gateway/summarizer.py')
with open(sum_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Fix 1: _get_global_messages should NOT use is_active=1 to find the read boundary
old_query = """                cursor = await db.execute(
                    \"\"\"SELECT created_at FROM summaries 
                       WHERE tag = 'round' AND is_active = 1 
                       ORDER BY created_at DESC LIMIT 1\"\"\"
                )"""

new_query = """                cursor = await db.execute(
                    \"\"\"SELECT created_at FROM summaries 
                       WHERE tag = 'round' 
                       ORDER BY created_at DESC LIMIT 1\"\"\"
                )"""

if old_query in content:
    content = content.replace(old_query, new_query)
    print("summarizer.py _get_global_messages patched successfully.")
else:
    print("Warning: Could not find old_query in summarizer.py")

with open(sum_path, 'w', encoding='utf-8') as f:
    f.write(content)

import sqlite3
db_path = os.path.expanduser('~/caeron-gateway/gateway.db')
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Check active rounds
cur.execute("SELECT id, created_at, is_active FROM summaries WHERE tag = 'round' ORDER BY created_at DESC LIMIT 5")
print("\nRecent round summaries:")
for r in cur.fetchall():
    print(r)

conn.close()

