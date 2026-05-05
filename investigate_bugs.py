import sqlite3
import os

db_path = os.path.expanduser('~/caeron-gateway/gateway.db')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Bug 2: 检查 ID=188 的幽灵轮总
print("=" * 60)
print("Bug 2: 检查 ID=188 的记录")
print("=" * 60)
cur.execute("SELECT * FROM summaries WHERE id = 188")
row = cur.fetchone()
if row:
    cols = row.keys()
    for c in cols:
        val = row[c]
        if c == 'content':
            val = str(val)[:200] + '...' if val and len(str(val)) > 200 else val
        print(f"  {c} = {val}")
else:
    print("  ID=188 不存在")

print()

# 检查是否有多条相同内容的记录
print("检查是否有重复内容:")
cur.execute("SELECT id, tag, is_active, created_at FROM summaries WHERE content LIKE '%Admin面板后反馈%'")
rows = cur.fetchall()
print(f"  包含'Admin面板后反馈'的记录共 {len(rows)} 条:")
for r in rows:
    print(f"    ID={r['id']}, tag={r['tag']}, is_active={r['is_active']}, created_at={r['created_at']}")

print()

# 检查当前所有 is_active=1 的 round 记录
print("当前所有 is_active=1 的 round 记录:")
cur.execute("SELECT id, created_at, SUBSTR(content, 1, 80) as preview FROM summaries WHERE tag = 'round' AND is_active = 1 ORDER BY created_at DESC")
rows = cur.fetchall()
print(f"  共 {len(rows)} 条:")
for r in rows:
    print(f"    ID={r['id']}, created_at={r['created_at']}, preview={r['preview']}")

print()

# 检查当前所有 is_active=1 的 round_rollup 记录
print("当前所有 is_active=1 的 round_rollup 记录:")
cur.execute("SELECT id, created_at, SUBSTR(content, 1, 80) as preview FROM summaries WHERE tag = 'round_rollup' AND is_active = 1 ORDER BY created_at DESC")
rows = cur.fetchall()
print(f"  共 {len(rows)} 条:")
for r in rows:
    print(f"    ID={r['id']}, created_at={r['created_at']}, preview={r['preview']}")

print()

# Bug 1: 检查 injection_rules 表结构
print("=" * 60)
print("Bug 1: injection_rules 表结构")
print("=" * 60)
cur.execute("PRAGMA table_info(injection_rules)")
for col in cur.fetchall():
    print(f"  {col['name']} ({col['type']})")

print()
print("当前所有规则:")
cur.execute("SELECT id, name, LENGTH(content) as content_len, is_enabled, position, role FROM injection_rules")
rows = cur.fetchall()
for r in rows:
    print(f"  ID={r['id']}, name={r['name']}, len={r['content_len']}, enabled={r['is_enabled']}, pos={r['position']}, role={r['role']}")

conn.close()

