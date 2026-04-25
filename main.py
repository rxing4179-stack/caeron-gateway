"""
Caeron Gateway - FastAPI 应用入口
核心路由定义：健康检查、模型列表、chat completions 转发、供应商管理 API
"""

import json
import logging
import os
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from dotenv import load_dotenv

from database import init_db, get_db
from config import init_default_config, get_config, set_config
from providers import ProviderManager
from proxy import proxy_chat_completion, proxy_models
from injection import InjectionEngine

# 加载环境变量
load_dotenv()

# 配置日志格式：[时间] [级别] [模块] 消息
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('caeron')

# 初始化供应商管理器（全局单例）
provider_manager = ProviderManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时初始化数据库和配置"""
    logger.info("=" * 50)
    logger.info("Caeron Gateway 启动中...")
    logger.info("=" * 50)
    await init_db()
    await init_default_config()
    logger.info("Caeron Gateway 启动完成，等待请求...")
    yield
    logger.info("Caeron Gateway 已关闭")


app = FastAPI(
    title="Caeron Gateway",
    description="OpenAI 兼容的 API 中转网关",
    version="0.1.0",
    lifespan=lifespan
)


# ==================== 核心路由 ====================

@app.get("/")
async def health_check():
    """健康检查端点"""
    return {
        "status": "running",
        "version": "0.1.0",
        "name": "Caeron Gateway"
    }


@app.get("/v1/models")
async def list_models(request: Request):
    """转发模型列表请求，根据 Authorization 头中的 API Key 选择对应供应商"""
    try:
        # 从请求头提取 API Key
        auth_header = request.headers.get('authorization', '')
        api_key = ''
        if auth_header.startswith('Bearer '):
            api_key = auth_header[7:].strip()

        # 优先按 API Key 精确匹配供应商
        provider = None
        if api_key:
            provider = await provider_manager.get_provider_by_api_key(api_key)

        # 匹配不到则回退到默认（优先级最高的供应商）
        if not provider:
            logger.info(f"API Key 未匹配到供应商，回退到默认")
            provider = await provider_manager.get_provider("")

        logger.info(f"模型列表请求 -> 供应商: {provider['name']}")
        return await proxy_models(provider)
    except Exception as e:
        logger.error(f"获取模型列表失败: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    核心路由：转发 chat completion 请求
    1. 解析请求体
    2. 获取最佳供应商
    3. 尝试转发，失败时自动 fallback（最多重试 2 次）
    4. 全部失败返回 502
    """
    # 解析请求体
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")

    model = body.get('model', '')
    logger.info(f"收到请求: model={model}, stream={body.get('stream', False)}")

    # 提示词注入处理
    injection_engine = InjectionEngine()
    body['messages'] = await injection_engine.inject(body.get('messages', []), {'model': model})

    # 获取主供应商
    try:
        provider = await provider_manager.get_provider(model)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"没有可用的供应商: {e}")

    # 尝试转发，最多 3 次（主供应商 + 2 次 fallback）
    last_error = None
    tried_ids = set()

    for attempt in range(3):
        # 跳过已尝试的供应商
        if provider['id'] in tried_ids:
            fallbacks = await provider_manager.get_fallback_providers(model, provider['id'])
            fallbacks = [p for p in fallbacks if p['id'] not in tried_ids]
            if not fallbacks:
                break
            provider = fallbacks[0]

        tried_ids.add(provider['id'])

        try:
            # 更新最近使用时间
            await provider_manager.update_last_used(provider['id'])

            # 转发请求
            response = await proxy_chat_completion(body, provider)

            # 成功，确保标记为健康
            await provider_manager.mark_healthy(provider['id'])
            logger.info(f"请求成功: 供应商={provider['name']}, 尝试次数={attempt + 1}")

            return response

        except Exception as e:
            last_error = str(e)
            logger.error(
                f"供应商 {provider['name']} 请求失败 (尝试 {attempt + 1}/3): {e}"
            )
            await provider_manager.mark_unhealthy(provider['id'], last_error)

            # 获取下一个 fallback 供应商
            fallbacks = await provider_manager.get_fallback_providers(model, provider['id'])
            fallbacks = [p for p in fallbacks if p['id'] not in tried_ids]
            if fallbacks:
                provider = fallbacks[0]
                logger.info(f"切换到 fallback 供应商: {provider['name']}")

    # 全部失败
    raise HTTPException(
        status_code=502,
        detail=f"所有供应商均不可用，最后错误: {last_error}"
    )


# ==================== 供应商管理 API ====================

@app.get("/admin/api/providers")
async def admin_list_providers():
    """列出所有供应商（API Key 脱敏显示）"""
    providers = await provider_manager.list_providers()
    # API Key 脱敏：只显示前 8 位 + ***
    for p in providers:
        key = p.get('api_key', '')
        p['api_key_masked'] = key[:8] + '***' if len(key) > 8 else '***'
        del p['api_key']
    return providers


