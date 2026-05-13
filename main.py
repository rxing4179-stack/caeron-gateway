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

# 技术模式：跳过轮总触发和上下文压缩
tech_mode = True  # 默认技术模式，手动切回日常时自动总结


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
        logger.info("�������������重置所有供应商健康状态")
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

from log_viewer import log_app
app.mount("/syslogs", log_app)

from qq_adapter import qq_router
app.include_router(qq_router)

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

import time

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        
        try:
            response = await call_next(request)
        except Exception as e:
            # Error happened during request processing
            raise e
        finally:
            # We want to log completions
            if request.url.path == "/v1/chat/completions" and hasattr(request.state, 'log_info'):
                duration = (time.time() - start_time) * 1000
                info = request.state.log_info
                ip = request.client.host if request.client else "Unknown"
                ua = request.headers.get("user-agent", "Unknown UA")
                
                # Try to use x-forwarded-for if behind proxy
                forwarded_for = request.headers.get("x-forwarded-for")
                if forwarded_for:
                    ip = forwarded_for.split(',')[0].strip()
                    
                source = f"{ip} - {ua}"
                
                # Send to log viewer
                from log_viewer import log_request_event
                asyncio.create_task(log_request_event(
                    source=source,
                    last_user_msg=info.get('msg', ''),
                    status_code=response.status_code,
                    duration_ms=duration,
                    is_stream=info.get('is_stream', False)
                ))
                
        return response

app.add_middleware(RequestLoggingMiddleware)

# ==================== 核心路由 ====================

@app.get("/")
async def health_check():
    """健康检查端点"""
    return {
        "status": "running",
        "version": "0.1.0",
        "name": "Caeron Gateway"
    }

@app.get("/napcat/qr")
async def napcat_qr():
    """实时获取NapCat登录二维码"""
    import subprocess, tempfile, os
    try:
        tmp = '/tmp/napcat_qr_live.png'
        subprocess.run(['sudo', 'docker', 'cp', 'napcat:/app/napcat/cache/qrcode.png', tmp],
                      capture_output=True, timeout=5)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            return FileResponse(tmp, media_type='image/png')
        else:
            return JSONResponse({'error': 'QR code not found'}, status_code=404)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


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

        # 匹配不到则回���������认（优先级最高的供应商）
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
    # 全局异常捕获（调试500用）
    try:
        return await _handle_chat_completions(request)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"[FATAL] chat_completions 未捕获异常: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

