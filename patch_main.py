import re

with open('/home/ubuntu/caeron-gateway/main.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Add config check to _handle_chat_completions
insert_point = "    request.state.log_info = {"
config_check = """
    # 动态获取各个功能开关
    from config import get_config as _get_config
    gateway_master = await _get_config('gateway_master_switch', '1') == '1'
    feature_summary = await _get_config('feature_summary', '1') == '1'
    feature_memory = await _get_config('feature_memory', '1') == '1'
    feature_injection = await _get_config('feature_injection', '1') == '1'
    
    # 传递给下文
    request.state.gateway_master = gateway_master
    request.state.feature_summary = feature_summary
    request.state.feature_memory = feature_memory
    request.state.feature_injection = feature_injection

"""
if "request.state.gateway_master = gateway_master" not in code:
    code = code.replace(insert_point, config_check + insert_point)

# Intercept Summary
code = code.replace('    if is_summary_request:', '    if is_summary_request and gateway_master and feature_summary:')

# Get unified history
code = code.replace("    if not tech_mode and request.headers.get('x-skip-rules', '').lower() != 'true':", 
"    if gateway_master and feature_memory and not tech_mode and request.headers.get('x-skip-rules', '').lower() != 'true':")

# Store incoming messages
code = code.replace("    try:\n        await ensure_conversation(conversation_id, model=model)",
"    try:\n        if gateway_master:\n            await ensure_conversation(conversation_id, model=model)")

code = code.replace('        if _is_qq:', '        if gateway_master and _is_qq:')
code = code.replace('        else:\n            # 技术模式和日常模式都存 messages', '        elif gateway_master:\n            # 技术模式和日常模式都存 messages')
code = code.replace('            if was_rerolled:', '            if gateway_master and was_rerolled and feature_memory:')

# Background Summary
code = code.replace('    if stored_count > 0 and (not tech_mode or _is_qq):  # QQ不受技术模式限制',
'    if gateway_master and feature_summary and stored_count > 0 and (not tech_mode or _is_qq):  # QQ不受技术模式限制')

# Injection
code = code.replace('    # === Step 3: 执行提示词注入 ===',
'    # === Step 3: 执行提示词注入 ===\n    if gateway_master and feature_injection:')
code = code.replace("    if request.headers.get('x-skip-rules', '').lower() == 'true':",
"        if request.headers.get('x-skip-rules', '').lower() == 'true':")
code = code.replace('        logger.info("[TECH_MODE] 处于技术模式或QQ通道")',
'            logger.info("[TECH_MODE] 处于技术模式或QQ通道")')
code = code.replace("        body['messages'] = await injection_engine.inject_memory_only",
"            body['messages'] = await injection_engine.inject_memory_only")
code = code.replace("        body['messages'] = await injection_engine.inject_tech_mode",
"            body['messages'] = await injection_engine.inject_tech_mode")
code = code.replace('    else:', '        else:')
code = code.replace("        body['messages'] = await injection_engine.inject",
"            body['messages'] = await injection_engine.inject")

with open('/home/ubuntu/caeron-gateway/main.py', 'w', encoding='utf-8') as f:
    f.write(code)
    
print("main.py patched successfully")
