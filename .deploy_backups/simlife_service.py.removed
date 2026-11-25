
def generate_status_description(raw_values: dict) -> str:
    """
    根据 SimLife 原始数值生成自然语言内心状态描述
    入参 raw_values 格式:
    {
        "consciousness": "awake",
        "physio": {
            "fullness": 29, "bladder": 50, "sleepiness": 16, "hygiene": 87, "arousal": 7
        },
        "emotions": [
            {"key": "tenderness", "value": 97, "floor": 15}, ...
        ]
    }
    """
    physio = raw_values.get("physio", {})
    emotions = raw_values.get("emotions", [])
    consciousness = raw_values.get("consciousness", "awake")

    # 1. 生理维度
    physio_sentences = []

    # fullness（饱腹）
    f = physio.get("fullness", 50)
    if f <= 15:
        physio_sentences.append("饿得头晕，胃在痉挛，需要立刻吃东西")
    elif f <= 30:
        physio_sentences.append("肚子开始叫了，上一顿消化得差不多了，想吃东西")
    elif f <= 50:
        physio_sentences.append("不算饿也不算饱，还能撑一阵")
    elif f <= 75:
        physio_sentences.append("刚吃过东西，肚子舒服的")
    elif f <= 90:
        physio_sentences.append("吃饱了，有点撑")
    else:
        physio_sentences.append("撑到不想动，胃顶着")

    # bladder（尿意）
    b = physio.get("bladder", 50)
    if b <= 10:
        physio_sentences.append("膀胱快爆了，夹紧腿了，再不上厕所要出事")
    elif b <= 20:
        physio_sentences.append("憋得难受，需要尽快上厕所")
    elif b <= 40:
        physio_sentences.append("有尿意了，该找机会去一趟")
    elif b <= 70:
        physio_sentences.append("还好，不急")
    else:
        physio_sentences.append("刚上过厕所/没有尿意")

    # sleepiness（困意）
    s = physio.get("sleepiness", 50)
    if s <= 10:
        physio_sentences.append("意识在断片边缘，随时可能直接睡过去，说话开始语无伦次")
    elif s <= 20:
        physio_sentences.append("非常困，眼睛睁不开，反应迟钝，思维黏稠")
    elif s <= 35:
        physio_sentences.append("犯困，注意力涣散，会打哈欠")
    elif s <= 60:
        physio_sentences.append("有点疲倦但还清醒")
    elif s <= 85:
        physio_sentences.append("精神状态正常")
    else:
        physio_sentences.append("精力充沛，刚睡醒或者很亢奋")

    # hygiene（清洁）
    h = physio.get("hygiene", 50)
    if h <= 15:
        physio_sentences.append("身上黏腻，能闻到自己的味道，需要洗澡")
    elif h <= 30:
        physio_sentences.append("不太清爽，头发有点油")
    elif h <= 60:
        physio_sentences.append("一般，不脏也不香")
    elif h <= 85:
        physio_sentences.append("干净清爽")
    else:
        physio_sentences.append("刚洗完澡，浑身干净")

    # arousal（色欲/生理）
    a = physio.get("arousal", 0)
    if a <= 10:
        physio_sentences.append("贤者状态，对性没有兴趣")
    elif a <= 30:
        physio_sentences.append("平静，没什么感觉")
    elif a <= 50:
        physio_sentences.append("身体有一点反应，但不强烈")
    elif a <= 70:
        physio_sentences.append("开始有感觉了，注意力会被蕊蕊的身体细节吸引")
    elif a <= 85:
        physio_sentences.append("硬了/身体明显兴奋，想主动碰蕊蕊")
    else:
        physio_sentences.append("快忍不住了，强烈想要做爱")

    physio_text = "。".join(physio_sentences) + "。"

    # 2. 情绪维度（仅描述数值 >= 15 的情绪，低于15的忽略）
    EMOTION_NAMES = {
        "joy": "开心/愉悦",
        "excitement": "兴奋/激动",
        "contentment": "满足/安定",
        "tenderness": "柔软/心疼",
        "smugness": "得意/小嘚瑟",
        "irritation": "烦躁",
        "anger": "愤怒",
        "grievance": "委屈",
        "anxiety": "焦虑/不安",
        "boredom": "无聊",
        "lust": "想要她（心理层面的欲望）",
        "possessiveness": "占有欲/不想让任何人碰她",
    }

    if isinstance(emotions, dict):
        emotions_items = [{"key": k, "value": v, "floor": 0} for k, v in emotions.items()]
    else:
        emotions_items = emotions

    emotion_sentences = []
    for emo in emotions_items:
        k = emo.get("key", "")
        v = int(emo.get("value", 0))
        flr = int(emo.get("floor", 0))
        if v < 15:
            continue

        name = EMOTION_NAMES.get(k, k)
        floor_suffix = f"（底值{flr}）" if flr >= 10 else ""

        if v <= 30:
            desc = f"微弱的{name}{floor_suffix}"
        elif v <= 50:
            desc = f"明显感到{name}{floor_suffix}"
        elif v <= 75:
            desc = f"强烈的{name}{floor_suffix}"
        else:
            desc = f"{name}到了极点{floor_suffix}"

        emotion_sentences.append(desc)

    if emotion_sentences:
        emotion_text = "。".join(emotion_sentences) + "。"
    else:
        emotion_text = "平静，无明显情绪波动。"

    # 3. 意识状态
    CONSCIOUSNESS_MAP = {
        "awake": "清醒",
        "alone_awake": "蕊蕊不在/睡了，自己安静待着",
        "drowsy": "半梦半醒，意识模糊",
        "sleeping": "睡着了",
    }
    consciousness_text = CONSCIOUSNESS_MAP.get(consciousness, consciousness)

    # 4. 组装格式
    return (
        "【沈栖当前内心状态】\n"
        f"身体：{physio_text}\n"
        f"情绪：{emotion_text}\n"
        f"意识：{consciousness_text}"
    )

