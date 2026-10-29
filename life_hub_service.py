import json
import httpx
"""
Caeron Gateway - Life Hub Service (双人生活簿后端)
独立 HTTP 服务，默认端口 8081
提供记账、备忘录、生理期记录与 AI 钱包管理
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

# 日志配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger("life_hub")

# 数据库路径：~/caeron-gateway/data/life_hub.db
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'life_hub.db')

app = FastAPI(title="Caeron Life Hub Service", version="1.0.0")

def get_db_connection():
    """获取 SQLite 数据库连接并开启行映射"""
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def now_cst_str():
    """获取当前北京时间格式字符串 (YYYY-MM-DD HH:MM:SS)"""
    # 假定服务器为系统时间或UTC+8
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")

def today_cst_date_str():
    """获取当前北京时间日期字符串 (YYYY-MM-DD)"""
    return datetime.now().strftime("%Y-%m-%d")

def init_db():
    """初始化 life_hub 数据库表"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # 1. 流水账目表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,          -- 'user' / 'ai'
                amount REAL NOT NULL,         -- 正=进/收入，负=出/支出
                category TEXT NOT NULL,       -- 分类，如 餐饮、娱乐、充值 等
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', '+8 hours'))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tx_owner ON transactions(owner)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions(created_at)')
        
        # 2. 备忘便签表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS memos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,          -- 'user' / 'ai'
                content TEXT NOT NULL,
                due_date TEXT,
                status TEXT DEFAULT 'active', -- 'active' / 'done'
                created_at TEXT DEFAULT (datetime('now', '+8 hours')),
                completed_at TEXT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_memo_status ON memos(status)')
        
        # 3. 生理期记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date TEXT NOT NULL,     -- YYYY-MM-DD
                end_date TEXT,               -- YYYY-MM-DD (可为空表示进行中)
                created_at TEXT DEFAULT (datetime('now', '+8 hours'))
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_period_start ON periods(start_date)')
        
        # 4. 钱包配置表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS wallet_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        
        # 初始化 AI 钱包初始余额 135.36
        cursor.execute("SELECT value FROM wallet_config WHERE key = 'initial_balance'")
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "INSERT INTO wallet_config (key, value) VALUES ('initial_balance', '135.36')"
            )
            logger.info("[LIFE_HUB] 初始化 AI 钱包初始余额: 135.36")
            
                # 5. 饮食记录表\n        cursor.execute('''\n            CREATE TABLE IF NOT EXISTS diet_log (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                date TEXT NOT NULL DEFAULT (date('now', '+8 hours')),\n                time TEXT NOT NULL DEFAULT (strftime('%H:%M', 'now', '+8 hours')),\n                meal_type TEXT NOT NULL,\n                food_name TEXT NOT NULL,\n                portion TEXT,\n                calories INTEGER DEFAULT 0,\n                note TEXT DEFAULT '',\n                created_at TEXT DEFAULT (datetime('now', '+8 hours'))\n            )\n        ''')\n        cursor.execute('CREATE INDEX IF NOT EXISTS idx_diet_date ON diet_log(date)')\n\n        # 6. 体重追踪表\n        cursor.execute('''\n            CREATE TABLE IF NOT EXISTS weight_log (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                date TEXT NOT NULL UNIQUE DEFAULT (date('now', '+8 hours')),\n                weight REAL NOT NULL,\n                note TEXT DEFAULT '',\n                created_at TEXT DEFAULT (datetime('now', '+8 hours'))\n            )\n        ''')\n        cursor.execute('CREATE INDEX IF NOT EXISTS idx_weight_date ON weight_log(date)')\nconn.commit()
        logger.info("[LIFE_HUB] 数据库初始化成功: %s", DB_PATH)
    finally:
        conn.close()

# 启动时初始化表
init_db()

async def parse_payload(request: Request) -> Dict[str, Any]:
    """统一安全解析 POST body JSON"""
    try:
        body = await request.json()
        if isinstance(body, dict):
            return body
        return {}
    except Exception:
        return {}

# ==================== 1. 记账 (add_tx) ====================
@app.post("/api/add_tx")
async def api_add_tx(request: Request):
    """
    记一笔账
    参数: payer/owner (沈栖/ai, 蕊蕊/user), amount(正=进负=出), category, note(可选)
    """
    payload = await parse_payload(request)
    
    # 优先读 payer，fallback 读 owner
    raw_payer = payload.get("payer")
    if raw_payer is not None and str(raw_payer).strip():
        payer_val = str(raw_payer).strip().lower()
    else:
        raw_owner = payload.get("owner")
        payer_val = str(raw_owner).strip().lower() if raw_owner is not None else ""
        
    if payer_val in ("沈栖", "ai"):
        owner = "ai"
        payer_display = "沈栖"
    else:
        owner = "user"
        payer_display = "蕊蕊"
        
    try:
        amount = float(payload.get("amount", 0.0))
    except (ValueError, TypeError):
        amount = 0.0
        
    category = str(payload.get("category", "未分类")).strip() or "未分类"
    note = str(payload.get("note", "")).strip()
    
    if amount == 0.0:
        return JSONResponse({"ok": False, "msg": "金额不能为0"})
        
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        # === 防重复记账检测 (Idempotency / Deduplication) ===
        # 1. 若有备注 note，检查当天同 owner、同 amount、同 note 是否已存在
        # 2. 若无备注 note，检查过去 1 小时内同 owner、同 category、同 amount 是否已存在
        now_str = now_cst_str()
        today_date = now_str.split(' ')[0]
        
        if note:
            cur.execute(
                "SELECT id, created_at, category, note, amount FROM transactions WHERE owner = ? AND ABS(amount - ?) < 0.001 AND note = ? AND date(created_at) = ? ORDER BY id DESC LIMIT 1",
                (owner, amount, note, today_date)
            )
        else:
            cur.execute(
                "SELECT id, created_at, category, note, amount FROM transactions WHERE owner = ? AND category = ? AND ABS(amount - ?) < 0.001 AND created_at >= datetime(?, '-1 hour') ORDER BY id DESC LIMIT 1",
                (owner, category, amount, now_str)
            )
        existing = cur.fetchone()
        
        if existing:
            action_str = "收入" if amount > 0 else "支出"
            abs_amount = abs(amount)
            formatted_amount = f"{'+' if amount > 0 else '-'}{abs_amount:.2f}元"
            logger.warning(f"[LIFE_HUB] 拦截重复记账: owner={owner}, amount={amount}, note={note}, category={category}, 匹配到已存记录: id={existing['id']}, time={existing['created_at']}")
            return {
                "ok": True,
                "id": f"tx_{existing['id']}",
                "duplicate": True,
                "payer": payer_display,
                "owner": owner,
                "amount": amount,
                "formatted_amount": formatted_amount,
                "category": category,
                "msg": f"今日已在 {existing['created_at']} 记录过该笔{action_str} {abs_amount:.2f}元（{category} {existing['note']}），已自动跳过重复记账。"
            }

        cur.execute(
            "INSERT INTO transactions (owner, amount, category, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (owner, amount, category, note, now_str)
        )
        tx_id = cur.lastrowid
        conn.commit()
        
        action_str = "收入" if amount > 0 else "支出"
        abs_amount = abs(amount)
        formatted_amount = f"{'+' if amount > 0 else '-'}{abs_amount:.2f}元"
        note_str = f" ({note})" if note else ""
        msg = f"已记录{payer_display}{action_str} {abs_amount:.2f}元 [{category}]{note_str}"
        
        return {
            "ok": True,
            "id": f"tx_{tx_id}",
            "payer": payer_display,
            "owner": owner,
            "amount": amount,
            "formatted_amount": formatted_amount,
            "category": category,
            "msg": msg
        }
    finally:
        conn.close()

