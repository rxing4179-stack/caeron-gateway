import sqlite3
db=sqlite3.connect('/home/ubuntu/caeron-gateway/gateway.db')
print("dialogue:", db.execute("SELECT created_at FROM memories WHERE category='dialogue' ORDER BY id DESC LIMIT 1").fetchone())
print("operit:", db.execute("SELECT created_at FROM memories WHERE category='operit' ORDER BY id DESC LIMIT 1").fetchone())
print("status:", db.execute("SELECT created_at FROM memories WHERE category='status' ORDER BY id DESC LIMIT 1").fetchone())