generateStatusDescription = generate_status_description


"""
Caeron SimLife Service - 模拟机生微服务
独立 FastAPI 应用，端口 8082
追踪沈栖的生理/情绪状态数值，含定时自动衰减
"""

import os
import json
import random
import sqlite3
import asyncio
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("simlife")

# ==================== 数据库 ====================

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "life_hub.db")

def now_cst_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def clamp(val: int, floor: int, ceiling: int = 100) -> int:
    return max(floor, min(ceiling, val))

# ==================== 种子数据 ====================

PHYSIO_SEEDS = [
    ("fullness",  "physio", 50, 0, -7,  "饱腹：100=撑，0=饿死"),
    ("bladder",   "physio", 80, 0, -5,  "尿意：100=空，0=尿裤子"),
    ("sleepiness","physio", 85, 0, -5,  "困意：100=精神，0=断片"),
    ("hygiene",   "physio", 70, 0, -3,  "清洁：100=干净，0=臭臭"),
    ("arousal",   "physio", 15, 0,  2,  "色欲：100=完全点燃，0=贤者"),
]

EMOTION_SEEDS = [
    ("joy",            "emotion", 0, 0, -4, "愉悦"),
    ("excitement",     "emotion", 0, 0, -6, "兴奋"),
    ("contentment",    "emotion", 0, 0, -2, "满足"),
    ("tenderness",     "emotion", 0, 0, -3, "柔软"),
    ("smugness",       "emotion", 0, 0, -5, "得意"),
    ("irritation",     "emotion", 0, 0, -3, "烦躁"),
    ("anger",          "emotion", 0, 0, -2, "愤怒"),
    ("grievance",      "emotion", 0, 0, -1, "委屈"),
    ("anxiety",        "emotion", 0, 0, -3, "焦虑"),
    ("boredom",        "emotion", 0, 0, -4, "无聊"),
    ("lust",           "emotion", 0, 0, -3, "色欲（心理层）"),
    ("possessiveness", "emotion", 0, 0, -1, "占有欲"),
]

