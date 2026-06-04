import sqlite3

conn = sqlite3.connect('/home/ubuntu/caeron-gateway/gateway.db')
cursor = conn.cursor()
cursor.execute("SELECT key, value FROM config")
for row in cursor.fetchall():
    print(f"{row[0]}: {row[1]}")
conn.close()
