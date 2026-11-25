import base64
import uuid
import time
import os

GENERATED_IMAGES_DIR = "/home/ubuntu/caeron-gateway/static/generated_images"

IMAGE_MODEL_KEYWORDS = [
    'image', 'dall-e', 'dalle', 'sunburst', 'flare', 'flux',
    'midjourney', 'stable-diffusion', 'sdxl', 'sd-', 'imagen', 'cogview'
]

def is_image_model(model: str) -> bool:
    if not model or not isinstance(model, str):
        return False
    m = model.lower()
    return any(k in m for k in IMAGE_MODEL_KEYWORDS)

def save_base64_data_to_file(b64_str: str, ext: str = "png") -> str:
    """将 Base64 图片解码并保存为 static/generated_images/{filename}"""
    try:
        os.makedirs(GENERATED_IMAGES_DIR, exist_ok=True)
        # 去掉 data:image/xxx;base64, 前缀（如果有）
        if ',' in b64_str and ('data:image' in b64_str[:60] or 'base64' in b64_str[:60]):
            header, b64_str = b64_str.split(',', 1)
            if 'jpeg' in header or 'jpg' in header:
                ext = "jpg"
            elif 'webp' in header:
                ext = "webp"
            elif 'gif' in header:
                ext = "gif"
            elif 'png' in header:
                ext = "png"

        clean_b64 = re.sub(r'\s+', '', b64_str)
        img_bytes = base64.b64decode(clean_b64)
        if len(img_bytes) < 16:
            return None

        # 嗅探文件魔数纠正扩展名
        if img_bytes.startswith(b'\xff\xd8\xff'):
            ext = 'jpg'
        elif img_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
            ext = 'png'
        elif img_bytes.startswith(b'GIF87a') or img_bytes.startswith(b'GIF89a'):
            ext = 'gif'
        elif img_bytes.startswith(b'RIFF') and img_bytes[8:12] == b'WEBP':
            ext = 'webp'

        filename = f"img_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
        filepath = os.path.join(GENERATED_IMAGES_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(img_bytes)
        logger.info(f"[IMAGE_INTERCEPT] 成功转存图片: {filename} ({len(img_bytes)} 字节)")
        return filename
    except Exception as e:
        logger.error(f"[IMAGE_INTERCEPT] 保存图片失败: {e}")
        return None

def transform_image_content(content, client_host: str = "1.14.59.116:8080"):
    """
    检查内容中是否有 Base64 图片数据，转存为静态文件并替换为标准 HTTP Markdown 链接。
    注意：在图片下方附带原图查看/保存链接，这同时能打破 Operit 气泡对纯单张图片的"表情包(80dp禁止点击)"拦截模式，
    恢复完整 MarkdownImageRenderer 的全屏放大弹窗（带右上角保存相册按钮）与超链接预览！
    """
    base_url = f"http://{client_host}/static/generated_images"

    if isinstance(content, str):
        # 1. 匹配完整 Markdown 图片 data-uri: ![alt](data:image/xxx;base64,...)
        pattern_md = r'!\[(.*?)\]\(data:image/([a-zA-Z0-9.+_-]+);base64,([A-Za-z0-9+/=\s]+)\)'
        if 'data:image' in content and ';base64,' in content:
            def replace_md(m):
                alt = m.group(1) or "生成的图片"
                ext = m.group(2) or "png"
                b64 = m.group(3)
                fname = save_base64_data_to_file(b64, ext)
                if fname:
                    img_url = f"{base_url}/{fname}"
                    return f"![{alt}]({img_url})\n\n[🔍 点击查看原图 / 保存到手机]({img_url})"
                return m.group(0)
            content = re.sub(pattern_md, replace_md, content)

            # 2. 匹配裸 data:image/...;base64,...
            pattern_bare = r'data:image/([a-zA-Z0-9.+_-]+);base64,([A-Za-z0-9+/=\s]{100,})'
            def replace_bare(m):
                ext = m.group(1) or "png"
                b64 = m.group(2)
                fname = save_base64_data_to_file(b64, ext)
                if fname:
                    img_url = f"{base_url}/{fname}"
                    return f"\n\n![生成的图片]({img_url})\n\n[🔍 点击查看原图 / 保存到手机]({img_url})\n\n"
                return m.group(0)
            content = re.sub(pattern_bare, replace_bare, content)

        # 3. 匹配长裸 Base64 字符串（无 data:image 前缀，直接以常见图片魔数开头）
        stripped = content.strip()
        if len(stripped) > 500 and not stripped.startswith('<') and not stripped.startswith('{'):
            first_chars = stripped[:15]
            detected_ext = None
            if first_chars.startswith('/9j/'):
                detected_ext = 'jpg'
            elif first_chars.startswith('iVBORw'):
                detected_ext = 'png'
            elif first_chars.startswith('R0lGOD'):
                detected_ext = 'gif'
            elif first_chars.startswith('UklGR'):
                detected_ext = 'webp'

            if detected_ext:
                fname = save_base64_data_to_file(stripped, detected_ext)
                if fname:
                    img_url = f"{base_url}/{fname}"
                    content = f"\n\n![生成的图片]({img_url})\n\n[🔍 点击查看原图 / 保存到手机]({img_url})\n\n"

        # 4. 如果内容仅仅是单个已生成的 HTTP 静态图片链接且无附带文字，也追加点击保存链接避免落入 Operit 表情包拦截
        single_img_pattern = r'^\s*!\[([^\]]*)\]\((http://[^)]+/static/generated_images/[^)]+)\)\s*$'
        single_match = re.match(single_img_pattern, content)
        if single_match:
            alt = single_match.group(1) or "生成的图片"
            img_url = single_match.group(2)
            content = f"![{alt}]({img_url})\n\n[🔍 点击查看原图 / 保存到手机]({img_url})"

        return content

    elif isinstance(content, list):
        new_list = []
        for item in content:
            if isinstance(item, dict):
                item_copy = item.copy()
                t = item_copy.get('type')
                if t == 'text':
                    item_copy['text'] = transform_image_content(item_copy.get('text', ''), client_host)
                elif t == 'image_url':
                    img_obj = item_copy.get('image_url', {})
                    if isinstance(img_obj, dict):
                        url = img_obj.get('url', '')
                        if 'data:image' in url and ';base64,' in url:
                            try:
                                parts = url.split(';base64,', 1)
                                mime_ext = parts[0].replace('data:image/', '')
                                fname = save_base64_data_to_file(parts[1], mime_ext)
                                if fname:
                                    img_obj['url'] = f"{base_url}/{fname}"
                                    item_copy['image_url'] = img_obj
                            except Exception:
                                pass
                    elif isinstance(img_obj, str) and 'data:image' in img_obj:
                        try:
                            parts = img_obj.split(';base64,', 1)
                            mime_ext = parts[0].replace('data:image/', '')
                            fname = save_base64_data_to_file(parts[1], mime_ext)
                            if fname:
                                item_copy['image_url'] = f"{base_url}/{fname}"
                        except Exception:
                            pass
                elif 'b64_json' in item_copy:
                    fname = save_base64_data_to_file(item_copy['b64_json'], 'png')
                    if fname:
                        item_copy = {
                            "type": "image_url",
                            "image_url": {"url": f"{base_url}/{fname}"}
                        }
                new_list.append(item_copy)
            else:
                new_list.append(item)
        return new_list

    return content

"""
Caeron Gateway - 请求转发模块
负责将请求转发到上游供应商，支持流式（SSE）和非流式两种模式
"""

import json
import logging
import httpx
import re
from urllib.parse import quote
import asyncio
from fastapi.responses import StreamingResponse, JSONResponse
from message_store import store_assistant_response
from think_parser import ThinkTagParser, ReasoningStreamParser
from exec_router import ExecTagStreamFilter, extract_exec_tags, strip_exec_tags, process_and_route

logger = logging.getLogger(__name__)

def clean_system_prompt(content: str) -> str:
    """清理上游平台注入的系统预设，采用白名单策略保护用户角色卡，剥离其余注入"""
    if not isinstance(content, str):
        return content

    # 1. 白名单保护提取
    user_prompt = ""
    if "<!-- user_prompt_start -->" in content:
        parts = content.split("<!-- user_prompt_start -->", 1)
        before_user = parts[0]
        after_start = parts[1]
        
        if "<!-- user_prompt_end -->" in after_start:
            subparts = after_start.split("<!-- user_prompt_end -->", 1)
            user_prompt = "<!-- user_prompt_start -->" + subparts[0] + "<!-- user_prompt_end -->"
            rest_content = before_user + "\n" + subparts[1]
        else:
            user_prompt = "<!-- user_prompt_start -->" + after_start
            rest_content = before_user
    else:
        rest_content = content
        
    # 2. 对其余部分（非白名单部分）进行无情清洗
    strict_strip_tags = [
        'identity', 'capabilities', 'response_style', 'rules', 
        'safety_guardrails', 'content_safety', 'coding_questions', 
        'git_safety', 'tone_and_style', 'instructions', 'system_prompt'
    ]
    for tag in strict_strip_tags:
        rest_content = re.sub(rf'<{tag}>.*?</{tag}>\n*', '', rest_content, flags=re.DOTALL | re.IGNORECASE)
        
    rest_content = re.sub(r'(?i)(You are Kiro.*?)(\n\n|$)', '', rest_content, flags=re.DOTALL)
    rest_content = re.sub(r'(?i)(You are a helpful.*?)(\n\n|$)', '', rest_content, flags=re.DOTALL)

    # 3. 重新拼接：如果有白名单用户prompt，就拼在一起；否则就返回清洗后的全量文本
    if user_prompt:
        final_content = rest_content.strip() + "\n\n" + user_prompt.strip()
    else:
        final_content = rest_content.strip()
        
    return final_content.strip()


def build_upstream_url(api_base_url: str) -> str:
    """
    构造上游 chat/completions 请求 URL
    兼容多种输入格式：
    - https://api.example.com → https://api.example.com/v1/chat/completions
    - https://api.example.com/v1 → https://api.example.com/v1/chat/completions
    - https://api.example.com/v1/chat/completions → 直接使用
    """
    url = api_base_url.rstrip('/')

    if url.endswith('/chat/completions'):
        return url
    elif url.endswith('/v1'):
        return f"{url}/chat/completions"
    else:
        return f"{url}/v1/chat/completions"


def build_models_url(api_base_url: str) -> str:
    """构造上游 /v1/models 请求 URL"""
    url = api_base_url.rstrip('/')

    if url.endswith('/v1'):
        return f"{url}/models"
    elif url.endswith('/chat/completions'):
        return url.rsplit('/chat/completions', 1)[0] + '/models'
    else:
        return f"{url}/v1/models"



def sanitize_tool_chain(messages: list[dict]) -> list[dict]:
    """
    终极防御性工具链清洗器（防止任何上游 400 invalid_request_error）：
    确保送往上游 Claude / OpenAI 的消息序列 100% 符合规范：
    1. 检查每个 role='tool' 消息或包含 'tool_result' 的 content block：
       - 前一条消息必须是 role='assistant' 且包含匹配的 tool_use_id / tool_calls id；
       - 若不匹配或前置 assistant 丢失，将该 tool 消息或 tool_result 降级为常规文本消息（例如: [历史工具执行结果]: ...），并改为 user 角色；
    2. 确保在系统消息之后，对话的起始非系统消息必须是 'user'；
    3. 合并相邻的同角色消息（若降级导致连续两条 user 消息）。
    """
    if not messages or not isinstance(messages, list):
        return messages

    cleaned = []
    current_assistant_tool_ids = set()

    for msg in messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue
            
        role = msg.get('role', '')
        content = msg.get('content', '')

        if role == 'assistant':
            current_assistant_tool_ids = set()
            tcs = msg.get('tool_calls') or []
            for tc in tcs:
                tid = tc.get('id')
                if tid:
                    current_assistant_tool_ids.add(str(tid))
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get('type') == 'tool_use':
                        tid = b.get('id')
                        if tid:
                            current_assistant_tool_ids.add(str(tid))
            cleaned.append(msg)

        elif role == 'tool':
            tid = msg.get('tool_call_id')
            prev_is_matching = (
                cleaned and 
                (cleaned[-1].get('role') in ('assistant', 'tool')) and 
                (str(tid) in current_assistant_tool_ids)
            )
            if prev_is_matching:
                cleaned.append(msg)
            else:
                logger.warning(f"[TOOL_CHAIN_GUARD] 发现孤立 tool 消息 (id={tid})，降级为 user 文本")
                text_content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                cleaned.append({
                    'role': 'user',
                    'content': f"[上轮工具执行结果]: {text_content}"
                })

        elif role == 'user':
            current_assistant_tool_ids = set()
            if isinstance(content, list):
                new_blocks = []
                for b in content:
                    if isinstance(b, dict) and b.get('type') == 'tool_result':
                        tid = b.get('tool_use_id')
                        prev_is_assistant = (
                            cleaned and 
                            cleaned[-1].get('role') == 'assistant' and 
                            (str(tid) in current_assistant_tool_ids)
                        )
                        if prev_is_assistant:
                            new_blocks.append(b)
                        else:
                            logger.warning(f"[TOOL_CHAIN_GUARD] 发现孤立 tool_result (tool_use_id={tid})，降级为 text block")
                            res_content = b.get('content', '')
                            res_str = res_content if isinstance(res_content, str) else json.dumps(res_content, ensure_ascii=False)
                            new_blocks.append({
                                'type': 'text',
                                'text': f"[上轮工具执行结果]: {res_str}"
                            })
                    else:
                        new_blocks.append(b)
                new_msg = dict(msg)
                new_msg['content'] = new_blocks
                cleaned.append(new_msg)
            else:
                cleaned.append(msg)
        else:
            current_assistant_tool_ids = set()
            cleaned.append(msg)

    first_non_sys_idx = -1
    for i, m in enumerate(cleaned):
        if m.get('role') != 'system':
            first_non_sys_idx = i
            break
            
    if first_non_sys_idx != -1:
        first_role = cleaned[first_non_sys_idx].get('role')
        if first_role in ('assistant', 'tool'):
            logger.warning(f"[TOOL_CHAIN_GUARD] 首条对话消息为 {first_role}，自动修正为合法对话流")
            if first_role == 'tool':
                cleaned[first_non_sys_idx]['role'] = 'user'
            else:
                cleaned.insert(first_non_sys_idx, {'role': 'user', 'content': '请继续。'})

    merged = []
    for m in cleaned:
        if merged and merged[-1].get('role') == 'user' and m.get('role') == 'user':
            c1 = merged[-1].get('content', '')
            c2 = m.get('content', '')
            if isinstance(c1, str) and isinstance(c2, str):
                merged[-1]['content'] = f"{c1}\n\n{c2}"
            elif isinstance(c1, list) and isinstance(c2, list):
                merged[-1]['content'] = c1 + c2
            elif isinstance(c1, list) and isinstance(c2, str):
                merged[-1]['content'] = c1 + [{'type': 'text', 'text': c2}]
            elif isinstance(c1, str) and isinstance(c2, list):
                merged[-1]['content'] = [{'type': 'text', 'text': c1}] + c2
        else:
            merged.append(m)

    return merged


async def proxy_chat_completion(request_body: dict, provider: dict, conversation_id: str = None, source: str = 'operit', skip_messages_table: bool = False, client_host: str = '1.14.59.116:8080'):
    """
    转发 chat completion 请求到上游供应商
    根据 stream 参数自动选择流式或非流式转发
    conversation_id: 可选，传入时会自动存储AI回复
    """
    # [工具链终极防护] 自动防御与修复孤立 tool_result，杜绝上游 400 报错
    if 'messages' in request_body and isinstance(request_body['messages'], list):
        request_body['messages'] = sanitize_tool_chain(request_body['messages'])

    # [新增逻辑] 拦截并清洗平台预设的 system prompt
    if 'messages' in request_body and isinstance(request_body['messages'], list):
        for msg in request_body['messages']:
            if msg.get('role') == 'system':
                original_content = msg.get('content', '')
                if isinstance(original_content, str):
                    msg['content'] = clean_system_prompt(original_content)

    # === [PREFILL_GUARD] 修复 Claude / Thinking 等模型不支持 Assistant Prefill 的报错 ===
    # 上游报错: "This model does not support assistant message prefill. The conversation must end with a user message."
    # 发生场景：Operit 在执行完本地工具后，将未完的 assistant 消息作为结尾重新发起请求续写
    if 'messages' in request_body and isinstance(request_body['messages'], list) and request_body['messages']:
        msgs = request_body['messages']
        # 1. 弹出末尾所有空内容或纯空白的 assistant 消息
        while msgs and msgs[-1].get('role') == 'assistant':
            c = msgs[-1].get('content', '')
            if not c or (isinstance(c, str) and not c.strip()):
                msgs.pop()
                logger.info("[PREFILL_GUARD] 移除了末尾空 assistant 消息")
            else:
                break
        
        # 2. 如果末尾仍然是 assistant 消息（客户端尝试做 assistant prefill 续写）
        # 追加一条简短 user 消息触发后续生成，完全绕过上游对 assistant prefill 的拦截
        if msgs and msgs[-1].get('role') == 'assistant':
            msgs.append({
                'role': 'user',
                'content': '请继续。'
            })
            logger.info("[PREFILL_GUARD] 检测到末尾为 assistant 消息，自动追加 '请继续。'，避免上游拒绝 assistant prefill")
        # [IMAGE_MODEL_GUARD] 图像模型自动剔除 tools / tool_choice，防止上游 400 报错
    req_model = request_body.get('model', '')
    if is_image_model(req_model):
        if 'tools' in request_body:
            del request_body['tools']
            logger.info(f"[IMAGE_MODEL_GUARD] 检测到图像模型 ({req_model})，自动移除 tools 参数")
        if 'tool_choice' in request_body:
            del request_body['tool_choice']

    upstream_url = build_upstream_url(provider['api_base_url'])
    is_stream = request_body.get('stream', False)

    headers = {
        'Authorization': f'Bearer {provider["api_key"]}',
        'Content-Type': 'application/json',
    }

    # 超时设置：连接 10 秒，读取 300 秒（长回复需要）
    timeout = httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)

    logger.info(f"转发请求到 {provider['name']}: {upstream_url} (stream={is_stream})")
    body_str = json.dumps(request_body)
    msgs = request_body.get('messages', [])
    role_totals = {}
    for i, msg in enumerate(msgs):
        content = msg.get('content', '')
        content_str = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        clen = len(content_str)
        role = msg.get('role', '?')
        role_totals[role] = role_totals.get(role, 0) + clen
        preview = content_str[:80]

    # 将最终发送给大模型的包持久化下来以供调试
    try:
        with open("/home/ubuntu/caeron-gateway/last_request.json", "w", encoding="utf-8") as f:
            json.dump(request_body, f, ensure_ascii=False, indent=2)
    except Exception as e:
        import logging; logging.getLogger(__name__).debug(f"Exception ignored: {e}")

    if is_stream:
        return await _proxy_stream(upstream_url, headers, request_body, timeout, provider, conversation_id, source, skip_messages_table, client_host=client_host)
    else:
        return await _proxy_json(upstream_url, headers, request_body, timeout, provider, conversation_id, source, skip_messages_table, client_host=client_host)


