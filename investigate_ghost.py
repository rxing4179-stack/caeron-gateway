import sqlite3
import os

db_path = os.path.expanduser('~/caeron-gateway/gateway.db')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=" * 80)
print("1. 搜索所有包含 'Admin面板优化' 的 summaries 记录")
print("=" * 80)
cur.execute("SELECT id, tag, is_active, created_at, LENGTH(content) as content_len, SUBSTR(content, 1, 200) as preview FROM summaries WHERE content LIKE '%Admin面板优化%'")
rows = cur.fetchall()
print(f"找到 {len(rows)} 条记录:")
for r in rows:
    print(f"  ID={r['id']}, tag={r['tag']}, is_active={r['is_active']}, created_at={r['created_at']}, len={r['content_len']}")
    print(f"  preview: {r['preview'][:150]}...")
    print()

print("=" * 80)
print("2. 搜索所有包含 'token暴涨' 的 summaries 记录")
print("=" * 80)
cur.execute("SELECT id, tag, is_active, created_at, LENGTH(content) as content_len, SUBSTR(content, 1, 200) as preview FROM summaries WHERE content LIKE '%token暴涨%'")
rows = cur.fetchall()
print(f"找到 {len(rows)} 条记录:")
for r in rows:
    print(f"  ID={r['id']}, tag={r['tag']}, is_active={r['is_active']}, created_at={r['created_at']}, len={r['content_len']}")
    print(f"  preview: {r['preview'][:150]}...")
    print()

print("=" * 80)
print("3. 搜索所有包含 '任务' 且 tag='round' 且 is_active=1 的记录")
print("=" * 80)
cur.execute("SELECT id, tag, is_active, created_at, LENGTH(content) as content_len, SUBSTR(content, 1, 200) as preview FROM summaries WHERE content LIKE '%[任务]%' AND is_active = 1")
rows = cur.fetchall()
print(f"找到 {len(rows)} 条记录:")
for r in rows:
    print(f"  ID={r['id']}, tag={r['tag']}, is_active={r['is_active']}, created_at={r['created_at']}, len={r['content_len']}")
    print(f"  preview: {r['preview'][:150]}...")
    print()

print("=" * 80)
print("4. 所有 is_active=1 的 round 和 round_rollup 摘要统计")
print("=" * 80)
cur.execute("SELECT tag, COUNT(*) as cnt, MIN(created_at) as earliest, MAX(created_at) as latest FROM summaries WHERE tag IN ('round', 'round_rollup') AND is_active = 1 GROUP BY tag")
rows = cur.fetchall()
for r in rows:
    print(f"  tag={r['tag']}, count={r['cnt']}, earliest={r['earliest']}, latest={r['latest']}")

print()
print("=" * 80)
print("5. 所有 is_active=1 的 round 摘要详细列表")
print("=" * 80)
cur.execute("SELECT id, created_at, LENGTH(content) as content_len, SUBSTR(content, 1, 150) as preview FROM summaries WHERE tag = 'round' AND is_active = 1 ORDER BY created_at DESC")
rows = cur.fetchall()
print(f"共 {len(rows)} 条活跃 round 摘要:")
for r in rows:
    print(f"  ID={r['id']}, created_at={r['created_at']}, len={r['content_len']}")
    print(f"    {r['preview'][:120]}...")
    print()

print("=" * 80)
print("6. 所有 is_active=1 的 round_rollup 摘要详细列表")
print("=" * 80)
cur.execute("SELECT id, created_at, LENGTH(content) as content_len, SUBSTR(content, 1, 150) as preview FROM summaries WHERE tag = 'round_rollup' AND is_active = 1 ORDER BY created_at DESC")
rows = cur.fetchall()
print(f"共 {len(rows)} 条活跃 round_rollup 摘要:")
for r in rows:
    print(f"  ID={r['id']}, created_at={r['created_at']}, len={r['content_len']}")
    print(f"    {r['preview'][:120]}...")
    print()

print("=" * 80)
print("7. 检查 summaries 表的 schema")
print("=" * 80)
cur.execute("PRAGMA table_info(summaries)")
for col in cur.fetchall():
    print(f"  {col['name']} ({col['type']}), nullable={not col['notnull']}, default={col['dflt_value']}")

conn.close()

