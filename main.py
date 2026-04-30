from datetime import datetime, timedelta
from utils import now_cst, today_cst_str
import calendar
"""
Caeron Gateway - FastAPI 应用入口
核心路由定义：健康检查、模型列表、chat completions 转发、供应商管理 API
"""

import json
import logging
import os
import re
import asyncio
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from dotenv import load_dotenv

from database import init_db, get_db
from config import init_default_config, get_config, set_config
from providers import ProviderManager
from proxy import proxy_chat_completion, proxy_models
from injection import InjectionEngine
from message_store import generate_conversation_id, ensure_conversation, store_incoming_messages
from summarizer import get_summarizer

# 加载环境变量
load_dotenv()

# Admin Token
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# 配置日志格式：[时间] [级别] [模块] 消息
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('caeron')

# 初始化供应商管理器（全局单例）
provider_manager = ProviderManager()


async def _summary_cron_loop():
    """后台定时任务：每天UTC 15:59（北京时间23:59）触发日总，周日额外触发周总，月末额外触发月总"""
    from summarizer import run_daily_cron, run_weekly_cron, run_monthly_cron
    from datetime import datetime
    
    while True:
        try:
            # 计算距离下一个UTC 15:59的秒数
            now = now_cst()
            target = now.replace(hour=23, minute=59, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            
            logger.info(f"[CRON] 下次总结触发: {target.isoformat()}Z (等待 {wait_seconds:.0f}s)")
            await asyncio.sleep(wait_seconds)
            
            # 到点了，执行日总
            trigger_time = now_cst()
            logger.info(f"[CRON] 定时触发: {trigger_time.isoformat()}Z")
            
            await run_daily_cron()
            
            # 判断是否周日（北京时间）-> UTC周日15:59 = 北京时间周日23:59
            # UTC周日 weekday=6
            if trigger_time.weekday() == 6:
                logger.info("[CRON] 今天是周日，触发周总")
                await run_weekly_cron()
            
            # 判断是否月末
            beijing_date = (trigger_time + timedelta(hours=8)).date()
            _, last_day = calendar.monthrange(beijing_date.year, beijing_date.month)
            if beijing_date.day == last_day:
                logger.info("[CRON] 今天是月末，触发月总")
                await run_monthly_cron()
            
        except asyncio.CancelledError:
            logger.info("[CRON] 定时任务已取消")
            break
        except Exception as e:
            logger.error(f"[CRON] 定时任务异常: {e}")
            await asyncio.sleep(60)  # 出错后等60秒重试


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时初始化数据库和配置"""
    logger.info("=" * 50)
    logger.info("Caeron Gateway 启动中...")
    logger.info("=" * 50)
    await init_db()
    await init_default_config()

    # 启动时重置所有供应商为健康状态（服务重启 = 清空历史故障）
    from database import get_db as _get_db
    db = await _get_db()
    try:
        await db.execute(
            'UPDATE providers SET is_healthy = 1, last_error = NULL, '
            'unhealthy_since = NULL, fail_count = 0 WHERE is_enabled = 1'
        )
        await db.commit()
        logger.info("已重置所有供应商健康状态")
    finally:
        await db.close()

    # 启动后台健康探针
    health_probe_task = asyncio.create_task(provider_manager.start_health_probe_loop())

    # 启动定时总结任务（日总/周总/月总）
    summary_cron_task = asyncio.create_task(_summary_cron_loop())
    logger.info("Caeron Gateway 启动完成，等待请求...")

    yield

    # 关闭时停止后台任务
    health_probe_task.cancel()
    summary_cron_task.cancel()
    for task in [health_probe_task, summary_cron_task]:
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("Caeron Gateway 已关闭")


app = FastAPI(
    title="Caeron Gateway",
    description="OpenAI 兼容的 API 中转网关",
    version="0.1.0",
    lifespan=lifespan
)


# ==================== Admin 认证中间件 ====================
class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/admin/api/"):
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
            else:
                token = request.query_params.get("token", "")
            if not ADMIN_TOKEN or token != ADMIN_TOKEN:
                from starlette.responses import JSONResponse as SJR
                return SJR(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)

app.add_middleware(AdminAuthMiddleware)

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
    """
    # DEBUG: Log all request headers to identify Operit session markers
    hdrs = dict(request.headers)
    interesting = {k: v for k, v in hdrs.items() if k.lower() not in ('authorization', 'host', 'content-type', 'content-length', 'accept', 'accept-encoding', 'connection', 'user-agent')}
    if interesting:
        logger.info(f"REQUEST HEADERS (non-standard): {interesting}")
    else:
        logger.info(f"REQUEST HEADERS: only standard headers, UA={hdrs.get('user-agent', 'N/A')[:80]}")
    """
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

    # === 总结请求拦截器 ===
    # 检测Operit的总结请求并拦截，阻止其消耗上游token
    SUMMARY_FINGERPRINTS = [
        '你是负责生成对话摘要的AI助手',
        '对话摘要',
        'Conversation Summary',
        'conversation summary',
        'summarize',
        '生成摘要',
        '总结以下对话',
        '请总结',
        '对话总结',
    ]
    is_summary_request = False
    for msg in body.get('messages', []):
        if msg.get('role') == 'system':
            sys_content = str(msg.get('content', ''))
            content_preview = sys_content[:300]
            for fp in SUMMARY_FINGERPRINTS:
                if fp in content_preview:
                    is_summary_request = True
                    logger.info(f"[SUMMARY_INTERCEPT] 指纹命中: '{fp}' in system消息前300字符")
                    break
            if is_summary_request:
                break
    
    # 额外检测：仅当消息数<=3 且 最后一条user消息很短（Operit自动总结的特征）
    # 且assistant消息里包含旧摘要标记时才判定
    messages = body.get('messages', [])
    if not is_summary_request and len(messages) <= 3:
        last_user_msg = ''
        for msg in reversed(messages):
            if msg.get('role') == 'user':
                last_user_msg = str(msg.get('content', ''))
                break
        if len(last_user_msg) <= 50:
            for msg in messages:
                content = str(msg.get('content', ''))
                if msg.get('role') == 'assistant' and ('==========对话摘要==========' in content or 'Conversation Summary' in content):
                    is_summary_request = True
                    logger.info(f"[SUMMARY_INTERCEPT] 摘要标记+短user消息({len(last_user_msg)}字), 判定为总结请求")
                    break
    
    if not is_summary_request:
        # 调试日志：打印所有system消息的前200字符，帮助排查漏网的总结请求
        for i, msg in enumerate(body.get('messages', [])):
            if msg.get('role') == 'system':
                preview = str(msg.get('content', ''))[:200]
                logger.info(f"[SUMMARY_DEBUG] system msg[{i}] preview: {preview}")
    
    
    if is_summary_request:
        logger.info(f"[SUMMARY_INTERCEPT] 拦截到Operit总结请求")
        import time as _time
        import json as _json
        import asyncio as _asyncio
        
        # 策略：立即返回缓存的上次总结（防止Operit超时断开），后台异步更新
        summarizer = get_summarizer()
        
        # 1. 先尝试读取缓存的上次总结
        try:
            cached_summary = await summarizer._get_latest_summary()
        except Exception as e:
            logger.error(f"[SUMMARY_INTERCEPT] 读取缓存总结失败: {e}")
            cached_summary = None
        
        if cached_summary:
            global_summary = cached_summary
            logger.info(f"[SUMMARY_INTERCEPT] 使用缓存总结即时返回 ({len(global_summary)} 字符)")
            
            # 后台异步触发新总结生成（不阻塞响应）
            async def _bg_update_summary():
                try:
                    new_summary = await summarizer.generate_global_summary()
                    logger.info(f"[SUMMARY_INTERCEPT] 后台总结更新完成 ({len(new_summary)} 字符)")
                except Exception as e:
                    logger.error(f"[SUMMARY_INTERCEPT] 后台总结更新失败: {e}")
            
            _asyncio.create_task(_bg_update_summary())
        else:
            # 没有缓存，只能同步等（首次运行的情况）
            logger.info(f"[SUMMARY_INTERCEPT] 无缓存总结，同步生成中...")
            try:
                global_summary = await summarizer.generate_global_summary()
                logger.info(f"[SUMMARY_INTERCEPT] 首次总结生成完成 ({len(global_summary)} 字符)")
            except Exception as e:
                logger.error(f"[SUMMARY_INTERCEPT] 总结引擎异常: {e}，fallback到空摘要")
                global_summary = summarizer._empty_summary()
        
        if body.get('stream', False):
            # 流式响应：SSE格式
            from starlette.responses import StreamingResponse
            
            async def fake_stream():
                chunk_id = f"chatcmpl-intercept-{int(_time.time())}"
                # 发送内容chunk
                data = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": int(_time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": global_summary},
                        "finish_reason": None
                    }]
                }
                yield f"data: {_json.dumps(data)}\n\n"
                # 发送结束chunk
                end_data = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": int(_time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop"
                    }]
                }
                yield f"data: {_json.dumps(end_data)}\n\n"
                yield "data: [DONE]\n\n"
            
            return StreamingResponse(
                fake_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Caeron-Intercepted": "summary"
                }
            )
        else:
            # 非流式响应
            from starlette.responses import JSONResponse
            return JSONResponse({
                "id": f"chatcmpl-intercept-{int(_time.time())}",
                "object": "chat.completion",
                "created": int(_time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": global_summary},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }, headers={"X-Caeron-Intercepted": "summary"})
    # === 总结拦截器结束 ===

    # === Step 2: 消息存储管道 ===
    # 在injection之前，对原始消息做存档
    raw_messages = body.get('messages', [])
    conversation_id = generate_conversation_id(raw_messages)
    
    # 异步存储，不阻塞主流程（存储失败不影响请求转发）
    stored_count = 0
    try:
        await ensure_conversation(conversation_id, model=model)
        stored_count = await store_incoming_messages(conversation_id, raw_messages)
    except Exception as e:
        logger.error(f"消息存储管道异常（不影响转发）: {e}")
    
    # === Step 2.5: 主动轮总触发 ===
    # 每存入N条消息（跨对话累计），后台触发一次轮总
    if stored_count > 0:
        try:
            trigger_threshold = int(await get_config('summary_interval', '16'))
            # 从数据库读取当前累计计数
            db = await get_db()
            try:
                cursor = await db.execute("SELECT value FROM config WHERE key = '_msg_counter'")
                row = await cursor.fetchone()
                current_count = int(row['value']) if row else 0
                new_count = current_count + stored_count
                
                if new_count >= trigger_threshold:
                    # 达到阈值，后台触发轮总
                    logger.info(f"[AUTO_SUMMARY] 累计 {new_count} 条消息，触发轮总生成")
                    
                    async def _bg_round_summary():
                        try:
                            summarizer = get_summarizer()
                            result = await summarizer.generate_round_summary()
                            logger.info(f"[AUTO_SUMMARY] 轮总生成完成 ({len(result) if result else 0} 字符)")
                        except Exception as e:
                            logger.error(f"[AUTO_SUMMARY] 轮总生成失败: {e}")
                    
                    asyncio.create_task(_bg_round_summary())
                    new_count = 0  # 重置计数
                
                # 更新计数器
                await db.execute(
                    "INSERT INTO config (key, value, description) VALUES ('_msg_counter', ?, '轮总触发计数器') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(new_count),)
                )
                await db.commit()
            finally:
                await db.close()
        except Exception as e:
            logger.error(f"[AUTO_SUMMARY] 计数器异常（不影响转发）: {e}")
    # === End Step 2.5 ===
    # === End Step 2 ===

    # 提示词�������入处�����
        # === Bug 1 修复：清理幽灵省略号 ===
    # 客户端在仅发送图片/空消息时可能使用 "..." 或 "…" 占位，导致 AI 误解
    for msg in body.get('messages', []):
        if msg.get('role') == 'user' and isinstance(msg.get('content'), str):
            c = msg['content']
            if c.startswith('...') or c.startswith('…'):
                import re
                text_only = re.sub(r'<attachment[^>]*>[\\s\\S]*?</attachment>', '', c).strip()
                if text_only in ('...', '…', ''):
                    c = c.replace('...', '').replace('…', '').strip()
                    msg['content'] = c if c else ' '

    injection_engine = InjectionEngine()
    body['messages'] = await injection_engine.inject(body.get('messages', []), {'model': model, 'conversation_id': conversation_id})

    # 获取主供应商
    try:
        provider = await provider_manager.get_provider(model)
    except Exception as e:
        # 没有健康供应商时，尝试冷却期到期的供应商
        cooled = await provider_manager.get_cooled_down_providers(model=model)
        if cooled:
            provider = cooled[0]
            logger.warning(f"无健康供应商，使用冷却期到期的供应商: {provider['name']}")
        else:
            raise HTTPException(status_code=502, detail=f"没有可用的供应商: {e}")

    # 尝试转发，最多 5 次（主供应商 + 健康fallback + 冷却期到期的供应商）
    last_error = None
    tried_ids = set()
    used_cooled_down = False  # 是否已经尝试过冷却期到期的供应商

    for attempt in range(5):
        # 跳过已尝试的供应商
        if provider['id'] in tried_ids:
            # 先尝试健康的fallback
            fallbacks = await provider_manager.get_fallback_providers(model, provider['id'])
            fallbacks = [p for p in fallbacks if p['id'] not in tried_ids]

            if not fallbacks and not used_cooled_down:
                # 健康供应商���部耗尽，尝试冷却期到期的不健康供应商
                cooled = await provider_manager.get_cooled_down_providers(model=model, exclude_ids=tried_ids)
                if cooled:
                    fallbacks = cooled
                    used_cooled_down = True
                    logger.warning(
                        f"健康供应商已耗尽，启用 {len(cooled)} 个冷却期到期的供应商"
                    )

            if not fallbacks:
                break
            provider = fallbacks[0]

        tried_ids.add(provider['id'])

        try:
            # 更新最近使用时间
            await provider_manager.update_last_used(provider['id'])

            # 转发请求（传入conversation_id用于存储AI回复）
            response = await proxy_chat_completion(body, provider, conversation_id=conversation_id)

            # 成功，确保���记为健康
            await provider_manager.mark_healthy(provider['id'])
            logger.info(f"请求成功: 供应商={provider['name']}, 尝试次数={attempt + 1}")

            return response

        except Exception as e:
            last_error = str(e)
            logger.error(
                f"供应商 {provider['name']} 请求失败 (尝试 {attempt + 1}/5): {e}"
            )
            await provider_manager.mark_unhealthy(provider['id'], last_error)

            # 获取下一个 fallback 供应商
            fallbacks = await provider_manager.get_fallback_providers(model, provider['id'])
            fallbacks = [p for p in fallbacks if p['id'] not in tried_ids]

            if not fallbacks and not used_cooled_down:
                # 健康供应商全部耗尽，尝试冷却期到期的不健康供应商
                cooled = await provider_manager.get_cooled_down_providers(model=model, exclude_ids=tried_ids)
                if cooled:
                    fallbacks = cooled
                    used_cooled_down = True
                    logger.warning(
                        f"健康���应商已耗尽，启用 {len(cooled)} 个冷却期到期的供应商"
                    )

            if fallbacks:
                provider = fallbacks[0]
                logger.info(f"切换到供应商: {provider['name']}")

    # 全部失败
    raise HTTPException(
        status_code=502,
        detail=f"所有供应商均不可用（含冷却期到期重试），最后错误: {last_error}"
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
        raise HTTPException(status_code=400, detail=f"获取模型����表失败: {str(e)}")


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
            await db.execute(f"UPDATE injection_rules SET {', '.join(fields)}, updated_at = datetime('now', '+8 hours')) WHERE id = ?", values)
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


# ==================== 对话记录 API ====================

@app.get("/admin/api/conversations")
async def admin_list_conversations(start: str = None, end: str = None):
    """列出��有对话，附带消息计数，支持日期过滤"""
    db = await get_db()
    try:
        query = '''
            SELECT c.id, c.conversation_id, c.model, c.message_count, c.created_at, c.last_message_at,
                   (SELECT content FROM messages WHERE conversation_id = c.conversation_id AND role = 'user' 
                    AND content NOT LIKE '==========对话摘要%'
                    ORDER BY message_index ASC LIMIT 1) as first_user_message
            FROM conversations c
        '''
        params = []
        conditions = []
        if start:
            conditions.append("COALESCE(c.last_message_at, c.created_at) >= ?")
            params.append(start)
        if end:
            conditions.append("COALESCE(c.last_message_at, c.created_at) <= ?")
            params.append(end + ' 23:59:59')
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY COALESCE(c.last_message_at, c.created_at) DESC"
        
        cursor = await db.execute(query, params)
        rows = [dict(row) for row in await cursor.fetchall()]
        for row in rows:
            msg = row.get('first_user_message', '') or ''
            # 处理多模态 JSON 数组
            if msg.startswith('['):
                try:
                    import json
                    arr = json.loads(msg)
                    if isinstance(arr, list):
                        text_parts = [item.get('text', '') for item in arr if item.get('type') == 'text']
                        img_count = sum(1 for item in arr if item.get('type') == 'image_url')
                        msg = '\n'.join(text_parts)
                        if img_count > 0 and not msg.strip():
                            msg = '[图片消息]'
                except:
                    pass
            # 剥离 attachment 标签
            msg = re.sub(r'<attachment[^>]*>[\s\S]*?</attachment>', '', msg).strip()
            row['preview'] = msg[:100] + ('...' if len(msg) > 100 else '')
            del row['first_user_message']
        return rows
    finally:
        await db.close()


# ==================== 窗口管理 CRUD ====================

@app.get("/admin/api/windows")
async def admin_list_windows():
    """列出所有手动创建的窗口及其关联对话，加上一个'未归类'虚拟窗口"""
    db = await get_db()
    try:
        # 1. 获取所有手动窗口
        cursor = await db.execute('SELECT * FROM windows ORDER BY updated_at DESC')
        win_rows = [dict(r) for r in await cursor.fetchall()]

        windows = []
        for wr in win_rows:
            cur2 = await db.execute('''
                SELECT c.conversation_id, c.model, c.message_count, c.created_at, c.last_message_at,
                       (SELECT content FROM messages WHERE conversation_id = c.conversation_id AND role = 'user'
                        AND content NOT LIKE '==========%%' ORDER BY message_index ASC LIMIT 1) as first_real_msg
                FROM conversations c WHERE c.window_id = ?
                ORDER BY COALESCE(c.last_message_at, c.created_at) DESC
            ''', [wr['id']])
            convs = []
            total_msgs = 0
            first_active = None
            last_active = None
            for row in await cur2.fetchall():
                row = dict(row)
                preview_raw = row.get('first_real_msg') or ''
                if preview_raw.startswith('['):
                    try:
                        import json
                        arr = json.loads(preview_raw)
                        if isinstance(arr, list):
                            text_parts = [item.get('text', '') for item in arr if item.get('type') == 'text']
                            img_count = sum(1 for item in arr if item.get('type') == 'image_url')
                            preview_raw = '\n'.join(text_parts)
                            if img_count > 0 and not preview_raw.strip():
                                preview_raw = '[图片消息]'
                    except:
                        pass
                preview_raw = re.sub(r'<attachment[^>]*>[\s\S]*?</attachment>', '', preview_raw).strip()
                convs.append({
                    'conversation_id': row['conversation_id'],
                    'model': row['model'],
                    'message_count': row['message_count'],
                    'created_at': row['created_at'],
                    'last_message_at': row['last_message_at'],
                    'preview': (preview_raw[:100] + '...' if len(preview_raw) > 100 else preview_raw) if preview_raw else '',
                })
                total_msgs += row['message_count']
                ts = row['last_message_at'] or row['created_at']
                if not last_active or ts > last_active:
                    last_active = ts
                if not first_active or (row['created_at'] and row['created_at'] < first_active):
                    first_active = row['created_at']

            windows.append({
                'window_id': wr['id'],
                'title': wr['name'],
                'description': wr['description'],
                'color': wr['color'],
                'conversations': convs,
                'total_messages': total_msgs,
                'first_active': first_active,
                'last_active': last_active,
                'is_manual': True,
            })

        # 2. 未归类对话（window_id 为 NULL）
        cur3 = await db.execute('''
            SELECT c.conversation_id, c.model, c.message_count, c.created_at, c.last_message_at,
                   (SELECT content FROM messages WHERE conversation_id = c.conversation_id AND role = 'user'
                    AND content NOT LIKE '==========%%' ORDER BY message_index ASC LIMIT 1) as first_real_msg
            FROM conversations c WHERE c.window_id IS NULL
            ORDER BY COALESCE(c.last_message_at, c.created_at) DESC
        ''')
        unassigned = []
        un_total = 0
        un_first = None
        un_last = None
        for row in await cur3.fetchall():
            row = dict(row)
            preview_raw = row.get('first_real_msg') or ''
            preview_raw = re.sub(r'<attachment[^>]*>[\s\S]*?</attachment>', '', preview_raw).strip()
            unassigned.append({
                'conversation_id': row['conversation_id'],
                'model': row['model'],
                'message_count': row['message_count'],
                'created_at': row['created_at'],
                'last_message_at': row['last_message_at'],
                'preview': (preview_raw[:100] + '...' if len(preview_raw) > 100 else preview_raw) if preview_raw else '',
            })
            un_total += row['message_count']
            ts = row['last_message_at'] or row['created_at']
            if not un_last or ts > un_last:
                un_last = ts
            if not un_first or (row['created_at'] and row['created_at'] < un_first):
                un_first = row['created_at']

        if unassigned:
            windows.append({
                'window_id': None,
                'title': '未归类对话',
                'description': '尚未分配到任何窗口的对话',
                'color': '#6b7280',
                'conversations': unassigned,
                'total_messages': un_total,
                'first_active': un_first,
                'last_active': un_last,
                'is_manual': False,
            })

        return windows
    finally:
        await db.close()


@app.post("/admin/api/windows")
async def admin_create_window(request: Request):
    """创建新窗口"""
    body = await request.json()
    name = body.get('name', '').strip()
    if not name:
        return JSONResponse({'error': '窗口名称不能为空'}, status_code=400)
    description = body.get('description', '')
    color = body.get('color', '#4a90d9')
    db = await get_db()
    try:
        cursor = await db.execute(
            'INSERT INTO windows (name, description, color) VALUES (?, ?, ?)',
            [name, description, color]
        )
        await db.commit()
        return {'id': cursor.lastrowid, 'name': name, 'description': description, 'color': color}
    finally:
        await db.close()


@app.put("/admin/api/windows/{window_id}")
async def admin_update_window(window_id: int, request: Request):
    """更新窗口名称/描述/颜色"""
    body = await request.json()
    db = await get_db()
    try:
        sets = []
        params = []
        for field in ['name', 'description', 'color']:
            if field in body:
                sets.append(f"{field} = ?")
                params.append(body[field])
        if not sets:
            return JSONResponse({'error': '无更新字段'}, status_code=400)
        sets.append("updated_at = datetime('now', '+8 hours'))")
        params.append(window_id)
        await db.execute(f"UPDATE windows SET {', '.join(sets)} WHERE id = ?", params)
        await db.commit()
        return {'success': True}
    finally:
        await db.close()


@app.delete("/admin/api/windows/{window_id}")
async def admin_delete_window(window_id: int):
    """删除窗口（对话回归未归类，不删除对话本身）"""
    db = await get_db()
    try:
        await db.execute('UPDATE conversations SET window_id = NULL WHERE window_id = ?', [window_id])
        await db.execute('DELETE FROM windows WHERE id = ?', [window_id])
        await db.commit()
        return {'success': True}
    finally:
        await db.close()


@app.post("/admin/api/windows/{window_id}/assign")
async def admin_assign_conversations(window_id: int, request: Request):
    """将对话分配到指定窗口"""
    body = await request.json()
    conversation_ids = body.get('conversation_ids', [])
    if not conversation_ids:
        return JSONResponse({'error': '未指定对话'}, status_code=400)
    db = await get_db()
    try:
        cur = await db.execute('SELECT id FROM windows WHERE id = ?', [window_id])
        if not await cur.fetchone():
            return JSONResponse({'error': '窗口不存在'}, status_code=404)
        placeholders = ','.join(['?' for _ in conversation_ids])
        await db.execute(
            f'UPDATE conversations SET window_id = ? WHERE conversation_id IN ({placeholders})',
            [window_id] + conversation_ids
        )
        await db.commit()
        return {'success': True, 'assigned': len(conversation_ids)}
    finally:
        await db.close()


@app.post("/admin/api/windows/unassign")
async def admin_unassign_conversations(request: Request):
    """将对话从窗口中移除（回到未归类）"""
    body = await request.json()
    conversation_ids = body.get('conversation_ids', [])
    if not conversation_ids:
        return JSONResponse({'error': '未指定对话'}, status_code=400)
    db = await get_db()
    try:
        placeholders = ','.join(['?' for _ in conversation_ids])
        await db.execute(
            f'UPDATE conversations SET window_id = NULL WHERE conversation_id IN ({placeholders})',
            conversation_ids
        )
        await db.commit()
        return {'success': True}
    finally:
        await db.close()



@app.get("/admin/api/conversations/search")
async def admin_search_messages(q: str, limit: int = 50):
    """搜索消息内容"""
    if not q or len(q.strip()) < 1:
        return []
    db = await get_db()
    try:
        cursor = await db.execute('''
            SELECT m.id, m.conversation_id, m.role, m.content, m.message_index, m.created_at,
                   c.model
            FROM messages m
            JOIN conversations c ON c.conversation_id = m.conversation_id
            WHERE m.content LIKE ? AND m.role IN ('user', 'assistant')
            ORDER BY m.created_at DESC
            LIMIT ?
        ''', (f'%{q.strip()}%', limit))
        results = []
        for row in await cursor.fetchall():
            r = dict(row)
            content = r['content'] or ''
            # 剥离 attachment / thinking / 摘要
            content = re.sub(r'<attachment[^>]*>[\s\S]*?</attachment>', '', content)
            content = re.sub(r'<thinking>[\s\S]*?</thinking>', '', content)
            content = content.strip()
            if not content or content.startswith('==========对话摘要'):
                continue
            # 找到匹配位置并截取上下文
            idx = content.lower().find(q.strip().lower())
            if idx == -1:
                continue
            start = max(0, idx - 40)
            end = min(len(content), idx + len(q.strip()) + 80)
            snippet = ('...' if start > 0 else '') + content[start:end] + ('...' if end < len(content) else '')
            r['snippet'] = snippet
            r['match_pos'] = idx - start + (3 if start > 0 else 0)
            del r['content']
            results.append(r)
        return results
    finally:
        await db.close()


@app.get("/admin/api/conversations/{conversation_id}/messages")
async def admin_get_messages(conversation_id: str):
    """获取某个对话的所有消息"""
    db = await get_db()
    try:
        cursor = await db.execute('''
            SELECT id, conversation_id, role, content, message_index, token_count, created_at
            FROM messages
            WHERE conversation_id = ?
            ORDER BY message_index ASC
        ''', (conversation_id,))
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


@app.delete("/admin/api/conversations/{conversation_id}")
async def admin_delete_conversation(conversation_id: str):
    """删除对话及其所有消息"""
    db = await get_db()
    try:
        await db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        await db.execute("DELETE FROM conversations WHERE conversation_id = ?", (conversation_id,))
        await db.commit()
        return {"message": "对话已删除"}
    finally:
        await db.close()


# ==================== 日历热力图 API ====================

@app.get("/admin/api/calendar")
async def admin_calendar(year: int = None, month: int = None):
    """返回每日消息计数，用于日历热力图"""
    from datetime import datetime
    china_now = now_cst()
    if not year:
        year = china_now.year
    if not month:
        month = china_now.month
    start_date = f"{year}-{month:02d}-01"
    if month == 12:
        end_date = f"{year+1}-01-01"
    else:
        end_date = f"{year}-{month+1:02d}-01"
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT date(created_at) as day, count(*) as count, "
            "SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) as user_count, "
            "SUM(CASE WHEN role='assistant' THEN 1 ELSE 0 END) as assistant_count "
            "FROM messages WHERE created_at >= ? AND created_at < ? "
            "GROUP BY date(created_at) ORDER BY day",
            (start_date, end_date)
        )
        days = [dict(row) for row in await cursor.fetchall()]
        return {"year": year, "month": month, "days": days}
    finally:
        await db.close()


# ==================== 记忆总览 API ====================

@app.get("/admin/api/summaries")
async def get_summaries(tag: str = None, is_active: int = None, limit: int = 100):
    """获取总结列表，支持按tag和is_active筛选"""
    db = await get_db()
    try:
        query = "SELECT id, conversation_id, tag, content, is_active, created_at, category, valence, arousal, anchor FROM summaries WHERE 1=1"
        params = []
        if tag:
            query += " AND tag = ?"
            params.append(tag)
        if is_active is not None:
            query += " AND is_active = ?"
            params.append(is_active)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = await db.execute_fetchall(query, params)
        cols = ["id", "conversation_id", "tag", "content", "is_active", "created_at", "category", "valence", "arousal", "anchor"]
        return [{c: r[i] for i, c in enumerate(cols)} for r in rows]
    finally:
        await db.close()

@app.get("/admin/api/summaries/stats")
async def get_summary_stats():
    """获取总结统计信息"""
    db = await get_db()
    try:
        # 各tag计数
        rows = await db.execute_fetchall(
            "SELECT tag, COUNT(*) as cnt, SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) as active_cnt FROM summaries GROUP BY tag"
        )
        tag_stats = {r[0]: {"total": r[1], "active": r[2]} for r in rows}
        # 分类统计
        cat_rows = await db.execute_fetchall(
            "SELECT category, COUNT(*) as cnt FROM summaries WHERE tag='round' AND category IS NOT NULL GROUP BY category"
        )
        category_stats = {r[0]: r[1] for r in cat_rows}
        # 总数
        total_row = await db.execute_fetchall("SELECT COUNT(*) FROM summaries")
        total = total_row[0][0] if total_row else 0
        # 下次cron时间（计算到下一个UTC 15:59）
        from datetime import datetime
        _now = now_cst()
        _target = _now.replace(hour=23, minute=59, second=0, microsecond=0)
        if _now >= _target:
            _target += timedelta(days=1)
        _beijing = _target + timedelta(hours=8)
        next_cron = _beijing.strftime('%Y-%m-%d %H:%M CST')
        _wait = int((_target - _now).total_seconds())
        _hours = _wait // 3600
        _mins = (_wait % 3600) // 60
        return {"tag_stats": tag_stats, "total": total, "category_stats": category_stats, "next_cron": f"{next_cron} (约{_hours}h{_mins}m后)"}
    finally:
        await db.close()

@app.delete("/admin/api/summaries/{summary_id}")
async def delete_summary(summary_id: int):
    """删除单条总结"""
    db = await get_db()
    try:
        await db.execute("DELETE FROM summaries WHERE id = ?", [summary_id])
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()

@app.get("/admin/api/summary-config")
async def get_summary_config():
    """获取轮总触发配置"""
    db = await get_db()
    try:
        # 获取触发间隔
        cursor = await db.execute("SELECT value FROM config WHERE key = 'summary_interval'")
        row = await cursor.fetchone()
        interval = int(row['value']) if row else 16
        
        # 获取当前计数
        cursor = await db.execute("SELECT value FROM config WHERE key = '_msg_counter'")
        row = await cursor.fetchone()
        current_count = int(row['value']) if row else 0
        
        return {"interval": interval, "currentCount": current_count}
    finally:
        await db.close()

@app.post("/admin/api/summary-config")
async def save_summary_config(request: Request):
    """保存轮总触发配置"""
    data = await request.json()
    interval = data.get('interval', 16)
    
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO config (key, value, description) VALUES ('summary_interval', ?, '轮总触发消息数') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(interval),)
        )
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()

@app.post("/admin/api/trigger-round-summary")
async def trigger_round_summary():
    """手动触发轮总生成"""
    try:
        summarizer = get_summarizer()
        result = await summarizer.generate_round_summary()
        
        # 重置计数器
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO config (key, value, description) VALUES ('_msg_counter', '0', '轮总触发计数器') "
                "ON CONFLICT(key) DO UPDATE SET value = '0'"
            )
            await db.commit()
        finally:
            await db.close()
        
        return {"success": True, "length": len(result) if result else 0}
    except Exception as e:
        logger.error(f"手动触发轮总失败: {e}")
        return {"success": False, "error": str(e)}

# ==================== 管理面板 ====================

@app.get("/admin")
async def admin_panel():
    """返回管理面板 HTML 页面"""
    html_path = os.path.join(os.path.dirname(__file__), 'static', 'admin.html')
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type='text/html')
    return HTMLResponse("<h1>管理面板文件未找到</h1>", status_code=404)


# ==================== 小游戏 ====================
@app.get("/games/snake")
async def snake_game():
    """贪吃蛇小游戏"""
    html_path = os.path.join(os.path.dirname(__file__), "static", "snake.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    return HTMLResponse("<h1>游戏文件未找到</h1>", status_code=404)

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