async def _handle_chat_completions(request: Request):
    # 解析请求体
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")

    model = body.get('model', '')
    is_stream = body.get('stream', False)
    logger.info(f"收到请求: model={model}, stream={is_stream}")
    
    # 获取最后一条用户消息用于日志展示
    last_user_msg = ""
    for msg in reversed(body.get('messages', [])):
        if msg.get('role') == 'user':
            c = msg.get('content', '')
            if isinstance(c, str):
                last_user_msg = c
            else:
                last_user_msg = "[图片/文件等多模态内容]"
            break
            
    request.state.log_info = {
        'msg': last_user_msg,
        'is_stream': is_stream
    }

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
        # 调试日志：打印所有system消息的前200字符，帮助排查漏网的总��请求
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
                    
                    from database import get_db as _get_db
                    db = await _get_db()
                    try:
                        await db.execute(
                            "INSERT INTO config (key, value, description) VALUES ('_msg_counter', '0', '轮总触发计数器') "
                            "ON CONFLICT(key) DO UPDATE SET value = '0'"
                        )
                        await db.commit()
                        logger.info("[SUMMARY_INTERCEPT] 已重置 _msg_counter 为 0，确保后续请求能正确裁剪历史消息")
                    finally:
                        await db.close()
                except Exception as e:
                    logger.error(f"[SUMMARY_INTERCEPT] 后台总结更新失败: {e}")
            
            _asyncio.create_task(_bg_update_summary())
        else:
            # 没有缓存，只能同步等（首次运行的情况）
            logger.info(f"[SUMMARY_INTERCEPT] 无缓存总结，同步生成中...")
            try:
                global_summary = await summarizer.generate_global_summary()
                logger.info(f"[SUMMARY_INTERCEPT] 首次总结生成完成 ({len(global_summary)} 字符)")
                
                from database import get_db as _get_db
                db = await _get_db()
                try:
                    await db.execute(
                        "INSERT INTO config (key, value, description) VALUES ('_msg_counter', '0', '轮总触发计数器') "
                        "ON CONFLICT(key) DO UPDATE SET value = '0'"
                    )
                    await db.commit()
                    logger.info("[SUMMARY_INTERCEPT] 已重置 _msg_counter 为 0")
                finally:
                    await db.close()
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
    
    # 允许通过 Header 显式指定 session_id 和 source
    explicit_session_id = request.headers.get('x-session-id')
    explicit_source = request.headers.get('x-source', 'operit')
    
    if explicit_session_id:
        conversation_id = explicit_session_id
    else:
        conversation_id = generate_conversation_id(raw_messages)
    
    # 异步存储，不阻塞主流程（存储失败不影响请求转发）
    stored_count = 0
    _is_qq = request.headers.get('x-skip-injection', '').lower() == 'true'
    try:
        await ensure_conversation(conversation_id, model=model)
        if _is_qq:
            # QQ端自己管history，不需要store_incoming_messages
            # 只需要proxy的store_assistant_response单独存回复
            # 但仍然需要存最新的user消息（用于轮总和记忆）
            last_user = None
            for msg in reversed(raw_messages):
                if msg.get('role') == 'user':
                    last_user = msg
                    break
            if last_user:
                stored_count = await store_incoming_messages(conversation_id, [last_user])
        else:
            stored_count = await store_incoming_messages(conversation_id, raw_messages)
    except Exception as e:
        logger.error(f"消息存储管道异常（不影响转发）: {e}")
    
    # === Step 2.5: 主动轮总触发 ===
    # 每存入N条消息（跨对话累计），后台触发一次轮总
    global tech_mode
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
                
                if new_count >= trigger_threshold and not tech_mode:
                    # 达到阈值且非技术模式，后台触发轮总
                    logger.info(f"[AUTO_SUMMARY] 累计 {new_count} 条消息，触发轮总生成")
                    
                    # --- Bug 修复：防止异步并发导致的重复生成 ---
                    # 先将计数器扣除阈值（原子化概念），而不是简单置0，这样能承接并发请求的消息
                    new_count = max(0, new_count - trigger_threshold)
                    
                    logger.info(f"[AUTO_SUMMARY] 准备触发轮总。当前触发阈值:{trigger_threshold}, 剩余未总结数:{new_count}")

                    async def _bg_round_summary():
                        try:
                            summarizer = get_summarizer()
                            result = await summarizer.generate_round_summary()
                            logger.info(f"[AUTO_SUMMARY] 轮总生成完成 ({len(result) if result else 0} 字符)")
                        except Exception as e:
                            logger.error(f"[AUTO_SUMMARY] 轮总生成失败: {e}")
                    
                    asyncio.create_task(_bg_round_summary())
                elif tech_mode and new_count >= trigger_threshold:
                    logger.info(f"[AUTO_SUMMARY] 技术模式启用中，跳过轮总触发 (累计 {new_count} 条)")
                
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

    # === Step 2.8: 状态便签追踪 ===
    try:
        import re
        from utils import now_cst
        db = await get_db()
        try:
            status_updates = []
            
            # 从DB加载所有的状态项和别名
            cursor = await db.execute("SELECT content, aliases FROM memories WHERE category = 'status'")
            status_items = await cursor.fetchall()
            
            # 只检查最新一条user消息，避免历史消息反复刷新状态
            latest_user_txt = None
            for msg in reversed(body.get('messages', [])):
                if msg.get('role') == 'user':
                    txt = msg.get('content', '')
                    if isinstance(txt, str) and len(txt.strip()) > 0:
                        latest_user_txt = txt
                        break
            
            for msg in [{'content': latest_user_txt}] if latest_user_txt else []:
                    txt = msg.get('content', '')
                    if not isinstance(txt, str):
                        continue
                    
                    # 语义检查
                    has_intention = bool(re.search(r'(要|想|打算|准备|明天|待会|等下)', txt))
                    has_completion = bool(re.search(r'(了|完|过|好了|吃过|洗完|吸了)', txt))
                    
                    for row in status_items:
                        key = row['content']
                        aliases_str = row['aliases'] or ''
                        
                        patterns = [re.escape(key)]
                        for a in aliases_str.split('|'):
                            if a.strip():
                                patterns.append(re.escape(a.strip()))
                        
                        pattern = r'(' + '|'.join(patterns) + r')'
                        
                        if re.search(pattern, txt):
                            # 关键词前后需要有完成态标记词才触发写入
                            # 仅含意向态标记词时不触发
                            if has_intention and not has_completion:
                                continue
                            if has_completion:
                                status_updates.append(key)
            
            if status_updates:
                now_str = now_cst().strftime('%Y-%m-%d %H:%M:%S')
                for key in set(status_updates):
                    await db.execute(
                        "UPDATE memories SET updated_at = ? WHERE category = 'status' AND content = ?",
                        (now_str, key)
                    )
                await db.commit()
                logger.info(f"[STATUS] 更新状态便签: {set(status_updates)}")
        finally:
            await db.close()
    except Exception as e:
        logger.error(f"[STATUS] 状态便签更新异常: {e}")
    # === End Step 2.8 ===

    # === Step 3: 预处理 — 清理上下文膨胀源 ===
    # 技术模式下仍然清理网关自身注入的残留（A/B类），但跳过C类（保留原始上下文）
    # Operit会保留上一轮网关注入的内容，如果不清理，每轮都会翻倍。
    # 需要清理三类残留：
    #   A. user_wrapped_system 消息 (role=user, <system>...</system>)
    #   B. context_summaries 系统消息 (role=system, <context_summaries>)  
    #   C. Operit手动��结产��的巨型user消息 (role=user, 以摘要格式开头, >5000字符)
    raw_msgs = body.get('messages', [])
    pre_dedup_count = len(raw_msgs)
    def _msg_chars(m):
        c = m.get('content', '')
        return len(c) if isinstance(c, str) else len(json.dumps(c, ensure_ascii=False))
    pre_dedup_chars = sum(_msg_chars(m) for m in raw_msgs)
    
    cleaned_msgs = []
    strip_a = 0  # user_wrapped_system
    strip_b = 0  # context_summaries
    strip_c = 0  # Operit手动总结巨块
    
    # Operit手动总结的特征：role=user, 内容以轮总格式开头（如"【日常】""【技术】"等），
    # 且长度超过阈值（正常用户消息不会这么长）
    SUMMARY_BLOB_THRESHOLD = 5000  # 超过5000字符的"摘要式"user消息视为总结残留
    SUMMARY_BLOB_PATTERNS = ['【日常】', '【技术】', '【学习】', '==========对话摘要==========', 
                              'Conversation Summary', '对话摘要', '<context_summaries>']
    
    # 第一轮：标记每条消息的处理方式，但不立即丢弃
    # action: 'keep' | 'strip_a' | 'strip_b' | 'strip_c'
    msg_actions = []
    
    for idx, msg in enumerate(raw_msgs):
        role = msg.get('role', '')
        content = msg.get('content', '')
        if not isinstance(content, str):
            msg_actions.append('keep')
            continue
        
        stripped = content.strip()
        
        # A: 网关注入的 user_wrapped_system
        if role == 'user' and stripped.startswith('<system>') and stripped.endswith('</system>'):
            msg_actions.append('strip_a')
            continue
        
        # B: 网关注入的 context_summaries
        if role == 'system' and '<context_summaries>' in stripped:
            msg_actions.append('strip_b')
            continue
        
        # C: Operit手动总结产生的总结块消息 (可能是user或assistant)
        # 如果是 assistant 角色，只要大于 100 字符且符合特征就剔除（因为一定没有用户输入）
        # 如果是 user 角色，必须大于 5000 字符（巨块）才整体剔除，防止误吞"附带摘要的正常用户输入"
        is_assistant_summary = (role == 'assistant' and len(content) > 100)
        is_user_summary_blob = (role == 'user' and len(content) > SUMMARY_BLOB_THRESHOLD)
        
        if is_assistant_summary or is_user_summary_blob:
            first_100 = content[:100]
            if any(pat in first_100 for pat in SUMMARY_BLOB_PATTERNS):
                msg_actions.append('strip_c')
                logger.info(f"[DEDUP] 检测到Operit总结残留 (role={role}): {len(content)} 字符, 前50字={content[:50]}")
                continue
        
        msg_actions.append('keep')
    
    # === 安全检查：如果剥离 C 类消息后，没有任何真实 user 对话消息，则回退 ===
    # "真实 user 对话消息" = role=user 且内容不是 <system>...</system> 包裹的规则注入
    def _is_real_user_msg(m):
        if m.get('role') != 'user':
            return False
        c = m.get('content', '')
        if not isinstance(c, str):
            return True  # multimodal content counts as real
        s = c.strip()
        if s.startswith('<system>') and s.endswith('</system>'):
            return False
        return True
    
    # 假设 C 类全部剥离后，检查是否还有真实 user 消息
    msgs_after_strip = [raw_msgs[i] for i, a in enumerate(msg_actions) if a == 'keep']
    has_real_user_after_strip = any(_is_real_user_msg(m) for m in msgs_after_strip)
    has_pending_strip_c = any(a == 'strip_c' for a in msg_actions)
    
    if has_pending_strip_c and not has_real_user_after_strip:
        # 剥离总结残留会导致零真实用户消息 → 回退：将 strip_c 改为 keep
        logger.warning(f"[DEDUP] ⚠️ 剥离总结残留后将无真实用户消息，回退保留以防吞消息")
        msg_actions = ['keep' if a == 'strip_c' else a for a in msg_actions]
    
    # 第二轮：按标记组装最终消息列表
    for idx, (msg, action) in enumerate(zip(raw_msgs, msg_actions)):
        if action == 'keep':
            cleaned_msgs.append(msg)
        elif action == 'strip_a':
            strip_a += 1
        elif action == 'strip_b':
            strip_b += 1
        elif action == 'strip_c':
            strip_c += 1
    
    total_stripped = strip_a + strip_b + strip_c
    if total_stripped > 0:
        body['messages'] = cleaned_msgs
        logger.info(f"[DEDUP] 清理 {total_stripped} 条残留 "
                    f"(A.规则={strip_a}, B.记忆摘要={strip_b}, C.Operit总结={strip_c}, "
                    f"消息: {pre_dedup_count} → {len(cleaned_msgs)}, "
                    f"字符: {pre_dedup_chars} → {sum(_msg_chars(m) for m in cleaned_msgs)})")
    
    # 日志：注入前的原始大小
    post_dedup_chars = sum(_msg_chars(m) for m in body.get('messages', []))
    logger.info(f"[SIZE] 注入前: {len(body.get('messages', []))} 条消息, {post_dedup_chars} 字符 "
                f"(去重前 {pre_dedup_count} 条, {pre_dedup_chars} 字符)")

    # === Bug 0.5 修复：彻底清理 tool 调用链（仅QQ来源） ===
    # Operit端自带完整tool_use+tool_result配对，清理会导致模型复读工具调用
    # 只对QQ来源（skip_injection=true）执行清理
    _is_qq_source = request.headers.get('x-skip-injection', '').lower() == 'true'
    
    if _is_qq_source:
        msgs = body.get('messages', [])
        cleaned = []
        tool_strip_count = 0
        for msg in msgs:
            if msg.get('role') == 'tool':
                tool_strip_count += 1
                continue
            if msg.get('role') == 'assistant':
                content = msg.get('content', '')
                if isinstance(content, list):
                    text_blocks = [b for b in content if isinstance(b, dict) and b.get('type') == 'text']
                    non_tool_blocks = [b for b in content if not isinstance(b, dict) or b.get('type') != 'tool_use']
                    if len(non_tool_blocks) != len(content):
                        if text_blocks:
                            msg = dict(msg)
                            msg['content'] = '\n'.join(b.get('text', '') for b in text_blocks)
                        else:
                            msg = dict(msg)
                            msg['content'] = ''
            cleaned.append(msg)
        if tool_strip_count > 0:
            body['messages'] = cleaned
            logger.info(f"[TOOL_CLEANUP] (QQ来源) 清理 {tool_strip_count} 条 tool 消息及 tool_use blocks")
    else:
        logger.info(f"[TOOL_CLEANUP] (Operit来源) 保留完整tool链，不清理")

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

    # 检查是否需要跳过注入引擎
    skip_injection = request.headers.get('x-skip-injection', '').lower() == 'true'
    
    # 技术模式 或 QQ非蕊蕊来源(已自带QQ prompt) 跳过注入引擎
    if tech_mode:
        logger.info(f"[TECH_MODE] 技术模式启用，跳过注入引擎，保留原始 {len(body.get('messages', []))} 条消息")
    elif skip_injection:
        logger.info(f"[SKIP_INJECTION] 请求来源已自带提示词 (source={explicit_source})，跳过注入引擎")
    else:
        injection_engine = InjectionEngine()
        body['messages'] = await injection_engine.inject(body.get('messages', []), {'model': model, 'conversation_id': conversation_id})

    # 打印最终发送给 API 的 context
    final_msg_count = len(body['messages'])
    final_msg_chars = sum(_msg_chars(m) for m in body['messages'])
    logger.info(f"[API_REQUEST] 最终发送至上游 API: 包含 {final_msg_count} 条消息, 约 {final_msg_chars} 字符")

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
    used_cooled_down = False  # 是否已经尝试过冷却期到期的���应商

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
            response = await proxy_chat_completion(body, provider, conversation_id=conversation_id, skip_tool_cleanup=(not _is_qq), skip_context_trim=tech_mode)

            # 成功，确保���记为��康
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
    base_url: str = ''
    api_key: str = ''
    provider_id: int = 0

