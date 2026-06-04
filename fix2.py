with open('/home/ubuntu/caeron-gateway/main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
    
lines_to_fix = [252, 448, 513, 888] # 0-indexed for 253, 449, 514, 889
for i in lines_to_fix:
    if 'else:' in lines[i]:
        lines[i] = '        else:\n'

with open('/home/ubuntu/caeron-gateway/main.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
print("Fixed remaining else lines")
