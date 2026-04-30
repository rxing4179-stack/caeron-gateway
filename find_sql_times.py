import os

def check_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    found = False
    for line_num, line in enumerate(content.split('\n'), 1):
        if ("datetime('now')" in line or "date('now')" in line) and "+8 hours" not in line:
            print(f"{filepath}:{line_num}: {line.strip()}")
            found = True
    return found

def main():
    root_dir = os.path.expanduser('~/caeron-gateway')
    for root, dirs, files in os.walk(root_dir):
        if 'venv' in root or '__pycache__' in root:
            continue
        for file in files:
            if file.endswith('.py') or file.endswith('.html'):
                check_file(os.path.join(root, file))

if __name__ == '__main__':
    main()