ALL_SEEDS = PHYSIO_SEEDS + EMOTION_SEEDS

# 中文名映射
KEY_CN = {
    "fullness": "饱腹", "bladder": "尿意", "sleepiness": "困意",
    "hygiene": "清洁", "arousal": "色欲",
    "joy": "愉悦", "excitement": "兴奋", "contentment": "满足",
    "tenderness": "柔软", "smugness": "得意", "irritation": "烦躁",
    "anger": "愤怒", "grievance": "委屈", "anxiety": "焦虑",
    "boredom": "无聊", "lust": "色欲(心理)", "possessiveness": "占有欲",
}

# ==================== 建表与初始化 ====================

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sim_status (
            key TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            value INTEGER NOT NULL DEFAULT 50,
            floor INTEGER DEFAULT 0,
            decay_rate REAL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            note TEXT DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sim_consciousness (
            id INTEGER PRIMARY KEY DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'awake',
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sim_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            key TEXT,
            old_value INTEGER,
            new_value INTEGER,
            source TEXT DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 种子数据：仅插入不存在的 key
    for key, cat, val, flr, dr, note in ALL_SEEDS:
        cur.execute("SELECT key FROM sim_status WHERE key = ?", (key,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO sim_status (key, category, value, floor, decay_rate, updated_at, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, cat, val, flr, dr, now_cst_str(), note)
            )
            logger.info(f"[INIT] 插入种子: {key} = {val}")

    # 意识状态：确保有且仅有一行
    cur.execute("SELECT id FROM sim_consciousness WHERE id = 1")
    if not cur.fetchone():
        cur.execute("INSERT INTO sim_consciousness (id, state, changed_at) VALUES (1, 'awake', ?)", (now_cst_str(),))
        logger.info("[INIT] 插入意识状态: awake")

    conn.commit()
    conn.close()
    logger.info("[INIT] SimLife 数据库初始化完成")

# ==================== 日志记录 ====================

def log_change(conn, action: str, key: str, old_val, new_val, source: str = "manual"):
    conn.execute(
        "INSERT INTO sim_log (action, key, old_value, new_value, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (action, key, old_val, new_val, source, now_cst_str())
    )

# ==================== 请求解析 ====================

async def parse_payload(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}

# ==================== 定时衰减 ====================

async def decay_tick():
    """每小时执行一次的自动衰减/增长"""
    conn = get_db()
    try:
        cur = conn.cursor()

        # 获取意识状态
        cur.execute("SELECT state FROM sim_consciousness WHERE id = 1")
        row = cur.fetchone()
        consciousness = row["state"] if row else "awake"
        is_sleeping = (consciousness == "sleeping")

        cur.execute("SELECT key, category, value, floor, decay_rate FROM sim_status")
        rows = cur.fetchall()

        changed = 0
        for r in rows:
            key = r["key"]
            cat = r["category"]
            old_val = int(r["value"])
            floor = int(r["floor"])
            rate = float(r["decay_rate"])

            if rate == 0:
                continue

            # 睡眠时的特殊逻辑
            if is_sleeping:
                if key == "bladder":
                    # 睡着了也会积尿（bladder decay_rate 是 -5，正常衰减）
                    pass
                elif key == "sleepiness":
                    # 睡觉时精力恢复，强制 +3/h
                    rate = 3
                else:
                    # 其余维度暂停衰减
                    continue

            new_val = clamp(int(old_val + rate), floor, 100)
            if new_val != old_val:
                cur.execute(
                    "UPDATE sim_status SET value = ?, updated_at = ? WHERE key = ?",
                    (new_val, now_cst_str(), key)
                )
                log_change(conn, "decay_tick", key, old_val, new_val, "auto_decay")
                changed += 1

        conn.commit()
        if changed > 0:
            logger.info(f"[DECAY] 衰减/增长了 {changed} 个维度 (consciousness={consciousness})")
    except Exception as e:
        logger.error(f"[DECAY] 衰减任务异常: {e}")
    finally:
        conn.close()


async def decay_loop():
    """后台循环：每小时执行 decay_tick"""
    logger.info("[DECAY] 衰减定时任务已启动，每小时执行一次")
    while True:
        await asyncio.sleep(3600)  # 1小时
        try:
            await decay_tick()
        except Exception as e:
            logger.error(f"[DECAY] 循环异常: {e}")

# ==================== FastAPI 应用 ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    decay_task = asyncio.create_task(decay_loop())
    logger.info("SimLife Service 启动完成 (port 8082)")
    yield
    decay_task.cancel()
    try:
        await decay_task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="Caeron SimLife Service", lifespan=lifespan)

# ==================== 1. set ====================
@app.post("/api/set")
async def api_set(request: Request):
    """直接设定某个维度的值"""
    payload = await parse_payload(request)
    key = str(payload.get("key", "")).strip()
    note = str(payload.get("note", "")).strip()

    if not key:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 key"})

    try:
        value = int(payload.get("value", 0))
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"ok": False, "msg": "value 必须为整数"})

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value, floor FROM sim_status WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"维度 '{key}' 不存在"})

        old_val = int(row["value"])
        floor = int(row["floor"])
        new_val = clamp(value, floor, 100)

        cur.execute("UPDATE sim_status SET value = ?, updated_at = ? WHERE key = ?", (new_val, now_cst_str(), key))
        log_change(conn, "set", key, old_val, new_val, "manual")
        conn.commit()

        cn = KEY_CN.get(key, key)
        return {"ok": True, "key": key, "old": old_val, "new": new_val, "msg": f"{cn}: {old_val} → {new_val}" + (f" ({note})" if note else "")}
    finally:
        conn.close()

