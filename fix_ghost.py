import sqlite3
import os

db_path = os.path.expanduser('~/caeron-gateway/gateway.db')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Step 1: 显示当前幽灵记录
print("=" * 60)
print("修复前 - 幽灵轮总记录 ID=187:")
cur.execute("SELECT id, tag, is_active, created_at, content FROM summaries WHERE id = 187")
row = cur.fetchone()
if row:
    print(f"  ID={row['id']}, tag={row['tag']}, is_active={row['is_active']}")
    print(f"  created_at={row['created_at']}")
    print(f"  content={row['content']}")
print()

# Step 2: 将该记录的 is_active 设为 0（归档，不删除）
print("执行修复：将 ID=187 的 is_active 设为 0...")
cur.execute("UPDATE summaries SET is_active = 0 WHERE id = 187")
conn.commit()
print(f"  受影响行数: {cur.rowcount}")
print()

# Step 3: 验证修复后的状态
print("修复后 - 所有活跃的 round 和 round_rollup 记录:")
cur.execute("SELECT id, tag, is_active, created_at, SUBSTR(content, 1, 100) as preview FROM summaries WHERE tag IN ('round', 'round_rollup') AND is_active = 1 ORDER BY created_at DESC")
rows = cur.fetchall()
print(f"  共 {len(rows)} 条:")
for r in rows:
    print(f"  ID={r['id']}, tag={r['tag']}, created_at={r['created_at']}")
    print(f"    {r['preview'][:80]}...")
print()

# Step 4: 同时检查是否有其他包含过期任务标记的活跃记录
print("检查其他包含 [任务] 标记的活跃记录:")
cur.execute("SELECT id, tag, is_active, created_at, SUBSTR(content, 1, 150) as preview FROM summaries WHERE content LIKE '%[任务]%' AND is_active = 1")
rows = cur.fetchall()
print(f"  共 {len(rows)} 条:")
for r in rows:
    print(f"  ID={r['id']}, tag={r['tag']}, created_at={r['created_at']}")
    print(f"    {r['preview'][:120]}...")
print()

conn.close()
print("完成！幽灵轮总已归档。")

