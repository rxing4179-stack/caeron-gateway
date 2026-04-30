import os
import re

def fix_python_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    if 'utils.py' in filepath or 'fix_times.py' in filepath:
        return

    original = content

    # 1. Patterns to replace with now_cst() or today_cst_str()
    
    # Complex ones first
    content = content.replace("(datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')", "now_cst().strftime('%Y-%m-%d %H:%M:%S')")
    content = content.replace("(datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d')", "today_cst_str()")
    content = content.replace("datetime.now().strftime('%Y-%m-%d')", "today_cst_str()")
    
    # timedelta addition
    content = re.sub(r'datetime\.utcnow\(\)\s*\+\s*timedelta\(hours=8\)', 'now_cst()', content)
    
    # Basic calls
    # Note: we use \b to ensure we match the full function call
    content = re.sub(r'\bdatetime\.utcnow\(\)', 'now_cst()', content)
    content = re.sub(r'\bdatetime\.now\(\)', 'now_cst()', content)
    content = re.sub(r'\bdatetime\.today\(\)', 'now_cst()', content)

    # 2. SQL patterns in strings
    # We look for datetime('now') and date('now') inside strings
    # We avoid replacing if '+8 hours' is already present
    content = re.sub(r"datetime\('now'(?!\s*,\s*'\+8 hours'\))", "datetime('now', '+8 hours')", content)
    content = re.sub(r"date\('now'(?!\s*,\s*'\+8 hours'\))", "date('now', '+8 hours')", content)

    # 3. Add imports if we made changes
    if content != original:
        if 'from utils import now_cst' not in content:
            # Insert after other imports
            if 'import datetime' in content:
                content = content.replace('import datetime', 'import datetime\nfrom utils import now_cst, today_cst_str')
            elif 'from datetime import' in content:
                # Find the line with from datetime import and insert after it
                lines = content.split('\n')
                for i, line in enumerate(lines):
                    if 'from datetime import' in line:
                        lines.insert(i + 1, 'from utils import now_cst, today_cst_str')
                        break
                content = '\n'.join(lines)
            else:
                # Just prepend
                content = 'from utils import now_cst, today_cst_str\n' + content
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Fixed {filepath}")

def main():
    root_dir = os.path.expanduser('~/caeron-gateway')
    for root, dirs, files in os.walk(root_dir):
        # Skip venv and __pycache__
        if 'venv' in root or '__pycache__' in root:
            continue
        for file in files:
            if file.endswith('.py'):
                fix_python_file(os.path.join(root, file))

if __name__ == '__main__':
    main()