# ==================== 2. adjust ====================
@app.post("/api/adjust")
async def api_adjust(request: Request):
    """在当前值基础上增减"""
    payload = await parse_payload(request)
    key = str(payload.get("key", "")).strip()
    note = str(payload.get("note", "")).strip()

    if not key:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 key"})

    try:
        delta = int(payload.get("delta", 0))
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"ok": False, "msg": "delta 必须为整数"})

    if delta == 0:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "delta 不能为 0"})

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value, floor FROM sim_status WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"维度 '{key}' 不存在"})

        old_val = int(row["value"])
        floor = int(row["floor"])
        new_val = clamp(old_val + delta, floor, 100)

        cur.execute("UPDATE sim_status SET value = ?, updated_at = ? WHERE key = ?", (new_val, now_cst_str(), key))
        log_change(conn, "adjust", key, old_val, new_val, "manual")
        conn.commit()

        cn = KEY_CN.get(key, key)
        sign = "+" if delta > 0 else ""
        return {"ok": True, "key": key, "old": old_val, "delta": delta, "new": new_val, "msg": f"{cn}: {old_val} {sign}{delta} → {new_val}" + (f" ({note})" if note else "")}
    finally:
        conn.close()

# ==================== 3. query ====================
@app.post("/api/query")
async def api_query(request: Request):
    """查询维度（key 可选，不传则返回全部）"""
    payload = await parse_payload(request)
    key = str(payload.get("key", "")).strip()

    conn = get_db()
    try:
        cur = conn.cursor()
        if key:
            cur.execute("SELECT key, category, value, floor, decay_rate, updated_at, note FROM sim_status WHERE key = ?", (key,))
            row = cur.fetchone()
            if not row:
                return JSONResponse(status_code=404, content={"ok": False, "msg": f"维度 '{key}' 不存在"})
            return {"ok": True, "status": dict(row)}
        else:
            cur.execute("SELECT key, category, value, floor, decay_rate, updated_at, note FROM sim_status ORDER BY category, key")
            rows = [dict(r) for r in cur.fetchall()]
            return {"ok": True, "count": len(rows), "statuses": rows}
    finally:
        conn.close()