@app.post("/admin/api/providers/fetch-models")
async def admin_fetch_models(req: FetchModelsRequest):
    """代���拉取上游模型列表"""
    import httpx
    try:
        api_key = req.api_key
        base_url = req.base_url.rstrip('/') if req.base_url else ''
        
        # 如果没传 api_key 但传了 provider_id，从数据库取原始 key
        if not api_key and req.provider_id:
            db = await get_db()
            try:
                cursor = await db.execute('SELECT api_key, api_base_url FROM providers WHERE id = ?', (req.provider_id,))
                row = await cursor.fetchone()
                if row:
                    api_key = row['api_key']
                    if not base_url:
                        base_url = row['api_base_url'].rstrip('/')
            finally:
                await db.close()
        
        if not api_key:
            raise HTTPException(status_code=400, detail="API Key 为空，请先填写并保存后再拉取")
        
        url = f"{base_url}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
            models = [m.get('id') for m in data.get('data', []) if m.get('id')]
            return {"models": models}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"获取模型����表���败: {str(e)}")


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
    try:
        data = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"JSON解析失败: {str(e)}")
    db = await get_db()
    try:
        fields, values = [], []
        for k in ['name', 'content', 'position', 'role', 'priority', 'depth', 'match_condition', 'is_enabled']:
            if k in data:
                fields.append(f"{k} = ?")
                values.append(data[k])
        if fields:
            values.append(rule_id)
            await db.execute(f"UPDATE injection_rules SET {', '.join(fields)}, updated_at = datetime('now', '+8 hours') WHERE id = ?", values)
            await db.commit()
        return {"message": "规则更新成功"}
    except Exception as e:
        logger.error(f"规则更新失败 (rule_id={rule_id}): {e}")
        raise HTTPException(status_code=500, detail=f"规则更新失败: {str(e)}")
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
            return JSONResponse({'error': '窗���不存在'}, status_code=404)
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

