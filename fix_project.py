import os
import re
import sqlite3
from datetime import datetime, timedelta

def fix_file_content(path, replacements, imports_to_add=None):
    if not os.path.exists(path):
        return
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original = content
    for old, new in replacements:
        content = content.replace(old, new)
    
    # Regex replacements
    content = re.sub(r'datetime\.utcnow\(\)\s*\+\s*timedelta\(hours=8\)', 'now_cst()', content)
    content = re.sub(r"datetime\('now'(?!\s*,\s*'\+8 hours'\))", "datetime('now', '+8 hours')", content)
    content = re.sub(r"date\('now'(?!\s*,\s*'\+8 hours'\))", "date('now', '+8 hours')", content)

    if imports_to_add and content != original:
        for imp in imports_to_add:
            if imp not in content:
                if 'import datetime' in content:
                    content = content.replace('import datetime', 'import datetime\n' + imp)
                elif 'from datetime import' in content:
                    content = content.replace('from datetime import', imp + '\nfrom datetime import')
                else:
                    content = imp + '\n' + content
    
    if content != original:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Fixed {path}")

def migrate_db():
    db_path = os.path.expanduser('~/caeron-gateway/gateway.db')
    if not os.path.exists(db_path):
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cursor.fetchall()]
    time_cols = ['created_at', 'updated_at', 'started_at', 'last_message_at', 'last_used_at', 'unhealthy_since', 'period_start', 'period_end']
    
    for table in tables:
        cursor.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cursor.fetchall()]
        for col in cols:
            if col in time_cols:
                # Update records that look like UTC (e.g. today's records that are 8 hours behind)
                # To be safe, we only update if they are in the past.
                cursor.execute(f"UPDATE {table} SET {col} = datetime({col}, '+8 hours') WHERE {col} IS NOT NULL AND {col} < datetime('now', '-7 hours')")
                if cursor.rowcount > 0:
                    print(f"Migrated {cursor.rowcount} rows in {table}.{col}")
    conn.commit()
    conn.close()

def main():
    root = os.path.expanduser('~/caeron-gateway')
    
    # 1. main.py
    main_py = os.path.join(root, 'main.py')
    main_replacements = [
        ("from datetime import datetime\nfrom utils import now_cst, today_cst_str, timedelta", ""),
        ("china_now = datetime.utcnow() + timedelta(hours=8)", "china_now = now_cst()"),
        ("_now = _dt_s.utcnow()", "_now = now_cst()"),
        ("_now.replace(hour=15, minute=59", "_now.replace(hour=23, minute=59"),
        ("_target += _td_s(days=1)", "_target += timedelta(days=1)"),
        ("_beijing = _target + _td_s(hours=8)", "_beijing = _target"),
        ("next_cron = _beijing.strftime", "next_cron = _target.strftime"),
        ("now = _dt.utcnow()", "now = now_cst()"),
        ("target = now.replace(hour=15, minute=59", "target = now.replace(hour=23, minute=59"),
        ("target += _td(days=1)", "target += timedelta(days=1)"),
        ("trigger_time = _dt.utcnow()", "trigger_time = now_cst()"),
        ("beijing_date = (trigger_time + _td(hours=8)).date()", "beijing_date = trigger_time.date()"),
        ("from utils import now_cst, today_cst_str as _dt, timedelta as _td", "from utils import now_cst, today_cst_str"),
        ("from utils import now_cst, today_cst_str as _dt_s, timedelta as _td_s", "from utils import now_cst, today_cst_str")
    ]
    fix_file_content(main_py, main_replacements, ["from utils import now_cst, today_cst_str", "from datetime import timedelta"])

    # 2. admin.html
    admin_html = os.path.join(root, 'static/admin.html')
    admin_replacements = [
        ("const d = new Date(timeStr.replace(/-/g, '/'));", 
         "let cleanStr = timeStr.replace(/-/g, '/'); if (!cleanStr.includes('Z') && !cleanStr.includes('+')) { cleanStr += ' +0800'; } const d = new Date(cleanStr);")
    ]
    fix_file_content(admin_html, admin_replacements)

    # 3. summariz^¬.py & injection.py (mostly handled by generic now_cst regex but let's be sure)
    for f in ['summarizer.py', 'injection.py', 'message_store.py']:
        fix_file_content(os.path.join(root, f), [], ["from utils import now_cst, today_cst_str", "from datetime import timedelta"])

    # 4. Database migration
    migrate_db()

if __name__ == '__main__':
    main()