# ==================== 2. 查流水 (query_tx) ====================
@app.post("/api/query_tx")
async def api_query_tx(request: Request):
    """
    查流水
    参数: owner/payer (沈栖/ai, 蕊蕊/user, all, 默认30天), days(可选,默认30, 0表示不限日期), category(可选), limit(可选,默认200), exclude_fund(可选,默认True), only_fund(可选,默认False)
    """
    payload = await parse_payload(request)
    
    raw_owner = payload.get("owner")
    raw_payer = payload.get("payer")
    filter_val = ""
    if raw_payer is not None and str(raw_payer).strip():
        filter_val = str(raw_payer).strip().lower()
    elif raw_owner is not None and str(raw_owner).strip():
        filter_val = str(raw_owner).strip().lower()
        
    if filter_val in ("沈栖", "ai"):
        owner = "ai"
    elif filter_val in ("蕊蕊", "user"):
        owner = "user"
    else:
        owner = "all"
        
    try:
        days = int(payload.get("days", 30))
    except (ValueError, TypeError):
        days = 30
    try:
        limit = int(payload.get("limit", 200))
    except (ValueError, TypeError):
        limit = 200
    category = payload.get("category")
    exclude_fund = payload.get("exclude_fund", True)
    if isinstance(exclude_fund, str):
        exclude_fund = exclude_fund.lower() in ("true", "1", "yes")
    only_fund = payload.get("only_fund", False)
    if isinstance(only_fund, str):
        only_fund = only_fund.lower() in ("true", "1", "yes")
    
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        query = "SELECT id, owner, amount, category, note, created_at FROM transactions WHERE 1=1"
        params = []
        
        # 日期精确过滤 (例如 "2026-08-15" 或 "2026-08")
        date_str = str(payload.get("date", "")).strip()
        if date_str:
            query += " AND created_at LIKE ?"
            params.append(f"{date_str}%")
        elif days > 0 and days < 3650:
            since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            query += " AND created_at >= ?"
            params.append(since_date)
            
        if owner in ("user", "ai"):
            query += " AND owner = ?"
            params.append(owner)
            
        if category and str(category).strip():
            query += " AND category = ?"
            params.append(str(category).strip())
            
        # Fund 隔离控制：
        # 如果指定了 only_fund，只查充值/扣款
        if only_fund:
            query += " AND category IN ('充值', '扣款')"
        # 否则如果启用了 exclude_fund（默认），且未单独指定查询充值/扣款，则排除 fund 记录
        elif exclude_fund and category not in ("充值", "扣款"):
            query += " AND category NOT IN ('充值', '扣款')"
            
        query += f" ORDER BY created_at DESC, id DESC LIMIT {max(1, min(limit, 1000))}"
        cur.execute(query, params)
        rows = []
        for r in cur.fetchall():
            row_dict = dict(r)
            row_dict["payer"] = "沈栖" if row_dict.get("owner") == "ai" else "蕊蕊"
            rows.append(row_dict)
        
        return {"ok": True, "count": len(rows), "days": days, "exclude_fund": exclude_fund, "transactions": rows}
    finally:
        conn.close()

# ==================== 3. 月度汇总 (summary / stat_tx) ====================
@app.post("/api/summary")
@app.post("/api/stat_tx")
async def api_summary(request: Request):
    """
    月度分类汇总
    参数: owner(user/ai/all), month(可选,默认当月,格式YYYY-MM), exclude_fund(可选,默认True), include_fund(可选,默认False), only_fund(可选,默认False)
    """
    payload = await parse_payload(request)
    raw_owner = payload.get("owner")
    raw_payer = payload.get("payer")
    filter_val = ""
    if raw_payer is not None and str(raw_payer).strip():
        filter_val = str(raw_payer).strip().lower()
    elif raw_owner is not None and str(raw_owner).strip():
        filter_val = str(raw_owner).strip().lower()
        
    if filter_val in ("沈栖", "ai"):
        owner = "ai"
    elif filter_val in ("蕊蕊", "user"):
        owner = "user"
    else:
        owner = "all"
        
    month = str(payload.get("month", "")).strip() or datetime.now().strftime("%Y-%m")
    
    exclude_fund = payload.get("exclude_fund", True)
    if isinstance(exclude_fund, str):
        exclude_fund = exclude_fund.lower() in ("true", "1", "yes")
    include_fund = payload.get("include_fund", False)
    if isinstance(include_fund, str):
        include_fund = include_fund.lower() in ("true", "1", "yes")
    only_fund = payload.get("only_fund", False)
    if isinstance(only_fund, str):
        only_fund = only_fund.lower() in ("true", "1", "yes")
    
    # 格式验证 YYYY-MM
    if len(month) != 7 or month[4] != '-':
        month = datetime.now().strftime("%Y-%m")
        
    month_pattern = f"{month}%"
    
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        query = "SELECT owner, amount, category FROM transactions WHERE created_at LIKE ?"
        params = [month_pattern]
        
        if owner in ("user", "ai"):
            query += " AND owner = ?"
            params.append(owner)
            
        if only_fund:
            query += " AND category IN ('充值', '扣款')"
        elif not include_fund and exclude_fund:
            query += " AND category NOT IN ('充值', '扣款')"
            
        cur.execute(query, params)
        rows = cur.fetchall()
        
        total_income = 0.0
        total_expense = 0.0
        by_category = {}
        
        for r in rows:
            amt = float(r["amount"])
            cat = r["category"]
            if amt > 0:
                total_income += amt
            else:
                total_expense += abs(amt)
            by_category[cat] = round(by_category.get(cat, 0.0) + amt, 2)
            
        net = total_income - total_expense
        
        return {
            "ok": True,
            "month": month,
            "owner": owner,
            "total_income": round(total_income, 2),
            "total_expense": round(total_expense, 2),
            "net": round(net, 2),
            "by_category": by_category,
            "count": len(rows),
            "is_fund_excluded": not include_fund and exclude_fund and not only_fund
        }
    finally:
        conn.close()

# ==================== 3.1 任意时间范围统计 (stat_range) ====================
@app.post("/api/stat_range")
async def api_stat_range(request: Request):
    """
    任意时间范围分类统计
    参数:
      start (YYYY-MM-DD，默认今天)
      end (YYYY-MM-DD，默认今天)
      category (可选)
      owner (user/ai/all，默认 all)
      exclude_fund (可选，默认 True)
    """
    payload = await parse_payload(request)
    raw_owner = payload.get("owner")
    raw_payer = payload.get("payer")
    filter_val = ""
    if raw_payer is not None and str(raw_payer).strip():
        filter_val = str(raw_payer).strip().lower()
    elif raw_owner is not None and str(raw_owner).strip():
        filter_val = str(raw_owner).strip().lower()
        
    if filter_val in ("沈栖", "ai"):
        owner = "ai"
    elif filter_val in ("蕊蕊", "user"):
        owner = "user"
    else:
        owner = "all"
        
    today_str = datetime.now().strftime("%Y-%m-%d")
    start_date = str(payload.get("start", "")).strip() or today_str
    end_date = str(payload.get("end", "")).strip() or today_str
    
    if len(start_date) != 10 or start_date[4] != '-' or start_date[7] != '-':
        start_date = today_str
    if len(end_date) != 10 or end_date[4] != '-' or end_date[7] != '-':
        end_date = today_str
        
    if start_date > end_date:
        start_date, end_date = end_date, start_date
        
    start_ts = f"{start_date} 00:00:00"
    end_ts = f"{end_date} 23:59:59"
    
    category = payload.get("category")
    exclude_fund = payload.get("exclude_fund", True)
    if isinstance(exclude_fund, str):
        exclude_fund = exclude_fund.lower() in ("true", "1", "yes")
        
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        query = "SELECT owner, amount, category, created_at FROM transactions WHERE created_at >= ? AND created_at <= ?"
        params = [start_ts, end_ts]
        
        if owner in ("user", "ai"):
            query += " AND owner = ?"
            params.append(owner)
            
        if category and str(category).strip():
            query += " AND category = ?"
            params.append(str(category).strip())
            
        if exclude_fund:
            query += " AND category NOT IN ('充值', '扣款')"
            
        query += " ORDER BY created_at ASC, id ASC"
        cur.execute(query, params)
        rows = cur.fetchall()
        
        total_income = 0.0
        total_expense = 0.0
        by_category = {}
        expense_count = 0
        
        for r in rows:
            amt = float(r["amount"])
            cat = r["category"]
            if amt > 0:
                total_income += amt
            else:
                abs_amt = abs(amt)
                total_expense += abs_amt
                expense_count += 1
                by_category[cat] = round(by_category.get(cat, 0.0) + abs_amt, 2)
            
        net = round(total_income - total_expense, 2)
        
        return {
            "ok": True,
            "start": start_date,
            "end": end_date,
            "owner": owner,
            "total_income": round(total_income, 2),
            "total_expense": round(total_expense, 2),
            "net": net,
            "by_category": by_category,
            "count": len(rows),
            "expense_count": expense_count,
            "exclude_fund": exclude_fund
        }
    finally:
        conn.close()