# ==================== 状态便签 API ====================

@app.get("/admin/api/status")
async def admin_get_status():
    """获取所有状态便签"""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM memories WHERE category = 'status' ORDER BY id DESC")
        rows = [dict(r) for r in await cursor.fetchall()]
        return rows
    finally:
        await db.close()

@app.post("/admin/api/status")
async def admin_create_status(request: Request):
    """新增状态便签"""
    body = await request.json()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO memories (content, category, threshold_hours, aliases, updated_at) VALUES (?, 'status', ?, ?, NULL)",
            (body.get('content'), body.get('threshold_hours', 24), body.get('aliases', ''))
        )
        await db.commit()
        return {'success': True}
    finally:
        await db.close()

@app.put("/admin/api/status/{status_id}")
async def admin_update_status(status_id: int, request: Request):
    """修改状态便签（比如手动设置已完成时间）"""
    body = await request.json()
    db = await get_db()
    try:
        fields = []
        params = []
        for k in ['content', 'threshold_hours', 'aliases', 'updated_at']:
            if k in body:
                fields.append(f"{k} = ?")
                params.append(body[k])
        if fields:
            params.append(status_id)
            await db.execute(f"UPDATE memories SET {', '.join(fields)} WHERE id = ?", params)
            await db.commit()
        return {'success': True}
    finally:
        await db.close()

