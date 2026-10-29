import os
import time
import base64
import logging
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, Form, Body, Request, HTTPException
from fastapi.responses import FileResponse

from monitor_db import (
    save_heartbeat,
    save_screenshot_record,
    get_latest_heartbeat,
    get_history_stats,
    get_monitor_command,
    set_monitor_command,
    get_recent_screenshots,
    get_recent_heartbeats
)

logger = logging.getLogger(__name__)

router = APIRouter()

SCREENSHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

@router.post("/heartbeat")
async def receive_heartbeat(request: Request):
    """
    接收手机端上报的心跳数据（每 30 秒一次）
    Payload: {"timestamp": 1234567890, "package": "com.xingin.xhs", "app_name": "小红书", "screen_state": "on/off"}
    响应返回当前控制指令 (Piggyback 指令下发通道)
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
        
    timestamp = body.get("timestamp")
    if not timestamp:
        timestamp = int(time.time())
        
    package = body.get("package", "unknown")
    app_name = body.get("app_name", "未知应用")
    screen_state = body.get("screen_state", "unknown")
    
    # 持久化存储并获取下发指令
    control = save_heartbeat(timestamp, package, app_name, screen_state)
    logger.debug(f"[MONITOR] 心跳已存储: {app_name}({package}) 屏幕: {screen_state} -> 下发指令: {control['command']}")
    
    return {
        "code": 0,
        "message": "ok",
        "command": control["command"],
        "interval": control["interval"]
    }

@router.post("/screenshot")
async def upload_screenshot(
    request: Request,
    file: UploadFile = File(None),
    timestamp: int = Form(None)
):
    """
    接收手机端上传的截屏图片
    优先支持 Multipart Form-Data (file 字段)，同时兼容 JSON Base64 降级
    """
    ts = timestamp or int(time.time())
    now_str = datetime.fromtimestamp(ts if ts < 1e11 else ts / 1000).strftime("%Y%m%d_%H%M%S")
    filename = f"screenshot_{now_str}_{int(time.time()*1000)%1000:03d}.jpg"
    dest_path = os.path.join(SCREENSHOTS_DIR, filename)
    
    # 1. Multipart Form-Data 上传
    if file is not None:
        try:
            content = await file.read()
            with open(dest_path, "wb") as f:
                f.write(content)
            file_size = len(content)
        except Exception as e:
            logger.error(f"[MONITOR] Multipart 截屏保存失败: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to save screenshot: {e}")
    else:
        # 2. JSON Base64 降级上传
        try:
            body = await request.json()
            b64_str = body.get("image_base64") or body.get("image") or ""
            if not b64_str:
                raise HTTPException(status_code=400, detail="No image file or base64 data provided")
                
            if "base64," in b64_str:
                b64_str = b64_str.split("base64,")[1]
                
            raw_bytes = base64.b64decode(b64_str)
            with open(dest_path, "wb") as f:
                f.write(raw_bytes)
            file_size = len(raw_bytes)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[MONITOR] Base64 截屏解析失败: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to decode base64 screenshot: {e}")
            
    # 存入数据库
    record_id = save_screenshot_record(ts, dest_path, file_size)
    logger.info(f"[MONITOR] 截屏已存储: {filename} ({file_size} bytes, id={record_id})")
    
    return {
        "code": 0,
        "message": "ok",
        "id": record_id,
        "file_name": filename,
        "file_path": dest_path,
        "file_size": file_size,
        "timestamp": ts
    }

@router.get("/current")
async def get_current_status():
    """获取最新的一条心跳记录"""
    latest = get_latest_heartbeat()
    if not latest:
        return {"code": 0, "data": None, "message": "No heartbeat data available"}
    return {"code": 0, "data": latest}

@router.get("/history")
async def get_history(minutes: int = 30):
    """获取最近 N 分钟的心跳明细与汇总统计"""
    stats = get_history_stats(minutes)
    return {"code": 0, "data": stats}

@router.get("/command")
async def get_command():
    """查询当前下发给手机端的控制指令"""
    cmd = get_monitor_command()
    return {"code": 0, "data": cmd}

@router.post("/command")
async def set_command(body: dict = Body(...)):
    """设置下发给手机端的控制指令 (如 'START_SCREENSHOT' / 'STOP_SCREENSHOT' / 'IDLE')"""
    command = body.get("command", "IDLE")
    interval = body.get("interval", 30)
    set_monitor_command(command, interval)
    logger.info(f"[MONITOR] 管理员更新控制指令: {command}, interval={interval}s")
    return {"code": 0, "message": "Command updated", "command": command.upper(), "interval": interval}


@router.get("/screenshots")
async def get_screenshots(limit: int = 20):
    """获取最近的截屏记录列表"""
    records = get_recent_screenshots(limit)
    data = []
    for r in records:
        fname = os.path.basename(r["file_path"])
        data.append({
            "id": r["id"],
            "file_name": fname,
            "url": f"/monitor/screenshot_file/{fname}",
            "file_size": r["file_size"],
            "timestamp": r["timestamp"],
            "created_at": r["created_at"]
        })
    return {"code": 0, "data": data}

@router.get("/screenshot_file/{filename}")
async def serve_screenshot_file(filename: str):
    """安全返回截屏静态图片文件"""
    clean_name = os.path.basename(filename)
    target = os.path.join(SCREENSHOTS_DIR, clean_name)
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail="Screenshot file not found")
    return FileResponse(target, media_type="image/jpeg")

@router.get("/logs")
async def get_monitor_logs(limit: int = 50):
    """获取最近心跳日志流水"""
    records = get_recent_heartbeats(limit)
    return {"code": 0, "data": records}