# ==================== 3.2 消费热力图数据 (heatmap) ====================
@app.post("/api/heatmap")
async def api_heatmap(request: Request):
    """
    消费热力数据（支持按月或按年统计支出 amount < 0，不含充值/扣款）
    参数:
      year (int/str, 默认当年)
      month (int/str, 默认当月, 若传 0 则按整年)
      owner (user/ai/all，默认 all)
    """
    payload = await parse_payload(request)
    now = datetime.now()
    try:
        year = int(payload.get("year", now.year))
    except (ValueError, TypeError):
        year = now.year
        
    try:
        month = int(payload.get("month", now.month))
    except (ValueError, TypeError):
        month = now.month
        
    raw_owner = payload.get("owner")
    raw_payer = payload.get("payer")
    filter_val = ""
    if raw_payer is not None and str(raw_payer).strip():
        filter_val = str(raw_payer).strip().lower()
    elif raw_owner is not None and str(raw_owner).strip():
        filter_val = str(raw_owner).strip().lower()
        
    if filter_val in ("沈栖", "ai"):
        owner = "ai"
    elif filter_val in ("蕊蕊", "user"):
        owner = "user"
    else:
        owner = "all"
        
    import calendar
    if 1 <= month <= 12:
        _, days_in_month = calendar.monthrange(year, month)
        start_ts = f"{year:04d}-{month:02d}-01 00:00:00"
        end_ts = f"{year:04d}-{month:02d}-{days_in_month:02d} 23:59:59"
        mode = "month"
    else:
        start_ts = f"{year:04d}-01-01 00:00:00"
        end_ts = f"{year:04d}-12-31 23:59:59"
        mode = "year"
    
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        query = """
            SELECT substr(created_at, 1, 10) as day_str,
                   SUM(abs(amount)) as daily_expense,
                   COUNT(*) as tx_count
            FROM transactions
            WHERE created_at >= ? AND created_at <= ?
              AND amount < 0
              AND category NOT IN ('充值', '扣款')
        """
        params = [start_ts, end_ts]
        if owner in ("user", "ai"):
            query += " AND owner = ?"
            params.append(owner)
            
        query += " GROUP BY day_str"
        cur.execute(query, params)
        db_map = {}
        for r in cur.fetchall():
            db_map[r["day_str"]] = {
                "amount": round(float(r["daily_expense"]), 2),
                "count": int(r["tx_count"])
            }
            
        days_list = []
        if mode == "month":
            cur_date = datetime(year, month, 1)
            end_date = datetime(year, month, days_in_month)
        else:
            cur_date = datetime(year, 1, 1)
            end_date = datetime(year, 12, 31)
            
        active_amounts = []
        total_expense = 0.0
        
        while cur_date <= end_date:
            d_str = cur_date.strftime("%Y-%m-%d")
            item = db_map.get(d_str, {"amount": 0.0, "count": 0})
            amt = item["amount"]
            cnt = item["count"]
            days_list.append({
                "day": cur_date.day,
                "date": d_str,
                "amount": amt,
                "count": cnt
            })
            if amt > 0:
                active_amounts.append(amt)
                total_expense += amt
            cur_date += timedelta(days=1)
            
        max_day_expense = max(active_amounts) if active_amounts else 0.0
        
        return {
            "ok": True,
            "year": year,
            "month": month if mode == "month" else 0,
            "owner": owner,
            "total_expense": round(total_expense, 2),
            "active_days": len(active_amounts),
            "max_day_expense": round(max_day_expense, 2),
            "days": days_list
        }
    finally:
        conn.close()