@app.delete("/admin/api/status/{status_id}")
async def admin_delete_status(status_id: int):
    """删除状态便签"""
    db = await get_db()
    try:
        await db.execute("DELETE FROM memories WHERE id = ?", (status_id,))
        await db.commit()
        return {'success': True}
    finally:
        await db.close()


@app.post("/admin/api/sync-memories")
async def admin_sync_memories(request: Request):
    """从Operit记忆库批量导入记忆到Gateway memories表（带embedding生成）"""
    from embedding import get_embedding
    body = await request.json()
    items = body.get('memories', [])
    if not items:
        return {'success': False, 'error': 'no memories provided'}
    
    db = await get_db()
    imported = 0
    skipped = 0
    try:
        for item in items:
            title = item.get('title', '')
            content = item.get('content', '')
            tags = item.get('tags', '')
            source = item.get('source', 'operit_sync')
            
            # 用 title + content 的前200字符做去重检查
            check_text = f"{title}: {content[:200]}"
            cursor = await db.execute(
                "SELECT id FROM memories WHERE category = 'operit' AND content LIKE ?",
                (f"%{title[:50]}%",)
            )
            existing = await cursor.fetchone()
            if existing:
                skipped += 1
                continue
            
            # 组合存储内容
            full_content = f"[{title}] {content}"
            if tags:
                full_content += f" (标签: {tags})"
            
            # 生成embedding
            emb = await get_embedding(full_content[:500])  # 取前500字做embedding
            emb_str = json.dumps(emb) if emb else None
            
            await db.execute(
                "INSERT INTO memories (content, category, embedding, created_at) VALUES (?, 'operit', ?, datetime('now', '+8 hours'))",
                (full_content, emb_str)
            )
            imported += 1
        
        await db.commit()
        return {'success': True, 'imported': imported, 'skipped': skipped, 'total': len(items)}
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

