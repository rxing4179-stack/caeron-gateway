with open('/home/ubuntu/caeron-gateway/main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if line.startswith('                else:') and 'token = auth[7:]' in lines[i-1]:
        lines[i] = '            else:\n'
with open('/home/ubuntu/caeron-gateway/main.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
print("Fixed main.py")