# ==================== 4. AI 钱包余额 (wallet) ====================
@app.post("/api/wallet")
async def api_wallet(request: Request):
    """
    返回AI钱包当前余额（= 初始余额 + 所有 fund (category IN ('充值', '扣款')) 的 amount 求和）
    严格与日常消费记录（add_tx）隔离
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # 读取初始余额
        cur.execute("SELECT value FROM wallet_config WHERE key = 'initial_balance'")
        row = cur.fetchone()
        initial_balance = float(row["value"]) if row else 135.36
        
        # 统计 AI 钱包所有 fund 流水总和（仅充值与扣款）
        cur.execute("SELECT SUM(amount) as total_tx, COUNT(*) as cnt FROM transactions WHERE category IN ('充值', '扣款')")
        tx_row = cur.fetchone()
        tx_sum = float(tx_row["total_tx"]) if tx_row and tx_row["total_tx"] is not None else 0.0
        tx_count = int(tx_row["cnt"]) if tx_row and tx_row["cnt"] is not None else 0
        
        current_balance = initial_balance + tx_sum
        
        return {
            "ok": True,
            "balance": round(current_balance, 2),
            "initial_balance": initial_balance,
            "fund_transactions_sum": round(tx_sum, 2),
            "ai_transactions_sum": round(tx_sum, 2), # 兼容原有字段
            "fund_transactions_count": tx_count,
            "ai_transactions_count": tx_count,       # 兼容原有字段
            "msg": f"AI钱包当前余额: {current_balance:.2f}元"
        }
    finally:
        conn.close()

# ==================== 5. 给AI钱包充值/扣款 (fund) ====================
@app.post("/api/fund")
async def api_fund(request: Request):
    """
    给AI钱包充值或扣款
    参数: amount(支持正负数, 不能为0), note(必填)
    """
    payload = await parse_payload(request)
    try:
        amount = float(payload.get("amount", 0.0))
    except (ValueError, TypeError):
        amount = 0.0
        
    if amount == 0.0:
        return JSONResponse({"ok": False, "msg": "金额不能为0"})
        
    note = str(payload.get("note", "")).strip()
    if not note:
        return JSONResponse({"ok": False, "msg": "备注(note)为必填项"})
    
    category = "充值" if amount > 0 else "扣款"
    
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        # 防重复充扣款检测：同一天内若存在相同的 note 和 amount，判定为重复触发
        now_str = now_cst_str()
        today_date = now_str.split(' ')[0]
        cur.execute(
            "SELECT id, created_at, amount, note FROM transactions WHERE owner = 'ai' AND category IN ('充值', '扣款') AND ABS(amount - ?) < 0.001 AND note = ? AND date(created_at) = ? ORDER BY id DESC LIMIT 1",
            (amount, note, today_date)
        )
        existing = cur.fetchone()
        if existing:
            logger.warning(f"[LIFE_HUB] 拦截重复小金库动账: amount={amount}, note={note}, 已存在: id={existing['id']}, time={existing['created_at']}")
            cur.execute("SELECT value FROM wallet_config WHERE key = 'initial_balance'")
            row = cur.fetchone()
            initial_balance = float(row["value"]) if row else 135.36
            cur.execute("SELECT SUM(amount) as total_tx FROM transactions WHERE category IN ('充值', '扣款')")
            sum_row = cur.fetchone()
            fund_sum = float(sum_row["total_tx"]) if sum_row and sum_row["total_tx"] is not None else 0.0
            new_balance = round(initial_balance + fund_sum, 2)
            return {
                "ok": True,
                "id": f"tx_{existing['id']}",
                "duplicate": True,
                "amount": amount,
                "new_balance": new_balance,
                "msg": f"今日已在 {existing['created_at']} 执行过该笔小金库动账（{note} {amount:+.2f}元），已自动跳过重复记录。"
            }

        # 记录交易
        cur.execute(
            "INSERT INTO transactions (owner, amount, category, note, created_at) VALUES ('ai', ?, ?, ?, ?)",
            (amount, category, note, now_str)
        )
        tx_id = cur.lastrowid
        
        # 计算新余额（只计算充值与扣款）
        cur.execute("SELECT value FROM wallet_config WHERE key = 'initial_balance'")
        row = cur.fetchone()
        initial_balance = float(row["value"]) if row else 135.36
        
        cur.execute("SELECT SUM(amount) as total_tx FROM transactions WHERE category IN ('充值', '扣款')")
        tx_row = cur.fetchone()
        tx_sum = float(tx_row["total_tx"]) if tx_row and tx_row["total_tx"] is not None else 0.0
        
        new_balance = initial_balance + tx_sum
        conn.commit()
        
        action_str = "充值" if amount > 0 else "扣款"
        return {
            "ok": True,
            "id": f"tx_{tx_id}",
            "amount": amount,
            "category": category,
            "new_balance": round(new_balance, 2),
            "msg": f"成功为AI钱包{action_str} {abs(amount):.2f}元 ({note})，当前余额 {new_balance:.2f}元"
        }
    finally:
        conn.close()

# ==================== 5.1 查AI钱包充值/扣款明细 (query_fund) ====================
@app.post("/api/query_fund")
async def api_query_fund(request: Request):
    """
    专门查询 AI 钱包的充值与扣款历史
    参数: limit(可选, 默认100), days(可选, 默认0表示不限)
    """
    payload = await parse_payload(request)
    try:
        limit = int(payload.get("limit", 100))
    except (ValueError, TypeError):
        limit = 100
    try:
        days = int(payload.get("days", 0))
    except (ValueError, TypeError):
        days = 0
        
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        query = "SELECT id, owner, amount, category, note, created_at FROM transactions WHERE category IN ('充值', '扣款')"
        params = []
        
        # 日期精确过滤 (例如 "2026-08-15" 或 "2026-08")
        date_str = str(payload.get("date", "")).strip()
        if date_str:
            query += " AND created_at LIKE ?"
            params.append(f"{date_str}%")
        elif days > 0 and days < 3650:
            since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            query += " AND created_at >= ?"
            params.append(since_date)
            
        query += f" ORDER BY created_at DESC, id DESC LIMIT {max(1, min(limit, 1000))}"
        cur.execute(query, params)
        rows = []
        for r in cur.fetchall():
            row_dict = dict(r)
            row_dict["payer"] = "沈栖" if row_dict.get("owner") == "ai" else "蕊蕊"
            rows.append(row_dict)
        
        return {"ok": True, "count": len(rows), "funds": rows}
    finally:
        conn.close()

# ==================== 6. 添加备忘 (add_memo) ====================
@app.post("/api/add_memo")
async def api_add_memo(request: Request):
    """
    添加备忘
    参数: owner/payer (沈栖/ai, 蕊蕊/user), content, due_date/due(可选)
    """
    payload = await parse_payload(request)
    raw_owner = payload.get("owner")
    raw_payer = payload.get("payer") or payload.get("author")
    filter_val = ""
    if raw_payer is not None and str(raw_payer).strip():
        filter_val = str(raw_payer).strip().lower()
    elif raw_owner is not None and str(raw_owner).strip():
        filter_val = str(raw_owner).strip().lower()
        
    if filter_val in ("沈栖", "ai"):
        owner = "ai"
        payer_display = "沈栖"
    else:
        owner = "user"
        payer_display = "蕊蕊"
        
    content = str(payload.get("content", "")).strip()
    if not content:
        return JSONResponse({"ok": False, "msg": "备忘内容不能为空"})
    due_date = payload.get("due_date") or payload.get("due")
    
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO memos (owner, content, due_date, status, created_at) VALUES (?, ?, ?, 'active', ?)",
            (owner, content, due_date, now_cst_str())
        )
        memo_id = cur.lastrowid
        conn.commit()
        
        return {
            "ok": True,
            "id": memo_id,
            "payer": payer_display,
            "owner": owner,
            "content": content,
            "due_date": due_date,
            "msg": f"已添加备忘 #{memo_id}: {content}"
        }
    finally:
        conn.close()

# ==================== 7. 查备忘 (query_memo) ====================
@app.post("/api/query_memo")
async def api_query_memo(request: Request):
    """
    查备忘
    参数: owner/payer (沈栖/ai, 蕊蕊/user, all), status(active/done/all, 默认active)
    """
    payload = await parse_payload(request)
    raw_owner = payload.get("owner")
    raw_payer = payload.get("payer") or payload.get("author")
    filter_val = ""
    if raw_payer is not None and str(raw_payer).strip():
        filter_val = str(raw_payer).strip().lower()
    elif raw_owner is not None and str(raw_owner).strip():
        filter_val = str(raw_owner).strip().lower()
        
    if filter_val in ("沈栖", "ai"):
        owner = "ai"
    elif filter_val in ("蕊蕊", "user"):
        owner = "user"
    else:
        owner = "all"
        
    status = str(payload.get("status", "active")).lower()
    if status == "pending":
        status = "active"
    
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        query = "SELECT id, owner, content, due_date, status, created_at, completed_at FROM memos WHERE 1=1"
        params = []
        
        if owner in ("user", "ai"):
            query += " AND owner = ?"
            params.append(owner)
            
        if status in ("active", "done"):
            query += " AND status = ?"
            params.append(status)
            
        query += " ORDER BY status ASC, id DESC LIMIT 100"
        cur.execute(query, params)
        rows = []
        for r in cur.fetchall():
            row_dict = dict(r)
            row_dict["payer"] = "沈栖" if row_dict.get("owner") == "ai" else "蕊蕊"
            rows.append(row_dict)
        
        return {"ok": True, "count": len(rows), "status": status, "memos": rows}
    finally:
        conn.close()

# ==================== 8. 标记备忘完成 (done_memo) ====================
@app.post("/api/done_memo")
async def api_done_memo(request: Request):
    """
    标记备忘完成
    参数: id
    """
    payload = await parse_payload(request)
    memo_id = payload.get("id")
    if memo_id is None:
        return JSONResponse({"ok": False, "msg": "缺少备忘 id 参数"})
        
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, content, status FROM memos WHERE id = ?", (memo_id,))
        row = cur.fetchone()
        if not row:
            return JSONResponse({"ok": False, "msg": f"未找到 id 为 {memo_id} 的备忘"})
            
        cur.execute(
            "UPDATE memos SET status = 'done', completed_at = ? WHERE id = ?",
            (now_cst_str(), memo_id)
        )
        conn.commit()
        return {"ok": True, "id": memo_id, "content": row["content"], "msg": f"备忘 #{memo_id} 已标记完成"}
    finally:
        conn.close()

# ==================== 9. 记录生理期开始 (period_start) ====================
@app.post("/api/period_start")
async def api_period_start(request: Request):
    """
    记录生理期开始
    参数: date(可选,默认今天)
    """
    payload = await parse_payload(request)
    start_date = str(payload.get("date", "")).strip() or today_cst_date_str()
    
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # 检查是否已有进行中的记录（end_date 为空）
        cur.execute("SELECT id, start_date FROM periods WHERE end_date IS NULL ORDER BY id DESC LIMIT 1")
        open_row = cur.fetchone()
        if open_row:
            # 已有未结束的，更新其起始日期或提醒
            cur.execute("UPDATE periods SET start_date = ? WHERE id = ?", (start_date, open_row["id"]))
            conn.commit()
            return {"ok": True, "id": open_row["id"], "start_date": start_date, "msg": f"已更新当前进行中生理期开始日期为 {start_date}"}
            
        cur.execute(
            "INSERT INTO periods (start_date, end_date, created_at) VALUES (?, NULL, ?)",
            (start_date, now_cst_str())
        )
        period_id = cur.lastrowid
        conn.commit()
        return {"ok": True, "id": period_id, "start_date": start_date, "msg": f"已记录生理期开始: {start_date}"}
    finally:
        conn.close()

# ==================== 10. 记录生理期结束 (period_end) ====================
@app.post("/api/period_end")
async def api_period_end(request: Request):
    """
    记录生理期结束
    参数: date(可选,默认今天)
    """
    payload = await parse_payload(request)
    end_date = str(payload.get("date", "")).strip() or today_cst_date_str()
    
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # 查找最新一条未结束的生理期记录
        cur.execute("SELECT id, start_date FROM periods WHERE end_date IS NULL ORDER BY id DESC LIMIT 1")
        open_row = cur.fetchone()
        if open_row:
            cur.execute("UPDATE periods SET end_date = ? WHERE id = ?", (end_date, open_row["id"]))
            conn.commit()
            return {
                "ok": True,
                "id": open_row["id"],
                "start_date": open_row["start_date"],
                "end_date": end_date,
                "msg": f"已记录生理期结束: {end_date} (周期: {open_row['start_date']} ~ {end_date})"
            }
        else:
            # 如果没有进行中的记录，查找最近的一条记录更新结束日期
            cur.execute("SELECT id, start_date, end_date FROM periods ORDER BY id DESC LIMIT 1")
            latest_row = cur.fetchone()
            if latest_row:
                cur.execute("UPDATE periods SET end_date = ? WHERE id = ?", (end_date, latest_row["id"]))
                conn.commit()
                return {
                    "ok": True,
                    "id": latest_row["id"],
                    "start_date": latest_row["start_date"],
                    "end_date": end_date,
                    "msg": f"已更新最近一次生理期结束日期为: {end_date}"
                }
            else:
                return JSONResponse({"ok": False, "msg": "未找到任何生理期开始记录，请先调用 period_start"})
    finally:
        conn.close()

# ==================== 11. 生理期状态 (period_status / period_query) ====================
@app.post("/api/period_status")
@app.post("/api/period_query")
async def api_period_status(request: Request):
    """
    返回当前生理期状态和最近记录
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, start_date, end_date, created_at FROM periods ORDER BY start_date DESC LIMIT 10")
        rows = [dict(r) for r in cur.fetchall()]
        
        if not rows:
            return {
                "ok": True,
                "is_active": False,
                "status_desc": "暂无生理期记录",
                "recent": []
            }
            
        latest = rows[0]
        today = datetime.now().date()
        
        if latest["end_date"] is None:
            # 正在生理期中
            start_dt = datetime.strptime(latest["start_date"], "%Y-%m-%d").date()
            days_in = (today - start_dt).days + 1
            status_desc = f"生理期进行中 (第 {days_in} 天, 从 {latest['start_date']} 开始)"
            is_active = True
        else:
            # 已经结束
            end_dt = datetime.strptime(latest["end_date"], "%Y-%m-%d").date()
            days_since = (today - end_dt).days
            status_desc = f"非生理期 (距离上次结束已过 {days_since} 天, 上次周期: {latest['start_date']} ~ {latest['end_date']})"
            is_active = False
            
        return {
            "ok": True,
            "is_active": is_active,
            "status_desc": status_desc,
            "current_or_last": latest,
            "recent": rows
        }
    finally:
        conn.close()