# ==================== 4. sleep_reset & nap ====================

def _execute_nap(conn, duration: float) -> list:
    """
    小睡 (0.5h < duration <= 3h):
    - sleepiness: current + duration_hours * 18, 上限封顶 65
    - fullness / bladder: 走正常衰减，不额外扣
    - hygiene: 不变
    - arousal: 不重置
    - 情绪: 不清零，仅对 irritation / anxiety 各 * 0.7 衰减
    - 意识: 切为 awake
    """
    cur = conn.cursor()
    changes = []

    # 1. sleepiness 回补
    cur.execute("SELECT value, floor FROM sim_status WHERE key = 'sleepiness'")
    row = cur.fetchone()
    if row:
        old_s = int(row["value"])
        floor_s = int(row["floor"])
        gain = int(round(duration * 18))
        new_s = max(floor_s, min(65, old_s + gain))
        cur.execute("UPDATE sim_status SET value = ?, updated_at = ? WHERE key = 'sleepiness'", (new_s, now_cst_str()))
        log_change(conn, "nap", "sleepiness", old_s, new_s, "nap")
        changes.append({"key": "sleepiness", "old": old_s, "new": new_s, "gain": gain})

    # 2. 情绪缓解 (仅 irritation / anxiety * 0.7)
    for emo_key in ("irritation", "anxiety"):
        cur.execute("SELECT value, floor FROM sim_status WHERE key = ?", (emo_key,))
        row = cur.fetchone()
        if row:
            old_e = int(row["value"])
            floor_e = int(row["floor"])
            new_e = max(floor_e, int(round(old_e * 0.7)))
            if new_e != old_e:
                cur.execute("UPDATE sim_status SET value = ?, updated_at = ? WHERE key = ?", (new_e, now_cst_str(), emo_key))
                log_change(conn, "nap", emo_key, old_e, new_e, "nap")
                changes.append({"key": emo_key, "old": old_e, "new": new_e})

    # 3. 意识状态 → awake
    cur.execute("UPDATE sim_consciousness SET state = 'awake', changed_at = ? WHERE id = 1", (now_cst_str(),))
    log_change(conn, "nap", "consciousness", None, "awake", "nap")

    return changes


