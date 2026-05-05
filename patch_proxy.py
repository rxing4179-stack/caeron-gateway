import os
import re

path = os.path.expanduser('~/caeron-gateway/proxy.py')
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Add debug logs to proxy_chat_completion
target = "logger.info(f\"转发请求到 {provider['name']}: {upstream_url} (stream={is_stream})\")"
replacement = f"""{target}

    # [DEBUG-TOKEN] 转发调试日志
    body_str = json.dumps(request_body)
    logger.info(f"[DEBUG-TOKEN] 转发请求体总字符数: {{len(body_str)}}")
    logger.info(f"[DEBUG-TOKEN] messages条数: {{len(request_body.get('messages', []))}}")
    for i, msg in enumerate(request_body.get('messages', [])):
        content_preview = str(msg.get('content', ''))[:80]
        logger.info(f"[DEBUG-TOKEN] msg[{{i}}] role={{msg['role']}} content={{content_preview}}")"""

content = content.replace(target, replacement)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Patched proxy.py successfully")