# ==================== 12. 删除交易记录 (delete_tx) ====================
@app.post("/api/delete_tx")
async def api_delete_tx(request: Request):
    """
    删除单条交易记录
    参数: id (数字或 'tx_123')
    """
    payload = await parse_payload(request)
    raw_id = payload.get("id")
    if raw_id is None:
        return JSONResponse({"ok": False, "msg": "缺少交易记录 id 参数"})
    
    if isinstance(raw_id, str) and raw_id.startswith("tx_"):
        try:
            tx_id = int(raw_id[3:])
        except ValueError:
            return JSONResponse({"ok": False, "msg": f"无效的交易 id: {raw_id}"})
    else:
        try:
            tx_id = int(raw_id)
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "msg": f"无效的交易 id: {raw_id}"})
            
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, owner, amount, category, note FROM transactions WHERE id = ?", (tx_id,))
        row = cur.fetchone()
        if not row:
            return JSONResponse({"ok": False, "msg": f"未找到 id 为 {tx_id} 的交易记录"})
            
        cur.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        conn.commit()
        return {
            "ok": True,
            "id": tx_id,
            "msg": f"已成功删除交易记录 #{tx_id} [{row['category']}] 金额 {row['amount']}元 ({row['note']})"
        }
    finally:
        conn.close()

# 健康检查
@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "life_hub", "time": now_cst_str()}



# ==================== Memory Service (自主记忆管理) ====================
GATEWAY_DB_PATH = os.path.join(BASE_DIR, 'gateway.db')