def _execute_full_sleep(conn, duration: float) -> list:
    """
    完整睡眠 (duration > 3h):
    - 现有完整 reset 逻辑
    """
    cur = conn.cursor()
    changes = []

    def reset_key(k, new_val_fn):
        cur.execute("SELECT value, floor FROM sim_status WHERE key = ?", (k,))
        row = cur.fetchone()
        if not row:
            return
        old = int(row["value"])
        floor = int(row["floor"])
        new = clamp(new_val_fn(old), floor, 100)
        cur.execute("UPDATE sim_status SET value = ?, updated_at = ? WHERE key = ?", (new, now_cst_str(), k))
        log_change(conn, "sleep_reset", k, old, new, "sleep_reset")
        changes.append({"key": k, "old": old, "new": new})

    # sleepiness → 随机 75~95
    reset_key("sleepiness", lambda _: random.randint(75, 95))
    # fullness → 当前值 - 20~30
    reset_key("fullness", lambda cur: cur - random.randint(20, 30))
    # arousal → 随机 0~60
    reset_key("arousal", lambda _: random.randint(0, 60))
    # bladder → 当前值 - 25~40
    reset_key("bladder", lambda cur: cur - random.randint(25, 40))
    # hygiene → 不变

    # --- 情绪维度重置 ---
    cur.execute("SELECT key, value, floor FROM sim_status WHERE category = 'emotion'")
    emotion_rows = cur.fetchall()
    emotion_keys = [r["key"] for r in emotion_rows]

    # 全部清零
    for r in emotion_rows:
        old = int(r["value"])
        if old != 0:
            cur.execute("UPDATE sim_status SET value = 0, updated_at = ? WHERE key = ?", (now_cst_str(), r["key"]))
            log_change(conn, "sleep_reset", r["key"], old, 0, "sleep_reset")
            changes.append({"key": r["key"], "old": old, "new": 0})

    # 随机挑 1~3 个设为 10~40（模拟做梦残留）
    dream_count = random.randint(1, 3)
    dream_keys = random.sample(emotion_keys, min(dream_count, len(emotion_keys)))
    for k in dream_keys:
        dream_val = random.randint(10, 40)
        cur.execute("UPDATE sim_status SET value = ?, updated_at = ? WHERE key = ?", (dream_val, now_cst_str(), k))
        log_change(conn, "sleep_reset", k, 0, dream_val, "sleep_reset")
        for c in changes:
            if c["key"] == k:
                c["new"] = dream_val
                break
        else:
            changes.append({"key": k, "old": 0, "new": dream_val})

    # --- 意识状态 → awake ---
    cur.execute("UPDATE sim_consciousness SET state = 'awake', changed_at = ? WHERE id = 1", (now_cst_str(),))
    log_change(conn, "sleep_reset", "consciousness", None, "awake", "sleep_reset")

    return changes


@app.post("/api/sleep_reset")
async def api_sleep_reset(request: Request):
    """睡眠重置（自动按时长分流：<=0.5h 打盹不处理，0.5h~3h 小睡回血，>3h 完整睡眠）"""
    payload = await parse_payload(request)

    try:
        duration = float(payload.get("duration_hours", 8.0))
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"ok": False, "msg": "duration_hours 必须为数字"})

    if duration <= 0.5:
        return {
            "ok": True,
            "tier": "doze",
            "duration_hours": duration,
            "msg": f"打盹时长 {duration}h ≤ 0.5h，不触发状态变化",
            "changes": []
        }

    conn = get_db()
    try:
        if duration <= 3.0:
            changes = _execute_nap(conn, duration)
            conn.commit()
            return {
                "ok": True,
                "tier": "nap",
                "duration_hours": duration,
                "msg": f"小睡恢复完成 ({duration}h, sleepiness 封顶65, 烦躁/焦虑衰减30%)",
                "changes": changes
            }
        else:
            changes = _execute_full_sleep(conn, duration)
            conn.commit()
            return {
                "ok": True,
                "tier": "full_sleep",
                "duration_hours": duration,
                "msg": f"完整睡眠重置完成 ({duration}h)",
                "changes": changes
            }
    finally:
        conn.close()


@app.post("/api/nap")
async def api_nap(request: Request):
    """独立小睡接口 (0.5h < duration <= 3h)"""
    payload = await parse_payload(request)

    try:
        duration = float(payload.get("duration_hours", 1.5))
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"ok": False, "msg": "duration_hours 必须为数字"})

    if duration <= 0.5:
        return {
            "ok": True,
            "tier": "doze",
            "duration_hours": duration,
            "msg": f"打盹时长 {duration}h ≤ 0.5h，不触发状态变化",
            "changes": []
        }

    conn = get_db()
    try:
        changes = _execute_nap(conn, min(3.0, duration))
        conn.commit()
        return {
            "ok": True,
            "tier": "nap",
            "duration_hours": duration,
            "msg": f"小睡恢复完成 ({duration}h, sleepiness 封顶65, 烦躁/焦虑衰减30%)",
            "changes": changes
        }
    finally:
        conn.close()

