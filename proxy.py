"""
Caeron Gateway - 请求转发模块
负责将请求转发到上游供应商，支持流式（SSE）和非流式两种模式
"""

import json
import logging
import httpx
from fastapi.responses import StreamingResponse, JSONResponse

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


async def proxy_chat_completion(request_body: dict, provider: dict):
    """
    转发 chat completion 请求到上游供应商
    根据 stream 参数自动选择流式或非流式转发
    """
    upstream_url = build_upstream_url(provider['api_base_url'])
    is_stream = request_body.get('stream', False)

    headers = {
        'Authorization': f'Bearer {provider["api_key"]}',
        'Content-Type': 'application/json',
    }

    # 超时设置：连接 10 秒，读取 300 秒（长回复需要）
    timeout = httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0)

    logger.info(f"转发请求到 {provider['name']}: {upstream_url} (stream={is_stream})")

    if is_stream:
        return await _proxy_stream(upstream_url, headers, request_body, timeout, provider)
    else:
        return await _proxy_json(upstream_url, headers, request_body, timeout, provider)


async def _proxy_stream(url: str, headers: dict, body: dict, timeout, provider: dict):
    """
    流式转发：使用 httpx stream 模式逐 chunk 转发 SSE 事件
    正确处理 data: {...}\n\n 格式和 [DONE] 标记
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

        async def stream_generator():
            """SSE 事件流生成器"""
            try:
                async for line in response.aiter_lines():
                    # 空行直接跳过（SSE 分隔符）
                    if line.strip() == '':
                        continue
                    # 已经是 data: 开头的 SSE 格式，直接转发
                    if line.startswith('data: '):
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

        return StreamingResponse(
            stream_generator(),
            media_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
                'X-Provider': provider['name'],
            }
        )
    except Exception as e:
        await client.aclose()
        raise


async def _proxy_json(url: str, headers: dict, body: dict, timeout, provider: dict):
    """非流式转发：直接转发 JSON 响应"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=body, headers=headers)

        if response.status_code != 200:
            raise Exception(
                f"上游返回 {response.status_code}: {response.text[:500]}"
            )

        return JSONResponse(
            content=response.json(),
            headers={'X-Provider': provider['name']}
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