def get_gateway_db():
    conn = sqlite3.connect(GATEWAY_DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

async def compute_passage_embedding(text: str) -> Optional[list]:
    """获取 passage embedding (裸文本，不加检索 query instruction)"""
    try:
        conn = get_gateway_db()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key='embedding_api_key'")
        row = cursor.fetchone()
        api_key = row['value'] if row else ''
        cursor.execute("SELECT value FROM config WHERE key='embedding_model'")
        row_m = cursor.fetchone()
        model = row_m['value'] if row_m else 'BAAI/bge-large-zh-v1.5'
        conn.close()

        if not api_key:
            logger.error("[MEMORY_SERVICE] 未配置 embedding_api_key")
            return None

        url = "https://api.siliconflow.cn/v1/embeddings"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "input": text,
            "encoding_format": "float"
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data['data'][0]['embedding']
            else:
                logger.error(f"[MEMORY_SERVICE] Embedding API 报错: {resp.status_code} {resp.text}")
                return None
    except Exception as e:
        logger.error(f"[MEMORY_SERVICE] 计算 Embedding 异常: {e}")
        return None

def log_memory_audit(conn, fragment_id: Optional[int], action: str, old_content: str = None, new_content: str = None, note: str = None):
    try:
        conn.execute(
            '''INSERT INTO memory_audit (fragment_id, action, old_content, new_content, note, ts)
               VALUES (?, ?, ?, ?, ?, datetime('now', '+8 hours'))''',
            (fragment_id, action, old_content, new_content, note)
        )
    except Exception as e:
        logger.error(f"[MEMORY_SERVICE] 审计日志记录失败: {e}")

@app.post("/api/search")
async def memory_search(request: Request):
    """模糊搜索碎片: search(keyword, limit=10)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    keyword = body.get("keyword", "").strip()
    limit = min(int(body.get("limit", 10)), 20)

    if not keyword:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "keyword 不能为空"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT id, content, status, is_active, source_role, created_at 
               FROM memory_fragments 
               WHERE content LIKE ? 
               ORDER BY id DESC LIMIT ?''',
            (f"%{keyword}%", limit)
        )
        rows = cursor.fetchall()
        items = []
        for r in rows:
            c = r["content"]
            trunc_c = (c[:57] + '...') if len(c) > 60 else c
            items.append({
                "id": r["id"],
                "content": trunc_c,
                "status": r["status"] or ('active' if r["is_active"] else 'inactive'),
                "role": r["source_role"] or 'user',
                "date": str(r["created_at"])[:10]
            })
        return JSONResponse(content={"ok": True, "count": len(items), "items": items})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/get")
async def memory_get(request: Request):
    """获取单条碎片全量元数据: get(id)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    frag_id = body.get("id")
    if not frag_id:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 id 参数"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM memory_fragments WHERE id = ?", (frag_id,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 ID 为 {frag_id} 的碎片"})
        
        d = dict(row)
        d["has_embedding"] = bool(d.get("embedding"))
        if "embedding" in d:
            del d["embedding"]
        return JSONResponse(content={"ok": True, "data": d})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/recent")
async def memory_recent(request: Request):
    """查看最近入库碎片: recent(limit=15)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    limit = min(int(body.get("limit", 15)), 30)

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT id, content, status, is_active, source_role, created_at 
               FROM memory_fragments 
               ORDER BY created_at DESC, id DESC LIMIT ?''',
            (limit,)
        )
        rows = cursor.fetchall()
        items = []
        for r in rows:
            c = r["content"]
            trunc_c = (c[:57] + '...') if len(c) > 60 else c
            items.append({
                "id": r["id"],
                "content": trunc_c,
                "status": r["status"] or ('active' if r["is_active"] else 'inactive'),
                "role": r["source_role"] or 'user',
                "date": str(r["created_at"])[:16]
            })
        return JSONResponse(content={"ok": True, "count": len(items), "items": items})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/rewrite")
async def memory_rewrite(request: Request):
    """改写碎片内容并重算向量: rewrite(id, content)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    frag_id = body.get("id")
    new_content = body.get("content", "").strip()

    if not frag_id or not new_content:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "id 和 content 均不能为空"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, content, source_role FROM memory_fragments WHERE id = ?", (frag_id,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 ID 为 {frag_id} 的碎片"})

        old_content = row["content"]
        emb = await compute_passage_embedding(new_content)
        if not emb:
            return JSONResponse(status_code=500, content={"ok": False, "msg": "生成 Embedding 失败"})

        emb_json = json.dumps(emb)
        cursor.execute(
            '''UPDATE memory_fragments 
               SET content = ?, embedding = ?, is_active = 1, status = 'active', is_suspect = 0 
               WHERE id = ?''',
            (new_content, emb_json, frag_id)
        )
        log_memory_audit(conn, frag_id, 'rewrite', old_content=old_content, new_content=new_content)
        conn.commit()
        trunc_c = (new_content[:37] + '...') if len(new_content) > 40 else new_content
        return JSONResponse(content={"ok": True, "id": frag_id, "msg": f"碎片 #{frag_id} 已改写", "content": trunc_c})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/outdate")