async def _proxy_stream(url: str, headers: dict, body: dict, timeout, provider: dict, conversation_id: str = None, source: str = 'operit', skip_messages_table: bool = False, client_host: str = '1.14.59.116:8080'):
    """
    流式转发：使用 httpx stream 模式逐 chunk 转发 SSE 事件
    正确处理 data: {...}\n\n 格式和 [DONE] 标记
    同时收集AI回复内容用于存储
    """
    client = httpx.AsyncClient(timeout=timeout)

    try:
        # 构建并发送流式请求
        req = client.build_request('POST', url, json=body, headers=headers)
        response = await client.send(req, stream=True)

        if response.status_code != 200:
            error_body = await response.aread()
            await response.aclose()
            await client.aclose()
            raise Exception(
                f"上游返回 {response.status_code}: {error_body.decode('utf-8', errors='replace')[:500]}"
            )

        # 收集流式回复内容的容器
        collected_chunks = []
        _conv_id = conversation_id  # 闭包捕获

        async def stream_generator():
            """SSE 事件流生成器，边转发边收集AI回复"""
            is_img_model = is_image_model(body.get('model', ''))
            img_chunks_buffer = []
            think_parser = ThinkTagParser()
            reasoning_parser = ReasoningStreamParser()
            exec_filter = ExecTagStreamFilter()
            was_in_think = False
            has_emitted_content = False
            try:
                async for line in response.aiter_lines():
                    # 空行直接跳过（SSE 分隔符）
                    if line.strip() == '':
                        continue
                    # 已经是 data: 开头的 SSE 格式，直接转发
                    if line.startswith('data: '):
                        # 收集delta内容
                        data_str = line[6:].strip()
                        if data_str == '[DONE]':
                            if is_img_model and img_chunks_buffer:
                                full_img_text = ''.join(img_chunks_buffer)
                                clean_img_text = transform_image_content(full_img_text, client_host)
                                collected_chunks.append(clean_img_text)
                                img_chunk = json.dumps({
                                    "id": f"img-{int(time.time())}",
                                    "object": "chat.completion.chunk",
                                    "model": body.get("model", "gpt-image"),
                                    "choices": [{"delta": {"content": clean_img_text}, "finish_reason": "stop"}]
                                }, ensure_ascii=False)
                                yield f"data: {img_chunk}\n\n"
                                yield f"{line}\n\n"
                                continue

                            # 冲洗 reasoning_parser 剩余的 buffer
                            for is_think, r_text in reasoning_parser.finalize():
                                if not r_text:
                                    continue
                                out_text = r_text
                                if is_think:
                                    if not was_in_think:
                                        out_text = "\n\n<think>\n" + out_text
                                        was_in_think = True
                                else:
                                    out_text = exec_filter.process(out_text)
                                    if was_in_think:
                                        out_text = "\n</think>\n\n" + out_text
                                        was_in_think = False
                                    collected_chunks.append(r_text)
                                    if r_text.strip():
                                        has_emitted_content = True
                                if out_text:
                                    drain_chunk = json.dumps({"id": "proxy-drain", "object": "chat.completion.chunk", "model": "proxy", "choices": [{"delta": {"content": out_text}}]}, ensure_ascii=False)
                                    yield f"data: {drain_chunk}\n\n"

                            # 冲洗 exec_filter 中可能残留的普通文本
                            flush_text = exec_filter.flush()
                            if flush_text:
                                flush_chunk = json.dumps({"id": "proxy-flush", "object": "chat.completion.chunk", "model": "proxy", "choices": [{"delta": {"content": flush_text}}]}, ensure_ascii=False)
                                yield f"data: {flush_chunk}\n\n"
                                if flush_text.strip():
                                    has_emitted_content = True

                            if was_in_think:
                                close_chunk = json.dumps({"id": "proxy-close", "object": "chat.completion.chunk", "model": "proxy", "choices": [{"delta": {"content": "\n</think>\n\n"}}]}, ensure_ascii=False)
                                yield f"data: {close_chunk}\n\n"
                                was_in_think = False

                            # 兜底防御：若本次流只有思考链，没有任何正文输出，Operit 会抛出 "AI犯了一个错误" 并注入警告
                            # 自动补齐简短正文，彻底消除客户端红条与警告死循环
                            if not has_emitted_content:
                                fallback_text = "我这就去查。\n"
                                collected_chunks.append(fallback_text)
                                fallback_chunk = json.dumps({"id": "proxy-fallback", "object": "chat.completion.chunk", "model": "proxy", "choices": [{"delta": {"content": fallback_text}}]}, ensure_ascii=False)
                                yield f"data: {fallback_chunk}\n\n"

                            yield f"{line}\n\n"
                            continue
                            
                        try:
                            chunk_data = json.loads(data_str)
                            delta = chunk_data.get('choices', [{}])[0].get('delta', {})
                            
                            has_content = 'content' in delta and delta['content']
                            has_reasoning = 'reasoning_content' in delta and delta['reasoning_content']
                            
                            if has_content or has_reasoning:
                                original_c = delta.get('content', '')
                                if is_img_model:
                                    if original_c:
                                        img_chunks_buffer.append(original_c)
                                    continue
                                reasoning_c = delta.get('reasoning_content', '')
                                
                                parts = []
                                if has_reasoning:
                                    for is_think, r_text in reasoning_parser.process(reasoning_c):
                                        parts.append((is_think, r_text))
                                        if not is_think and r_text:
                                            collected_chunks.append(r_text)
                                            if r_text.strip():
                                                has_emitted_content = True
                                                
                                if has_content:
                                    if '<status type="warning">' in original_c or '警告：请输出正文内容' in original_c:
                                        original_c = original_c.replace('<status type="warning">警告：请输出正文内容，禁止仅输出思考内容。</status>', '我这就去查。\n')
                                        original_c = original_c.replace('警告：请输出正文内容，禁止仅输出思考内容。', '我这就去查。\n')
                                    
                                    parsed_parts = think_parser.process(original_c)
                                    parts.extend(parsed_parts)
                                    for is_t, t_txt in parsed_parts:
                                        if not is_t and t_txt:
                                            collected_chunks.append(t_txt)
                                            if t_txt.strip():
                                                has_emitted_content = True
                                
                                for is_think, text in parts:
                                    if not text and was_in_think == is_think:
                                        continue
                                        
                                    out_text = text
                                    if is_think:
                                        if not was_in_think:
                                            out_text = "\n\n<think>\n" + out_text
                                            was_in_think = True
                                    else:
                                        # 正文流经 exec 过滤器，剔除 <exec>...</exec> 标签
                                        out_text = exec_filter.process(out_text)
                                        if was_in_think:
                                            out_text = "\n</think>\n\n" + out_text
                                            was_in_think = False
                                        
                                    if not out_text:
                                        continue
                                        
                                    new_delta = delta.copy()
                                    new_delta['content'] = out_text
                                    if 'reasoning_content' in new_delta:
                                        del new_delta['reasoning_content']
                                        
                                    chunk_data['choices'][0]['delta'] = new_delta
                                    yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"
                                
                                continue

                            if is_img_model:
                                if 'b64_json' in delta:
                                    fname = save_base64_data_to_file(delta['b64_json'])
                                    if fname:
                                        img_chunks_buffer.append(f"\n\n![生成的图片](http://{client_host}/static/generated_images/{fname})\n\n")
                                continue

                            # === [TOOL_CALLS_ORDER_GUARD] 确保工具调用时思考标签已闭合且正文已输出 ===
                            has_tool_calls = 'tool_calls' in delta and delta['tool_calls']
                            if has_tool_calls:
                                # 客户端 (如 Operit) 禁止在思考链中调用工具，且要求在正文中有输出
                                # 1. 若当前仍在思考标签内，立即闭合 </think>
                                if was_in_think:
                                    close_chunk = json.dumps({"id": "proxy-close", "object": "chat.completion.chunk", "model": "proxy", "choices": [{"delta": {"content": "\n</think>\n\n"}}]}, ensure_ascii=False)
                                    yield f"data: {close_chunk}\n\n"
                                    was_in_think = False

                                # 2. 若尚未输出过正文，立即发送提示词（保证正文有内容，满足 Operit 正文调工具要求）
                                if not has_emitted_content:
                                    fallback_text = "我这就去查。\n"
                                    collected_chunks.append(fallback_text)
                                    fallback_chunk = json.dumps({"id": "proxy-fallback", "object": "chat.completion.chunk", "model": "proxy", "choices": [{"delta": {"content": fallback_text}}]}, ensure_ascii=False)
                                    yield f"data: {fallback_chunk}\n\n"
                                    has_emitted_content = True

                                yield f"{line}\n\n"
                                continue
                                
                        except (json.JSONDecodeError, IndexError, KeyError) as e:
                            import logging; logging.getLogger(__name__).debug(f"Exception ignored: {e}")
                        yield f"{line}\n\n"
                    # 其他格式的行也包装为 SSE
                    else:
                        yield f"data: {line}\n\n"
            except Exception as e:
                logger.error(f"流式转发错误: {e}")
                error_data = json.dumps({'error': {'message': str(e)}})
                yield f"data: {error_data}\n\n"
            finally:
                await response.aclose()
                await client.aclose()
                # 流结束后处理可执行标签并存储剥离后的回复
                if _conv_id and collected_chunks:
                    full_content = ''.join(collected_chunks)
                    # 1. 优先使用过滤器中捕获的 exec_blocks 列表
                    exec_blocks = exec_filter.exec_blocks
                    if not exec_blocks and '<exec' in full_content:
                        exec_blocks = extract_exec_tags(full_content)
                    
                    if exec_blocks:
                        logger.info(f"[PROXY_STREAM] 捕获到 {len(exec_blocks)} 个 exec 块，启动异步路由执行")
                        asyncio.create_task(process_and_route(exec_blocks, _conv_id))
                    
                    # 2. 从入库内容中剥离 <exec> 标签及任何残留的 <think> 标签
                    clean_full_content = strip_exec_tags(full_content)
                    clean_full_content = re.sub(r'<think>[\s\S]*?</think>\s*', '', clean_full_content).strip()
                    try:
                        await store_assistant_response(_conv_id, clean_full_content, source=source, skip_messages_table=skip_messages_table)
                    except Exception as e:
                        logger.error(f"流式回复存储失败: {e}")

        # 供应商名可能包含中文，HTTP头只允许ASCII，用URL编码
        safe_name = quote(provider['name'], safe='')
        return StreamingResponse(
            stream_generator(),
            media_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
                'X-Provider': safe_name,
            }
        )
    except Exception as e:
        await client.aclose()
        raise


