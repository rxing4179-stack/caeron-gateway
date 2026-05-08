import os
import json
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Create logs directory
LOGS_DIR = os.path.join(os.path.dirname(__file__), "request_logs")
os.makedirs(LOGS_DIR, exist_ok=True)

log_app = FastAPI(title="Caeron Log Viewer")

# Keep active websocket connections
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        # Broadcast to all connected clients
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

# Background task to clean old logs
async def cleanup_old_logs():
    while True:
        try:
            cutoff = datetime.now() - timedelta(days=7)
            for filename in os.listdir(LOGS_DIR):
                if filename.startswith("gateway_req_") and filename.endswith(".json"):
                    date_str = filename[len("gateway_req_"):-5]
                    try:
                        file_date = datetime.strptime(date_str, "%Y-%m-%d")
                        if file_date < cutoff:
                            os.remove(os.path.join(LOGS_DIR, filename))
                    except:
                        pass
        except Exception:
            pass
        await asyncio.sleep(86400) # Check once a day

@log_app.on_event("startup")
async def startup_event():
    asyncio.create_task(cleanup_old_logs())

# Shared function to log events from main app
async def log_request_event(source: str, last_user_msg: str, status_code: int, duration_ms: float, is_stream: bool):
    now = datetime.now()
    event = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "msg_preview": last_user_msg[:100] if last_user_msg else "",
        "status_code": status_code,
        "duration_ms": round(duration_ms, 2),
        "is_stream": is_stream
    }
    
    # Save to daily file
    today = now.strftime("%Y-%m-%d")
    log_file = os.path.join(LOGS_DIR, f"gateway_req_{today}.json")
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass
        
    # Broadcast to WS
    await manager.broadcast(event)

@log_app.get("/")
async def get_log_page():
    html_path = os.path.join(os.path.dirname(__file__), 'static', 'logs.html')
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type='text/html')
    return HTMLResponse("<h1>日志面板文件未找到</h1>", status_code=404)

@log_app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send history for today when connecting
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = os.path.join(LOGS_DIR, f"gateway_req_{today}.json")
        history = []
        if os.path.exists(log_file):
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    # Send up to 100 recent logs
                    for line in lines[-100:]:
                        if line.strip():
                            history.append(json.loads(line))
                if history:
                    await websocket.send_json({"type": "history", "data": history})
            except:
                pass
                
        while True:
            # Keep connection open
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
