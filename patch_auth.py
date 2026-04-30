import re

with open("/home/ubuntu/caeron-gateway/main.py", "r") as f:
    content = f.read()

# 1. Add Depends and BaseHTTPMiddleware imports
old_import = "from fastapi import FastAPI, Request, HTTPException"
new_import = "from fastapi import FastAPI, Request, HTTPException, Depends\nfrom starlette.middleware.base import BaseHTTPMiddleware"
content = content.replace(old_import, new_import, 1)

# 2. Add ADMIN_TOKEN after load_dotenv()
old_env = "# 配置日志格式"
new_env = "# Admin Token\nADMIN_TOKEN = os.getenv(\"ADMIN_TOKEN\", \"\")\n\n# 配置日志格式"
content = content.replace(old_env, new_env, 1)

# 3. Add middleware after app creation, before core routes
old_routes = "# ==================== 核心路由 ===================="
new_routes = """# ==================== Admin 认证中间件 ====================
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

# ==================== 核心路由 ===================="""
content = content.replace(old_routes, new_routes, 1)

with open("/home/ubuntu/caeron-gateway/main.py", "w") as f:
    f.write(content)
print("PATCH OK")