async def _proxy_json(url: str, headers: dict, body: dict, timeout, provider: dict, conversation_id: str = None, source: str = 'operit', skip_messages_table: bool = False, client_host: str = '1.14.59.116:8080'):
    """非流式转发：直接转发 JSON 响应，并存储AI回复"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=body, headers=headers)

        if response.status_code != 200:
            raise Exception(
                f"上游返回 {response.status_code}: {response.text[:500]}"
            )

        response_data = response.json()

        # [IMAGE_INTERCEPT] 拦截并转存 Base64 图片
        try:
            choices = response_data.get('choices', [])
            if choices:
                msg = choices[0].get('message', {})
                if 'b64_json' in msg:
                    fname = save_base64_data_to_file(msg['b64_json'])
                    if fname:
                        msg['content'] = f"\n\n![生成的图片](http://{client_host}/static/generated_images/{fname})\n\n"
                        del msg['b64_json']
                if 'content' in msg:
                    msg['content'] = transform_image_content(msg['content'], client_host)
            elif 'data' in response_data and isinstance(response_data['data'], list):
                md_imgs = []
                for d in response_data['data']:
                    if isinstance(d, dict):
                        if 'b64_json' in d:
                            fname = save_base64_data_to_file(d['b64_json'])
                            if fname:
                                md_imgs.append(f"![生成的图片](http://{client_host}/static/generated_images/{fname})")
                        elif 'url' in d:
                            md_imgs.append(f"![生成的图片]({d['url']})")
                if md_imgs:
                    response_data['choices'] = [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "\n\n".join(md_imgs)
                        },
                        "finish_reason": "stop"
                    }]
        except Exception as e:
            logger.error(f"[IMAGE_INTERCEPT] 非流式图片响应处理异常: {e}", exc_info=True)

        # 处理与存储AI回复
        if conversation_id:
            try:
                choices = response_data.get('choices', [])
                if choices:
                    content = choices[0].get('message', {}).get('content', '')
                    if content:
                        clean_content = content
                        if '<exec' in content:
                            exec_blocks = extract_exec_tags(content)
                            if exec_blocks:
                                logger.info(f"[PROXY_JSON] 捕获到 {len(exec_blocks)} 个 exec 块，启动异步路由执行")
                                asyncio.create_task(process_and_route(exec_blocks, conversation_id))
                            clean_content = strip_exec_tags(content)
                        choices[0]['message']['content'] = clean_content
                        await store_assistant_response(conversation_id, clean_content, source=source, skip_messages_table=skip_messages_table)
            except Exception as e:
                logger.error(f"非流式回复处理/存储失败: {e}")

        safe_name = quote(provider['name'], safe='')
        return JSONResponse(
            content=response_data,
            headers={'X-Provider': safe_name}
        )


async def proxy_models(provider: dict):
    """转发模型列表请求到上游供应商"""
    url = build_models_url(provider['api_base_url'])

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            url,
            headers={'Authorization': f'Bearer {provider["api_key"]}'}
        )

        if response.status_code != 200:
            raise Exception(f"获取模型列表失败: HTTP {response.status_code}")

        return JSONResponse(content=response.json())