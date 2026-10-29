import os
import sqlite3
import time
import logging

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway.db")

def get_db_connection(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_monitor_db(db_path=None):
    """初始化 monitor 相关表结构及索引"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        
        # 1. 心跳数据表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS monitor_heartbeat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                package TEXT NOT NULL,
                app_name TEXT NOT NULL,
                screen_state TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_heartbeat_timestamp ON monitor_heartbeat(timestamp)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_heartbeat_created_at ON monitor_heartbeat(created_at)")

        # 2. 截屏记录表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS monitor_screenshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                analyzed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_screenshot_timestamp ON monitor_screenshot(timestamp)")

        # 3. 手机端远程控制指令表 (单行配置)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS monitor_control (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                command TEXT NOT NULL DEFAULT 'IDLE',
                interval_sec INTEGER NOT NULL DEFAULT 300,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            INSERT OR IGNORE INTO monitor_control (id, command, interval_sec)
            VALUES (1, 'IDLE', 30)
        """)
        
        conn.commit()
        logger.info("[MONITOR_DB] 监控表结构初始化完成")
    finally:
        conn.close()

def save_heartbeat(timestamp: int, package: str, app_name: str, screen_state: str, db_path=None) -> dict:
    """存储心跳记录，并返回当前手机端应执行的控制指令"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO monitor_heartbeat (timestamp, package, app_name, screen_state)
            VALUES (?, ?, ?, ?)
        """, (timestamp, package, app_name, screen_state.lower().strip()))
        
        cur.execute("SELECT command, interval_sec FROM monitor_control WHERE id = 1")
        row = cur.fetchone()
        command = row['command'] if row else 'IDLE'
        interval_sec = row['interval_sec'] if row else 300
        
        conn.commit()
        return {
            "command": command,
            "interval": interval_sec
        }
    finally:
        conn.close()

def save_screenshot_record(timestamp: int, file_path: str, file_size: int, db_path=None) -> int:
    """存储截屏记录，返回记录 ID"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO monitor_screenshot (timestamp, file_path, file_size, analyzed)
            VALUES (?, ?, ?, 0)
        """, (timestamp, file_path, file_size))
        record_id = cur.lastrowid
        conn.commit()
        prune_old_screenshots(keep_count=100, db_path=db_path)
        return record_id
    finally:
        conn.close()

def get_latest_heartbeat(db_path=None) -> dict | None:
    """获取最新的一条心跳记录"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, timestamp, package, app_name, screen_state, created_at
            FROM monitor_heartbeat
            ORDER BY id DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()

def get_history_stats(minutes: int = 30, db_path=None) -> dict:
    """获取最近 N 分钟的心跳明细与使用时长统计"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        now_ts = int(time.time())
        cutoff_ts = now_ts - (minutes * 60)
        
        cur.execute("""
            SELECT id, timestamp, package, app_name, screen_state, created_at
            FROM monitor_heartbeat
            WHERE timestamp >= ? OR (timestamp >= ? AND timestamp > 100000000000)
            ORDER BY id ASC
        """, (cutoff_ts, cutoff_ts * 1000))
        
        rows = [dict(r) for r in cur.fetchall()]
        total_samples = len(rows)
        screen_on_count = sum(1 for r in rows if r['screen_state'] == 'on')
        screen_off_count = total_samples - screen_on_count
        
        app_counts = {}
        for r in rows:
            if r['screen_state'] == 'on':
                key = (r['app_name'], r['package'])
                app_counts[key] = app_counts.get(key, 0) + 1
        
        app_stats = []
        for (app_name, package), count in sorted(app_counts.items(), key=lambda x: x[1], reverse=True):
            approx_min = max(1, round(count * 0.5))
            app_stats.append({
                "app_name": app_name,
                "package": package,
                "minutes": approx_min,
                "samples": count
            })
            
        return {
            "minutes": minutes,
            "total_samples": total_samples,
            "screen_on_min": round(screen_on_count * 0.5),
            "screen_off_min": round(screen_off_count * 0.5),
            "apps": app_stats,
            "records": rows[-50:]
        }
    finally:
        conn.close()

def get_continuous_screen_on_duration(db_path=None) -> float:
    """
    计算当前连续亮屏时长（单位：分钟）
    从最新一条记录往前倒推，统计连续为 'on' 的样本数；遇到 'off' 或时间中断则停止。
    """
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT timestamp, screen_state
            FROM monitor_heartbeat
            ORDER BY id DESC
            LIMIT 120
        """)
        rows = cur.fetchall()
        if not rows:
            return 0.0
            
        if rows[0]['screen_state'] != 'on':
            return 0.0
            
        consecutive_on_count = 0
        last_ts = None
        
        for r in rows:
            state = r['screen_state']
            ts = r['timestamp']
            if ts > 100000000000:
                ts = int(ts / 1000)
                
            if state != 'on':
                break
                
            if last_ts is not None and abs(last_ts - ts) > 180:
                break
                
            consecutive_on_count += 1
            last_ts = ts
            
        return consecutive_on_count * 0.5
    finally:
        conn.close()

def set_monitor_command(command: str, interval_sec: int = 30, db_path=None):
    """设置下发给手机端的控制指令"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE monitor_control
            SET command = ?, interval_sec = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
        """, (command.upper().strip(), interval_sec))
        conn.commit()
    finally:
        conn.close()

def get_monitor_command(db_path=None) -> dict:
    """获取当前下发的控制指令"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT command, interval_sec, updated_at FROM monitor_control WHERE id = 1")
        row = cur.fetchone()
        if not row:
            return {"command": "IDLE", "interval": 30}
        return dict(row)
    finally:
        conn.close()


def get_recent_screenshots(limit: int = 20, db_path=None) -> list:
    """获取最近的截屏记录列表"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, timestamp, file_path, file_size, analyzed, created_at
            FROM monitor_screenshot
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

def get_recent_heartbeats(limit: int = 50, db_path=None) -> list:
    """获取最近的心跳流水明细"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, timestamp, package, app_name, screen_state, created_at
            FROM monitor_heartbeat
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_latest_screenshot(db_path=None) -> dict | None:
    """获取最新的一条截屏记录"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, timestamp, file_path, file_size, analyzed, created_at
            FROM monitor_screenshot
            ORDER BY id DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def prune_old_screenshots(keep_count: int = 100, db_path=None):
    """清理超出数量的旧截屏文件与数据库记录，防止服务器磁盘堆积"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, file_path FROM monitor_screenshot
            ORDER BY id DESC
            LIMIT -1 OFFSET ?
        """, (keep_count,))
        old_rows = cur.fetchall()
        if old_rows:
            ids_to_del = []
            for r in old_rows:
                ids_to_del.append(r['id'])
                fpath = r['file_path']
                try:
                    if os.path.exists(fpath):
                        os.remove(fpath)
                except Exception as e:
                    logger.warning(f"[MONITOR_DB] 删除旧截屏文件异常: {e}")
            cur.execute(f"DELETE FROM monitor_screenshot WHERE id IN ({','.join(['?']*len(ids_to_del))})", ids_to_del)
            conn.commit()
            logger.info(f"[MONITOR_DB] 已自动轮转清理 {len(ids_to_del)} 张旧截屏")
    except Exception as e:
        logger.error(f"[MONITOR_DB] 轮转清理旧截屏异常: {e}")
    finally:
        conn.close()