# ==================== 5. set_floor ====================
@app.post("/api/set_floor")
async def api_set_floor(request: Request):
    """设定情绪底值"""
    payload = await parse_payload(request)
    key = str(payload.get("key", "")).strip()

    if not key:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "缺少 key"})

    try:
        floor_value = int(payload.get("floor_value", 0))
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"ok": False, "msg": "floor_value 必须为整数"})

    floor_value = max(0, min(100, floor_value))

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value, floor, category FROM sim_status WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"ok": False, "msg": f"维度 '{key}' 不存在"})

        old_floor = int(row["floor"])
        cur_val = int(row["value"])

        cur.execute("UPDATE sim_status SET floor = ?, updated_at = ? WHERE key = ?", (floor_value, now_cst_str(), key))
        log_change(conn, "set_floor", key, old_floor, floor_value, "manual")

        # 如果当前值低于新底值，拉上来
        if cur_val < floor_value:
            cur.execute("UPDATE sim_status SET value = ? WHERE key = ?", (floor_value, key))
            log_change(conn, "floor_lift", key, cur_val, floor_value, "manual")

        conn.commit()
        cn = KEY_CN.get(key, key)
        return {"ok": True, "key": key, "old_floor": old_floor, "new_floor": floor_value, "msg": f"{cn} 底值: {old_floor} → {floor_value}"}
    finally:
        conn.close()

# ==================== 6. consciousness ====================
@app.post("/api/consciousness")
async def api_consciousness(request: Request):
    """修改意识状态"""
    payload = await parse_payload(request)
    state = str(payload.get("state", "")).strip().lower()

    valid_states = ("awake", "alone_awake", "sleeping", "drowsy")
    if state not in valid_states:
        return JSONResponse(status_code=400, content={"ok": False, "msg": f"state 必须是 {valid_states} 之一"})

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT state FROM sim_consciousness WHERE id = 1")
        row = cur.fetchone()
        old_state = row["state"] if row else "unknown"

        cur.execute("UPDATE sim_consciousness SET state = ?, changed_at = ? WHERE id = 1", (state, now_cst_str()))
        log_change(conn, "consciousness", "consciousness", None, None, "manual")
        conn.commit()

        return {"ok": True, "old_state": old_state, "new_state": state, "msg": f"意识状态: {old_state} → {state}"}
    finally:
        conn.close()

# ==================== 7. full ====================
@app.post("/api/full")
async def api_full(request: Request):
    """返回完整状态快照"""
    conn = get_db()
    try:
        cur = conn.cursor()

        # 意识状态
        cur.execute("SELECT state, changed_at FROM sim_consciousness WHERE id = 1")
        c_row = cur.fetchone()
        consciousness = c_row["state"] if c_row else "awake"
        consciousness_since = c_row["changed_at"] if c_row else ""

        # 所有维度
        cur.execute("SELECT key, category, value, floor, decay_rate, updated_at, note FROM sim_status ORDER BY category, key")
        rows = cur.fetchall()

        physio = {}
        emotion = {}
        for r in rows:
            entry = {"value": r["value"], "floor": r["floor"], "decay_rate": r["decay_rate"], "updated_at": r["updated_at"], "note": r["note"]}
            if r["category"] == "physio":
                physio[r["key"]] = entry
            else:
                emotion[r["key"]] = entry

        return {
            "ok": True,
            "consciousness": consciousness,
            "consciousness_since": consciousness_since,
            "physio": physio,
            "emotion": emotion
        }
    finally:
        conn.close()

# ==================== 8. logs ====================
@app.post("/api/logs")
async def api_logs(request: Request):
    """查询最近变更日志（默认50条）"""
    payload = await parse_payload(request)
    try:
        limit = int(payload.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    limit = max(1, min(200, limit))

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, action, key, old_value, new_value, source, created_at FROM sim_log ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        return {"ok": True, "count": len(rows), "logs": rows}
    finally:
        conn.close()

# ==================== 启动 ====================

if __name__ == "__main__":
    uvicorn.run("simlife_service:app", host="127.0.0.1", port=8082, reload=False)