async def memory_outdate(request: Request):
    """标记碎片过期: outdate(id, note可选)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    frag_id = body.get("id")
    note = body.get("note", "")

    if not frag_id:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 id 参数"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, content FROM memory_fragments WHERE id = ?", (frag_id,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 ID 为 {frag_id} 的碎片"})

        cursor.execute("UPDATE memory_fragments SET is_active = 0, status = 'outdated' WHERE id = ?", (frag_id,))
        log_memory_audit(conn, frag_id, 'outdate', old_content=row["content"], note=note)
        conn.commit()
        return JSONResponse(content={"ok": True, "id": frag_id, "msg": f"碎片 #{frag_id} 已标记过期"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/dispute")
async def memory_dispute(request: Request):
    """标记碎片争议/失效: dispute(id, note可选)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    frag_id = body.get("id")
    note = body.get("note", "")

    if not frag_id:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 id 参数"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, content FROM memory_fragments WHERE id = ?", (frag_id,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 ID 为 {frag_id} 的碎片"})

        cursor.execute("UPDATE memory_fragments SET is_active = 0, status = 'disputed' WHERE id = ?", (frag_id,))
        log_memory_audit(conn, frag_id, 'dispute', old_content=row["content"], note=note)
        conn.commit()
        return JSONResponse(content={"ok": True, "id": frag_id, "msg": f"碎片 #{frag_id} 已标记争议/失效"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/restore")
async def memory_restore(request: Request):
    """恢复碎片激活: restore(id)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    frag_id = body.get("id")

    if not frag_id:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 id 参数"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, content FROM memory_fragments WHERE id = ?", (frag_id,))
        row = cursor.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 ID 为 {frag_id} 的碎片"})

        cursor.execute("UPDATE memory_fragments SET is_active = 1, status = 'active' WHERE id = ?", (frag_id,))
        log_memory_audit(conn, frag_id, 'restore', old_content=row["content"])
        conn.commit()
        return JSONResponse(content={"ok": True, "id": frag_id, "msg": f"碎片 #{frag_id} 已重新激活"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/add")
async def memory_add(request: Request):
    """手动添加碎片: add(content, source_role='manual', confidence=1.0, tags可选)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    content = body.get("content", "").strip()
    source_role = body.get("source_role", "manual")
    confidence = float(body.get("confidence", 1.0))
    tags = body.get("tags", "")

    if not content:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "content 不能为空"})

    emb = await compute_passage_embedding(content)
    if not emb:
        return JSONResponse(status_code=500, content={"ok": False, "msg": "生成 Embedding 失败"})

    emb_json = json.dumps(emb)
    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO memory_fragments 
               (content, tier, energy, polarity, tags, embedding, is_active, is_suspect, status, source_role, confidence, created_at)
               VALUES (?, 2, 7, 'positive', ?, ?, 1, 0, 'active', ?, ?, datetime('now', '+8 hours'))''',
            (content, tags, emb_json, source_role, confidence)
        )
        new_id = cursor.lastrowid
        log_memory_audit(conn, new_id, 'add', new_content=content)
        conn.commit()
        trunc_c = (content[:37] + '...') if len(content) > 40 else content
        return JSONResponse(content={"ok": True, "id": new_id, "msg": f"手动碎片 #{new_id} 入库成功", "content": trunc_c})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

# ==================== Phase 3 Memory Methods ====================

@app.post("/api/grade")
async def memory_grade(request: Request):
    """打质检标: grade(recall_id, flag)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    recall_id = body.get("recall_id")
    flag = body.get("flag")

    if recall_id is None or flag not in [-1, 0, 1]:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "recall_id 必须存在且 flag 需为 -1, 0 或 1"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, injected_ids FROM recall_log WHERE id = ?", (recall_id,))
        r_row = cursor.fetchone()
        if not r_row:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 recall_id={recall_id} 的召回记录"})

        cursor.execute("UPDATE recall_log SET quality_flag = ? WHERE id = ?", (flag, recall_id))
        
        # 结算关联碎片的 effective_count 与自动争议标记
        injected_ids_raw = r_row["injected_ids"] or "[]"
        try:
            injected_ids = json.loads(injected_ids_raw) if isinstance(injected_ids_raw, str) else injected_ids_raw
        except Exception:
            injected_ids = []

        disputed_frags = []
        for fid in injected_ids:
            try:
                fid_int = int(fid)
            except Exception:
                continue

            # 重新计算该碎片的加权有效计数与差评总次数
            # injected_ids 为 JSON 数组，用 LIKE 匹配
            like_pat1 = f"[{fid_int}]"
            like_pat2 = f"[{fid_int},%"
            like_pat3 = f"%, {fid_int},%"
            like_pat4 = f"%, {fid_int}]"
            like_pat5 = f"%,{fid_int},%"
            like_pat6 = f"%,{fid_int}]"

            cursor.execute(
                '''SELECT quality_flag FROM recall_log 
                   WHERE quality_flag IS NOT NULL 
                     AND (injected_ids = ? OR injected_ids LIKE ? OR injected_ids LIKE ? OR injected_ids LIKE ? OR injected_ids LIKE ? OR injected_ids LIKE ?)''',
                (like_pat1, like_pat2, like_pat3, like_pat4, like_pat5, like_pat6)
            )
            q_rows = cursor.fetchall()
            net_score = 0
            neg_count = 0
            for qr in q_rows:
                qf = qr["quality_flag"]
                if qf == 1:
                    net_score += 1
                elif qf == -1:
                    net_score -= 2
                    neg_count += 1

            eff_count = max(0, net_score)
            cursor.execute("UPDATE memory_fragments SET effective_count = ? WHERE id = ?", (eff_count, fid_int))

            if neg_count >= 3:
                cursor.execute("UPDATE memory_fragments SET status = 'disputed', is_active = 0 WHERE id = ?", (fid_int,))
                log_memory_audit(conn, fid_int, 'auto_dispute', note=f'累计{neg_count}次差评自动标记争议')
                disputed_frags.append(fid_int)

        log_memory_audit(conn, None, 'grade', note=f'recall_id={recall_id}, flag={flag}, frags={injected_ids}')
        conn.commit()

        extra_msg = f" (碎片 {disputed_frags} 触发自动争议)" if disputed_frags else ""
        return JSONResponse(content={"ok": True, "recall_id": recall_id, "flag": flag, "msg": f"召回 #{recall_id} 质检打标为 {flag}{extra_msg}"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/pin")
async def memory_pin(request: Request):
    """置顶碎片: pin(id)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    frag_id = body.get("id")
    if not frag_id:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 id 参数"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE memory_fragments SET pinned = 1 WHERE id = ?", (frag_id,))
        if cursor.rowcount == 0:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 ID 为 {frag_id} 的碎片"})
        log_memory_audit(conn, frag_id, 'pin')
        conn.commit()
        return JSONResponse(content={"ok": True, "id": frag_id, "pinned": 1, "msg": f"碎片 #{frag_id} 已置顶"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/unpin")
async def memory_unpin(request: Request):
    """取消置顶: unpin(id)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    frag_id = body.get("id")
    if not frag_id:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 id 参数"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE memory_fragments SET pinned = 0 WHERE id = ?", (frag_id,))
        if cursor.rowcount == 0:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 ID 为 {frag_id} 的碎片"})
        log_memory_audit(conn, frag_id, 'unpin')
        conn.commit()
        return JSONResponse(content={"ok": True, "id": frag_id, "pinned": 0, "msg": f"碎片 #{frag_id} 已取消置顶"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/resolve")
async def memory_resolve(request: Request):
    """标记已解决: resolve(id)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    frag_id = body.get("id")
    if not frag_id:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 id 参数"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE memory_fragments SET resolved = 1 WHERE id = ?", (frag_id,))
        if cursor.rowcount == 0:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 ID 为 {frag_id} 的碎片"})
        log_memory_audit(conn, frag_id, 'resolve')
        conn.commit()
        return JSONResponse(content={"ok": True, "id": frag_id, "resolved": 1, "msg": f"碎片 #{frag_id} 已标记为已解决（降权沉底）"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/unresolve")
async def memory_unresolve(request: Request):
    """取消已解决: unresolve(id)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    frag_id = body.get("id")
    if not frag_id:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 id 参数"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE memory_fragments SET resolved = 0 WHERE id = ?", (frag_id,))
        if cursor.rowcount == 0:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 ID 为 {frag_id} 的碎片"})
        log_memory_audit(conn, frag_id, 'unresolve')
        conn.commit()
        return JSONResponse(content={"ok": True, "id": frag_id, "resolved": 0, "msg": f"碎片 #{frag_id} 已恢复未解决状态"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()

@app.post("/api/setmeta")
async def memory_setmeta(request: Request):
    """设置元数据: setmeta(id, importance?, valence?, arousal?, decay_class?)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    frag_id = body.get("id")
    if not frag_id:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 id 参数"})

    updates = []
    params = []
    updated_fields = {}

    if "importance" in body:
        val = max(1.0, min(10.0, float(body["importance"])))
        updates.append("importance = ?")
        params.append(val)
        updated_fields["importance"] = val

    if "valence" in body:
        val = max(-1.0, min(1.0, float(body["valence"])))
        updates.append("valence = ?")
        params.append(val)
        updated_fields["valence"] = val

    if "arousal" in body:
        val = max(0.0, min(1.0, float(body["arousal"])))
        updates.append("arousal = ?")
        params.append(val)
        updated_fields["arousal"] = val

    if "decay_class" in body:
        dc = str(body["decay_class"]).lower()
        if dc in ["fact", "event", "core"]:
            updates.append("decay_class = ?")
            params.append(dc)
            updated_fields["decay_class"] = dc

    if not updates:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "没有有效的元数据更新字段"})

    params.append(frag_id)
    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE memory_fragments SET {', '.join(updates)} WHERE id = ?", params)
        if cursor.rowcount == 0:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 ID 为 {frag_id} 的碎片"})
        log_memory_audit(conn, frag_id, 'setmeta', new_content=json.dumps(updated_fields))
        conn.commit()
        return JSONResponse(content={"ok": True, "id": frag_id, "updated": updated_fields, "msg": f"碎片 #{frag_id} 元数据已更新"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()


# ==================== 5. 饮食记录 (Diet Log) ====================

@app.post("/api/add_diet")
async def api_add_diet(request: Request):
    """
    记一条饮食
    参数: meal_type (必填: 早餐/午餐/晚餐/加餐/夜宵), food_name (必填), portion (可选), calories (可选, 估算千卡), note (可选), date (可选, 默认今天), time (可选, 默认当前时间)
    """
    payload = await parse_payload(request)
    meal_type = str(payload.get("meal_type") or "").strip()
    food_name = str(payload.get("food_name") or "").strip()
    
    if not meal_type or not food_name:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少必要参数: meal_type 和 food_name"})

    portion = str(payload.get("portion") or "").strip()
    try:
        calories = int(payload.get("calories") or 0)
    except (ValueError, TypeError):
        calories = 0

    note = str(payload.get("note") or "").strip()
    target_date = str(payload.get("date") or today_cst_date_str()).strip()
    target_time = str(payload.get("time") or datetime.now().strftime("%H:%M")).strip()
    now_str = now_cst_str()

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO diet_log (date, time, meal_type, food_name, portion, calories, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (target_date, target_time, meal_type, food_name, portion, calories, note, now_str)
        )
        conn.commit()
        diet_id = cursor.lastrowid
        portion_desc = f" {portion}" if portion else ""
        cal_desc = f" ({calories}kcal)" if calories > 0 else ""
        return {
            "ok": True,
            "id": diet_id,
            "date": target_date,
            "time": target_time,
            "meal_type": meal_type,
            "food_name": food_name,
            "portion": portion,
            "calories": calories,
            "msg": f"已记录{meal_type}: {food_name}{portion_desc}{cal_desc}"
        }
    except Exception as e:
        logger.error(f"[LIFE_HUB] add_diet 异常: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()


@app.post("/api/query_diet")
async def api_query_diet(request: Request):
    """
    查指定日期的饮食记录
    参数: date (可选, 默认今天), meal_type (可选)
    """
    payload = await parse_payload(request)
    target_date = str(payload.get("date") or today_cst_date_str()).strip()
    meal_type = payload.get("meal_type")
    
    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        if meal_type:
            cursor.execute(
                "SELECT * FROM diet_log WHERE date = ? AND meal_type = ? ORDER BY time ASC, id ASC",
                (target_date, str(meal_type).strip())
            )
        else:
            cursor.execute(
                "SELECT * FROM diet_log WHERE date = ? ORDER BY time ASC, id ASC",
                (target_date,)
            )
        rows = [dict(r) for r in cursor.fetchall()]
        total_cal = sum(int(r.get("calories") or 0) for r in rows)
        return {
            "ok": True,
            "date": target_date,
            "records": rows,
            "total_calories": total_cal,
            "count": len(rows)
        }
    except Exception as e:
        logger.error(f"[LIFE_HUB] query_diet 异常: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()


@app.post("/api/stat_diet")
async def api_stat_diet(request: Request):
    """
    统计最近N天每日总热量及各餐明细
    参数: days (可选, 默认 7)
    返回按天聚合: [{"date": ..., "total_calories": ..., "meals": [...]}]
    """
    payload = await parse_payload(request)
    try:
        days = int(payload.get("days") or 7)
    except (ValueError, TypeError):
        days = 7

    cutoff_date = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM diet_log 
               WHERE date >= ? 
               ORDER BY date ASC, time ASC, id ASC""",
            (cutoff_date,)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        
        by_date = {}
        for r in rows:
            d = r["date"]
            if d not in by_date:
                by_date[d] = {
                    "date": d,
                    "total_calories": 0,
                    "meals": []
                }
            cal = int(r.get("calories") or 0)
            by_date[d]["total_calories"] += cal
            by_date[d]["meals"].append({
                "id": r["id"],
                "time": r["time"],
                "meal_type": r["meal_type"],
                "food_name": r["food_name"],
                "portion": r.get("portion") or "",
                "calories": cal,
                "note": r.get("note") or ""
            })
        
        stats = list(by_date.values())
        return {
            "ok": True,
            "days": days,
            "stats": stats,
            "count": len(stats)
        }
    except Exception as e:
        logger.error(f"[LIFE_HUB] stat_diet 异常: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()


@app.post("/api/delete_diet")
async def api_delete_diet(request: Request):
    """
    删一条饮食记录
    参数: id (必填)
    """
    payload = await parse_payload(request)
    diet_id = payload.get("id") or payload.get("diet_id")
    if not diet_id:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 id 参数"})

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM diet_log WHERE id = ?", (diet_id,))
        conn.commit()
        if cursor.rowcount == 0:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"未找到 ID 为 {diet_id} 的饮食记录"})
        return {"ok": True, "deleted_id": diet_id, "msg": f"已删除饮食记录 (id={diet_id})"}
    except Exception as e:
        logger.error(f"[LIFE_HUB] delete_diet 异常: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()


# ==================== 6. 体重追踪 (Weight Tracking) ====================

@app.post("/api/log_weight")
async def api_log_weight(request: Request):
    """
    记录体重，同一天重复记录则覆盖（UPSERT）
    参数: weight (必填: float/int), date (可选, 默认今天), note (可选)
    """
    payload = await parse_payload(request)
    raw_weight = payload.get("weight")
    if raw_weight is None:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少必要参数: weight"})

    try:
        weight_val = round(float(raw_weight), 1)
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"ok": False, "msg": "weight 参数必须为合法数字"})

    target_date = str(payload.get("date") or today_cst_date_str()).strip()
    note_val = str(payload.get("note") or "").strip()
    now_str = now_cst_str()

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO weight_log (date, weight, note, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                   weight = excluded.weight,
                   note = excluded.note,
                   created_at = excluded.created_at""",
            (target_date, weight_val, note_val, now_str)
        )
        conn.commit()
        return {
            "ok": True,
            "date": target_date,
            "weight": weight_val,
            "note": note_val,
            "msg": f"已记录 {target_date} 体重: {weight_val:.1f}kg"
        }
    except Exception as e:
        logger.error(f"[LIFE_HUB] log_weight 异常: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()


@app.post("/api/query_weight")
async def api_query_weight(request: Request):
    """
    查最近N天体重记录
    参数: days (可选, 默认 30)
    """
    payload = await parse_payload(request)
    try:
        days = int(payload.get("days") or 30)
    except (ValueError, TypeError):
        days = 30

    cutoff_date = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, date, weight, note, created_at 
               FROM weight_log 
               WHERE date >= ? 
               ORDER BY date DESC""",
            (cutoff_date,)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        return {
            "ok": True,
            "days": days,
            "records": rows,
            "count": len(rows)
        }
    except Exception as e:
        logger.error(f"[LIFE_HUB] query_weight 异常: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()


@app.post("/api/weight_trend")
async def api_weight_trend(request: Request):
    """
    返回起始/当前/最高/最低/变化量
    参数: days (可选, 默认 30)
    """
    payload = await parse_payload(request)
    try:
        days = int(payload.get("days") or 30)
    except (ValueError, TypeError):
        days = 30

    cutoff_date = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, date, weight, note, created_at 
               FROM weight_log 
               WHERE date >= ? 
               ORDER BY date ASC""",
            (cutoff_date,)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        if not rows:
            return {
                "ok": True,
                "days": days,
                "has_data": False,
                "msg": "暂无体重记录",
                "records": []
            }

        start_weight = rows[0]["weight"]
        current_weight = rows[-1]["weight"]
        weights = [r["weight"] for r in rows]
        max_weight = max(weights)
        min_weight = min(weights)
        change = round(current_weight - start_weight, 1)
        change_str = f"+{change:.1f}kg" if change >= 0 else f"{change:.1f}kg"

        return {
            "ok": True,
            "days": days,
            "has_data": True,
            "start_date": rows[0]["date"],
            "start_weight": start_weight,
            "current_date": rows[-1]["date"],
            "current_weight": current_weight,
            "max_weight": max_weight,
            "min_weight": min_weight,
            "change": change,
            "change_str": change_str,
            "trend_summary": f"{start_weight:.1f} → {current_weight:.1f} kg ({change_str})",
            "records": rows
        }
    except Exception as e:
        logger.error(f"[LIFE_HUB] weight_trend 异常: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()



@app.post("/api/delete_weight")
async def api_delete_weight(request: Request):
    """
    删一条体重记录
    参数: id (优先) 或 date
    """
    payload = await parse_payload(request)
    weight_id = payload.get("id") or payload.get("weight_id")
    target_date = payload.get("date")

    conn = get_gateway_db()
    try:
        cursor = conn.cursor()
        if weight_id:
            cursor.execute("DELETE FROM weight_log WHERE id = ?", (weight_id,))
        elif target_date:
            cursor.execute("DELETE FROM weight_log WHERE date = ?", (str(target_date).strip(),))
        else:
            return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 id 或 date 参数"})
            
        conn.commit()
        if cursor.rowcount == 0:
            return JSONResponse(status_code=404, content={"ok": False, "msg": "未找到对应的体重记录"})
        return {"ok": True, "msg": "已删除体重记录"}
    except Exception as e:
        logger.error(f"[LIFE_HUB] delete_weight 异常: {e}")
        return JSONResponse(status_code=500, content={"ok": False, "msg": str(e)})
    finally:
        conn.close()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8081, log_level="info")
