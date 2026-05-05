import sqlite3
import os
db_path = os.path.expanduser('~/caeron-gateway/gateway.db')
conn = sqlite3.connect(db_path)
conn.execute("UPDATE summaries SET is_active=0 WHERE id=190")
conn.commit()
conn.close()
print("Archived ID 190")

