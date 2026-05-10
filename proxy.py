"""
Caeron Gateway - 请求转发模块
负责将请求转发到上游供应商，支持流式（SSE）和非流式两种模式
"""

import json
import logging
import httpx
from urllib.parse import quote
from fastapi.responses import StreamingResponse, JSONResponse
from message_store import store_assistant_response

logger = logging.getLogger(__name__)


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


async def proxy_chat_completion(request_body: dict, provider: dict, conversation_id: str = None):
    """
    转发 chat completion 请求到上游供应商
    根据 stream 参数自动选择流式或非流式转发
    conversation_id: 可选，传入时会自动存储AI回复
    """
    # === 修复：确保消息以user结尾（部分模型不支持assistant prefill）===
    pre_msgs = request_body.get('messages', [])
    if pre_msgs and pre_msgs[-1].get('role') == 'assistant':
        pre_msgs.pop()
        logger.info(f"[PROXY_PREFILL_FIX] 删除末尾assistant消息，确保以user结尾")

    # === 最终安全网：清理 tool 调用（OpenAI格式和Claude格式都处理）===
    msgs = request_body.get('messages', [])
    cleaned_msgs = []
    tool_stripped = 0
    for msg in msgs:
        role = msg.get('role', '')
        # 删掉所有 role=tool 消息
        if role == 'tool':
            tool_stripped += 1
            continue
        if role == 'assistant':
            msg = dict(msg)  # 避免修改原对象
            # OpenAI格式：删掉 tool_calls 字段
            if 'tool_calls' in msg:
                del msg['tool_calls']
            # Claude格式：content是list且含tool_use
            content = msg.get('content', '')
            if isinstance(content, list):
                has_tool = any(isinstance(b, dict) and b.get('type') == 'tool_use' for b in content)
                if has_tool:
                    text_parts = [b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text']
                    msg['content'] = '\n'.join(text_parts) if text_parts else ''
            elif isinstance(content, str) and 'tool_use' in content:
                try:
                    import json as _json
                    parsed = _json.loads(content)
                    if isinstance(parsed, list):
                        text_parts = [b.get('text', '') for b in parsed if isinstance(b, dict) and b.get('type') == 'text']
                        msg['content'] = '\n'.join(text_parts) if text_parts else ''
                except:
                    pass
        cleaned_msgs.append(msg)
    request_body['messages'] = cleaned_msgs
    if tool_stripped > 0:
        logger.info(f"[PROXY_TOOL_CLEANUP] 清理 {tool_stripped} 条 tool 消息")

    upstream_url = build_upstream_url(provider['api_base_url'])
    is_stream = request_body.get('stream', False)

    headers = {
        'Authorization': f'Bearer {provider["api_key"]}',
        'Content-Type': 'application/json',
    }

    # 超时设置：连接 10 秒，读取 300 秒（长回复需要）
    timeout = httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)

    logger.info(f"转发请求到 {provider['name']}: {upstream_url} (stream={is_stream})")

    # [DEBUG-TOKEN] 转发调试日志
    body_str = json.dumps(request_body)
    logger.info(f"[DEBUG-TOKEN] 转发请求体总字符数: {len(body_str)}")
    msgs = request_body.get('messages', [])
    logger.info(f"[DEBUG-TOKEN] messages条数: {len(msgs)}")
    role_totals = {}
    for i, msg in enumerate(msgs):
        content = msg.get('content', '')
        content_str = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        clen = len(content_str)
        role = msg.get('role', '?')
        role_totals[role] = role_totals.get(role, 0) + clen
        preview = content_str[:80]
        logger.info(f"[DEBUG-TOKEN] msg[{i}] role={role} chars={clen} content={preview}")
    logger.info(f"[DEBUG-TOKEN] 各角色字符数合计: {role_totals}")

    if is_stream:
        return await _proxy_stream(upstream_url, headers, request_body, timeout, provider, conversation_id)
    else:
        return await _proxy_json(upstream_url, headers, request_body, timeout, provider, conversation_id)


async def _proxy_stream(url: str, headers: dict, body: dict, timeout, provider: dict, conversation_id: str = None):
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
            try:
                async for line in response.aiter_lines():
                    # 空行直接跳过（SSE 分隔符）
                    if line.strip() == '':
                        continue
                    # 已经是 data: 开头的 SSE 格式，直接转发
                    if line.startswith('data: '):
                        # 收集delta内容
                        data_str = line[6:].strip()
                        if data_str != '[DONE]':
                            try:
                                chunk_data = json.loads(data_str)
                                delta = chunk_data.get('choices', [{}])[0].get('delta', {})
                                if 'content' in delta and delta['content']:
                                    collected_chunks.append(delta['content'])
                            except (json.JSONDecodeError, IndexError, KeyError):
                                pass
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
                # 流结束后存储完整的AI回复
                if _conv_id and collected_chunks:
                    full_content = ''.join(collected_chunks)
                    try:
                        await store_assistant_response(_conv_id, full_content)
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


async def _proxy_json(url: str, headers: dict, body: dict, timeout, provider: dict, conversation_id: str = None):
    """非流式转发：直接转发 JSON 响应，并存储AI回复"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=body, headers=headers)

        if response.status_code != 200:
            raise Exception(
                f"上游返回 {response.status_code}: {response.text[:500]}"
            )

        response_data = response.json()

        # 存储AI回复
        if conversation_id:
            try:
                choices = response_data.get('choices', [])
                if choices:
                    content = choices[0].get('message', {}).get('content', '')
                    if content:
                        await store_assistant_response(conversation_id, content)
            except Exception as e:
                logger.error(f"非流式回复存储失败: {e}")

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