# ==================== QQ Bot 配置 API ====================
@app.get("/admin/api/qq-config")
async def get_qq_config():
    """获取QQ Bot配置"""
    from qq_config import config as qq_config
    qq_config.reload()
    return {
        'BOT_QQ': qq_config.BOT_QQ,
        'RUIRUI_QQ': qq_config.RUIRUI_QQ,
        'END_EMOJI': qq_config.END_EMOJI,
        'SILENCE_TIMEOUT': qq_config.SILENCE_TIMEOUT,
        'GROUP_KEYWORDS': ','.join(qq_config.GROUP_KEYWORDS),
        'GROUP_BUFFER_SIZE': qq_config.GROUP_BUFFER_SIZE,
        'GROUP_BUFFER_TIME': qq_config.GROUP_BUFFER_TIME,
        'SESSION_MAX_TURNS': qq_config.SESSION_MAX_TURNS,
        'REPLY_DELAY_MIN': qq_config.REPLY_DELAY_MIN,
        'REPLY_DELAY_MAX': qq_config.REPLY_DELAY_MAX,
        'DEFAULT_PROMPT': qq_config.DEFAULT_PROMPT,
        'RUIRUI_PROMPT': qq_config.RUIRUI_PROMPT,
        'RUIRUI_QQ_PROMPT': qq_config.RUIRUI_QQ_PROMPT,
        'DEFAULT_MODEL': qq_config.DEFAULT_MODEL
    }

@app.post("/admin/api/qq-config")
async def save_qq_config(request: Request):
    """保存QQ Bot配置"""
    data = await request.json()
    from qq_config import config as qq_config
    if 'BOT_QQ' in data: qq_config.BOT_QQ = int(data['BOT_QQ'])
    if 'RUIRUI_QQ' in data: qq_config.RUIRUI_QQ = int(data['RUIRUI_QQ'])
    if 'END_EMOJI' in data: qq_config.END_EMOJI = data['END_EMOJI']
    if 'SILENCE_TIMEOUT' in data: qq_config.SILENCE_TIMEOUT = int(data['SILENCE_TIMEOUT'])
    if 'GROUP_KEYWORDS' in data: qq_config.GROUP_KEYWORDS = data['GROUP_KEYWORDS'].split(',')
    if 'GROUP_BUFFER_SIZE' in data: qq_config.GROUP_BUFFER_SIZE = int(data['GROUP_BUFFER_SIZE'])
    if 'GROUP_BUFFER_TIME' in data: qq_config.GROUP_BUFFER_TIME = int(data['GROUP_BUFFER_TIME'])
    if 'SESSION_MAX_TURNS' in data: qq_config.SESSION_MAX_TURNS = int(data['SESSION_MAX_TURNS'])
    if 'REPLY_DELAY_MIN' in data: qq_config.REPLY_DELAY_MIN = float(data['REPLY_DELAY_MIN'])
    if 'REPLY_DELAY_MAX' in data: qq_config.REPLY_DELAY_MAX = float(data['REPLY_DELAY_MAX'])
    if 'DEFAULT_PROMPT' in data: qq_config.DEFAULT_PROMPT = data['DEFAULT_PROMPT']
    if 'RUIRUI_PROMPT' in data: qq_config.RUIRUI_PROMPT = data['RUIRUI_PROMPT']
    if 'RUIRUI_QQ_PROMPT' in data: qq_config.RUIRUI_QQ_PROMPT = data['RUIRUI_QQ_PROMPT']
    if 'DEFAULT_MODEL' in data: qq_config.DEFAULT_MODEL = data['DEFAULT_MODEL']
    qq_config.save()
    return {"ok": True}