@app.post("/admin/api/providers")
async def admin_add_provider(request: Request):
    """添加供应商"""
    data = await request.json()
    # 校验必填字段
    required = ['name', 'api_base_url', 'api_key']
    for field in required:
        if field not in data or not data[field]:
            raise HTTPException(status_code=400, detail=f"缺少必填字段: {field}")
    provider_id = await provider_manager.add_provider(data)
    return {"id": provider_id, "message": "供应商添加成功"}


@app.put("/admin/api/providers/{provider_id}")
async def admin_update_provider(provider_id: int, request: Request):
    """更新供应商"""
    data = await request.json()
    await provider_manager.update_provider(provider_id, data)
    return {"message": "供应商更新成功"}


@app.delete("/admin/api/providers/{provider_id}")
async def admin_delete_provider(provider_id: int):
    """删除供应商"""
    await provider_manager.delete_provider(provider_id)
    return {"message": "供应商删除成功"}


@app.post("/admin/api/providers/{provider_id}/test")
async def admin_test_provider(provider_id: int):
    """测试供应商连通性"""
    result = await provider_manager.test_provider(provider_id)
    return result


from pydantic import BaseModel

class FetchModelsRequest(BaseModel):
    base_url: str
    api_key: str

@app.post("/admin/api/providers/fetch-models")
async def admin_fetch_models(req: FetchModelsRequest):
    """代理拉取上游模型列表"""
    import httpx
    try:
        base_url = req.base_url.rstrip('/')
        url = f"{base_url}/models"
        headers = {"Authorization": f"Bearer {req.api_key}"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            models = [m.get('id') for m in data.get('data', []) if m.get('id')]
            return {"models": models}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"获取模型列表失败: {str(e)}")


# ==================== 提示词注入规则 API ====================

@app.get("/admin/api/rules")
async def admin_list_rules():
    db = await get_db()
    try:
        cursor = await db.execute('SELECT * FROM injection_rules ORDER BY priority ASC')
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()

@app.post("/admin/api/rules")
async def admin_add_rule(request: Request):
    data = await request.json()
    db = await get_db()
    try:
        cursor = await db.execute('''
            INSERT INTO injection_rules (name, content, position, role, priority, depth, match_condition, is_enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('name'), data.get('content'), data.get('position', 'system_append'),
            data.get('role', 'system'), data.get('priority', 0), data.get('depth', 0),
            data.get('match_condition', '') or '', data.get('is_enabled', 1)
        ))
        await db.commit()
        return {"id": cursor.lastrowid, "message": "规则添加成功"}
    finally:
        await db.close()

@app.put("/admin/api/rules/{rule_id}")
async def admin_update_rule(rule_id: int, request: Request):
    data = await request.json()
    db = await get_db()
    try:
        fields, values = [], []
        for k in ['name', 'content', 'position', 'role', 'priority', 'depth', 'match_condition', 'is_enabled']:
            if k in data:
                fields.append(f"{k} = ?")
                values.append(data[k])
        if fields:
            values.append(rule_id)
            await db.execute(f"UPDATE injection_rules SET {', '.join(fields)}, updated_at = datetime('now') WHERE id = ?", values)
            await db.commit()
        return {"message": "规则更新成功"}
    finally:
        await db.close()

@app.delete("/admin/api/rules/{rule_id}")
async def admin_delete_rule(rule_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM injection_rules WHERE id = ?", (rule_id,))
        await db.commit()
        return {"message": "规则删除成功"}
    finally:
        await db.close()

@app.post("/admin/api/rules/preview")
async def admin_preview_rule(request: Request):
    data = await request.json()
    messages = data.get('messages', [])
    model = data.get('model', '')
    engine = InjectionEngine()
    injected = await engine.inject(messages, {'model': model})
    return {"original": messages, "injected": injected}


# ==================== 配置管理 API ====================

@app.get("/admin/api/config")
async def admin_list_config():
    """列出所有配置项"""
    db = await get_db()
    try:
        cursor = await db.execute('SELECT * FROM config ORDER BY key')
        configs = [dict(row) for row in await cursor.fetchall()]
        return configs
    finally:
        await db.close()


@app.put("/admin/api/config/{key}")
async def admin_update_config(key: str, request: Request):
    """更新配置项"""
    data = await request.json()
    await set_config(key, data['value'])
    return {"message": f"配置 {key} 更新成功"}


# ==================== 管理面板 ====================

@app.get("/admin")
async def admin_panel():
    """返回管理面板 HTML 页面"""
    html_path = os.path.join(os.path.dirname(__file__), 'static', 'admin.html')
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type='text/html')
    return HTMLResponse("<h1>管理面板文件未找到</h1>", status_code=404)


# ==================== 启动入口 ====================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    logger.info(f"启动服务器: 0.0.0.0:{port}")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info"
    )