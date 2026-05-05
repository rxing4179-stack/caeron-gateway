import os
import re

path = os.path.expanduser('~/caeron-gateway/summarizer.py')
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

changes = 0

# Fix 1: 在 previous_summary 传入 LLM 前，剥离旧的 [任务] 标记
# 找到这段代码：
#   if previous_summary:
#       user_parts.append(f"上轮总结：{previous_summary}\n")
old_prev_summary = '''        if previous_summary:
            user_parts.append(f"上轮总结：{previous_summary}\\n")'''
new_prev_summary = '''        if previous_summary:
            # 剥离上轮总结中的 [任务] 标记，防止过期任务被无限 carry forward
            clean_prev = re.sub(r'\\n?\\[任务\\].*$', '', previous_summary, flags=re.MULTILINE).strip()
            user_parts.append(f"上轮总结：{clean_prev}\\n")'''

if old_prev_summary in content:
    content = content.replace(old_prev_summary, new_prev_summary)
    changes += 1
    print(f"Fix 1: 剥离 previous_summary 中的旧 [任务] 标记 ✓")
else:
    print(f"Fix 1: 未找到目标代码块，跳过")

# Fix 2: 修改 prompt 中关于任务状态的指令，改为"只记录本轮新产生的任务，不继承上轮"
old_task_rule = "- 如果多条轮总有正在进行的任务，保留最新的任务状态"
new_task_rule = "- [任务状态] 只记录本轮对话中明确提到的、正在进行的新任务。不要从上轮总结中继承旧任务。如果本轮无新任务，写\"无\""

if old_task_rule in content:
    content = content.replace(old_task_rule, new_task_rule)
    changes += 1
    print(f"Fix 2: 修改 prompt 中的任务状态规则 ✓")
else:
    print(f"Fix 2: 未找到目标 prompt 文本，跳过")

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"\n共修改 {changes} 处。")
if changes == 2:
    print("SUCCESS: summarizer.py 已完整修复")
else:
    print("WARNING: 部分修复未生效，请人工检查")

