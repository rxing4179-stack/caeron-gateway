import os
import re
import sqlite3

# ============================================================
# Bug 1: 修复 main.py 中 PUT /admin/api/rules 的 SQL 语法错误
# ============================================================
main_path = os.path.expanduser('~/caeron-gateway/main.py')
with open(main_path, 'r', encoding='utf-8') as f:
    main_content = f.read()

changes = 0

# Fix 1a: SQL语法错误 - 多余的右括号
old_sql = "updated_at = datetime('now', '+8 hours')) WHERE id = ?"
new_sql = "updated_at = datetime('now', '+8 hours') WHERE id = ?"
if old_sql in main_content:
    main_content = main_content.replace(old_sql, new_sql)
    changes += 1
    print("Bug 1a: 修复 SQL 多余右括号 ✓")
else:
    print("Bug 1a: 未找到多余右括号，可能已修复")

# Fix 1b: PUT handler 添加异常捕获，返回具体错误信息
old_put_handler = '''@app.put("/admin/api/rules/{rule_id}")
async def admin_update_rule(rule_id: int, request: Request):
    data = await request.json()
    db = await get_db()
    try:
        fields, values = [], []
        for k in ['name', 'content', 'position', 'role', 'priority', 'depth', 'match_condition', 'is_enabled']:
            if k in data:
                fields.append(f"{k} = ?")
                values.append(data[k])
        if fields:
            values.append(rule_id)
            await db.execute(f"UPDATE injection_rules SET {', '.join(fields)}, updated_at = datetime('now', '+8 hours') WHERE id = ?", values)
            await db.commit()
        return {"message": "规则更新成功"}
    finally:
        await db.close()'''

new_put_handler = '''@app.put("/admin/api/rules/{rule_id}")
async def admin_update_rule(rule_id: int, request: Request):
    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"JSON解析失败: {str(e)}")
    db = await get_db()
    try:
        fields, values = [], []
        for k in ['name', 'content', 'position', 'role', 'priority', 'depth', 'match_condition', 'is_enabled']:
            if k in data:
                fields.append(f"{k} = ?")
                values.append(data[k])
        if fields:
            values.append(rule_id)
            await db.execute(f"UPDATE injection_rules SET {', '.join(fields)}, updated_at = datetime('now', '+8 hours') WHERE id = ?", values)
            await db.commit()
        return {"message": "规则更新成功"}
    except Exception as e:
        logger.error(f"规则更新失败 (rule_id={rule_id}): {e}")
        raise HTTPException(status_code=500, detail=f"规则更新失败: {str(e)}")
    finally:
        await db.close()'''

if old_put_handler in main_content:
    main_content = main_content.replace(old_put_handler, new_put_handler)
    changes += 1
    print("Bug 1b: 添加异常捕获和详细错误返回 ✓")
else:
    print("Bug 1b: PUT handler 结构不匹配，尝试简单修复...")
    # 至少确保SQL已修复

with open(main_path, 'w', encoding='utf-8') as f:
    f.write(main_content)

print(f"main.py 修改了 {changes} 处")

# ============================================================
# Fix 1c: 修复前端错误处理 - 显示具体错误信息
# ============================================================
admin_path = os.path.expanduser('~/caeron-gateway/static/admin.html')
with open(admin_path, 'r', encoding='utf-8') as f:
    admin_content = f.read()

fe_changes = 0

# 修复规则保存的错误处理：从只显示'保存失败'到显示后端返回的detail
old_rule_error = "if (!res.ok) throw new Error('保存失败');"
new_rule_error = """if (!res.ok) {
                            const errData = await res.json().catch(() => ({}));
                            throw new Error(errData.detail || `保存失败 (HTTP ${res.status})`);
                        }"""
if old_rule_error in admin_content:
    admin_content = admin_content.replace(old_rule_error, new_rule_error, 1)
    fe_changes += 1
    print("Bug 1c: 前端规则保存错误信息增强 ✓")
else:
    print("Bug 1c: 未找到前端规则保存错误处理代码")

with open(admin_path, 'w', encoding='utf-8') as f:
    f.write(admin_content)

print(f"admin.html 修改了 {fe_changes} 处")

# ============================================================
# Bug 2: 归档幽灵轮总 ID=188
# ============================================================
db_path = os.path.expanduser('~/caeron-gateway/gateway.db')
conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute("SELECT id, is_active FROM summaries WHERE id = 188")
row = cur.fetchone()
if row:
    if row[1] == 1:
        cur.execute("UPDATE summaries SET is_active = 0 WHERE id = 188")
        conn.commit()
        print(f"\nBug 2: 幽灵轮总 ID=188 已归档 (is_active: 1 → 0) ✓")
    else:
        print(f"\nBug 2: ID=188 已经是 is_active=0，无需修改")
else:
    print(f"\nBug 2: ID=188 不存在")

# 验证
cur.execute("SELECT COUNT(*) FROM summaries WHERE tag = 'round' AND is_active = 1")
active_rounds = cur.fetchone()[0]
print(f"当前活跃 round 记录数: {active_rounds}")

conn.close()

print("\n所有修复已完成。")