# ==================== 技术模式 API ====================
@app.get("/admin/api/tech-mode")
async def get_tech_mode():
    """获取技术模式状态"""
    global tech_mode
    return {"enabled": tech_mode}

@app.post("/admin/api/tech-mode")
async def set_tech_mode(request: Request):
    """切换技术模式"""
    global tech_mode
    data = await request.json()
    new_state = bool(data.get('enabled', False))
    old_state = tech_mode
    tech_mode = new_state
    logger.info(f"[TECH_MODE] 技术模式切换: {old_state} → {new_state}")
    
    # 从技术模式切回正常模式时，自动触发一次轮总，压缩技术模式期间积累的消息
    if old_state and not new_state:
        logger.info("[TECH_MODE] 切回正常模式，自动触发一次轮总压缩积累的消息")
        async def _bg_catchup_summary():
            try:
                summarizer = get_summarizer()
                result = await summarizer.generate_round_summary()
                logger.info(f"[TECH_MODE] 切回后轮总完成 ({len(result) if result else 0} 字符)")
                # 重置计数��
                db = await get_db()
                try:
                    await db.execute(
                        "INSERT INTO config (key, value, description) VALUES ('_msg_counter', '0', '轮总触发计数器') "
                        "ON CONFLICT(key) DO UPDATE SET value = '0'"
                    )
                    await db.commit()
                    logger.info("[TECH_MODE] 计数器已重置为 0")
                finally:
                    await db.close()
            except Exception as e:
                logger.error(f"[TECH_MODE] 切回后轮总失败: {e}")
        asyncio.create_task(_bg_catchup_summary())
    
    return {"ok": True, "enabled": tech_mode}

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

@app.get("/admin/test-recall")
async def test_recall_page():
    """返回语义记忆召回测试页面"""
    html_path = os.path.join(os.path.dirname(__file__), 'static', 'test_recall.html')
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type='text/html')
    return HTMLResponse("<h1>页面文件未找到</h1>", status_code=404)

@app.post("/admin/api/test-recall")
async def api_test_recall(request: Request):
    """测试记忆召回"""
    body = await request.json()
    text = body.get('text', '')
    if not text:
        return {'success': False, 'error': '文本不能为空'}
    
    try:
        from embedding import get_embedding, cosine_similarity
        user_emb = await get_embedding(text)
        if not user_emb:
            return {'success': False, 'error': 'Embedding API 调用失败或未配置 Key'}
        
        db = await get_db()
        try:
            cursor = await db.execute("SELECT id, content, embedding FROM memories WHERE embedding IS NOT NULL")
            memories_rows = await cursor.fetchall()
            
            scored = []
            for row in memories_rows:
                try:
                    mem_emb = json.loads(row['embedding'])
                    sim = cosine_similarity(user_emb, mem_emb)
                    if sim > 0.5:
                        content_trunc = row['content'][:100] + ('...' if len(row['content']) > 100 else '')
                        scored.append({'sim': f"{sim:.2f}", 'content': content_trunc, 'raw_sim': sim})
                except Exception:
                    pass
            
            scored.sort(key=lambda x: x['raw_sim'], reverse=True)
            top_5 = scored[:5]
            
            return {'success': True, 'results': top_5}
        finally:
            await db.close()
            
    except Exception as e:
        return {'success': False, 'error': str(e)}


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