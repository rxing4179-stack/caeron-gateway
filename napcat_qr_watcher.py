#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

STATUS_PATH = "/home/ubuntu/caeron-gateway/static/qr_status.json"
GATEWAY_LOG = "/home/ubuntu/caeron-gateway/gateway.log"
CST = timezone(timedelta(hours=8))

OFFLINE_MARKERS = (
    "[KickedOffLine]",
    "账号状态变更为离线",
    "你的账号当前登录已失效",
    '"result": 1006514',
)

def run_cmd(cmd, timeout=12):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except Exception as e:
        return -1, "", str(e)

def write_status(data):
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATUS_PATH)

def parse_iso_utc(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    if "." in s:
        base, frac = s.split(".", 1)
        frac = (frac + "000000")[:6]
        s = f"{base}.{frac}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

def now_cst_str():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def check_gateway_ws_connected(started_utc):
    started_cst = started_utc.astimezone(CST).replace(tzinfo=None)
    try:
        with open(GATEWAY_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            if "[QQ] NapCat WebSocket 已连接" in line:
                m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
                if m:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    if ts >= started_cst:
                        return True
                break
    except Exception:
        pass
    return False

def get_latest_offline_pos(logs):
    latest = -1
    for marker in OFFLINE_MARKERS:
        pos = logs.rfind(marker)
        if pos > latest:
            latest = pos
    return latest

def main():
    # Clean up legacy /tmp watcher if any
    if os.path.exists("/tmp/napcat_qr_watcher.pid"):
        try:
            old_pid = int(open("/tmp/napcat_qr_watcher.pid").read().strip())
            os.kill(old_pid, 15)
        except Exception:
            pass
        try:
            os.remove("/tmp/napcat_qr_watcher.pid")
        except Exception:
            pass

    last_restart_wall = 0.0

    while True:
        now_ts = time.time()
        rc, out, _ = run_cmd(["sudo", "docker", "inspect", "-f", "{{.State.Running}} {{.State.StartedAt}}", "napcat"], timeout=8)
        parts = out.strip().split()
        if rc != 0 or len(parts) < 2 or parts[0] != "true":
            write_status({
                "logged_in": False,
                "qr_ready": False,
                "restarting": True,
                "qr_mtime": 0,
                "qr_age_sec": 0,
                "status_msg": "NapCat 容器正在重启或未运行，等待恢复...",
                "updated_at": now_cst_str()
            })
            time.sleep(2)
            continue

        started_iso = parts[1]
        try:
            started_utc = parse_iso_utc(started_iso)
            started_unix = started_utc.timestamp()
        except Exception:
            started_utc = datetime.now(timezone.utc)
            started_unix = now_ts

        since_unix = str(int(started_unix))
        _, logs_out, logs_err = run_cmd(["sudo", "docker", "logs", "--since", since_unix, "--tail", "2000", "napcat"], timeout=10)
        logs = (logs_out or "") + "\n" + (logs_err or "")

        # 1. Check if this boot got kicked offline / disconnected
        last_offline_pos = get_latest_offline_pos(logs)
        relogin_after_offline = (
            last_offline_pos != -1
            and "账号登录成功" in logs[last_offline_pos:]
        )
        kicked_offline = (last_offline_pos != -1) and (not relogin_after_offline)

        if kicked_offline and (now_ts - last_restart_wall > 20):
            print(f"[{now_cst_str()}] Offline/Kicked detected in napcat logs, restarting container...", flush=True)
            write_status({
                "logged_in": False,
                "qr_ready": False,
                "restarting": True,
                "qr_mtime": 0,
                "qr_age_sec": 0,
                "status_msg": "检测到 QQ 掉线/登录失效，正在自动重启 NapCat 生成新二维码...",
                "updated_at": now_cst_str()
            })
            last_restart_wall = time.time()
            run_cmd(["sudo", "docker", "restart", "napcat"], timeout=25)
            time.sleep(5)
            continue

        # 2. Check if logged in on current boot
        logged_in = (not kicked_offline) and (
            "AAA专业浣熊养殖工程师 |" in logs
            or "账号登录成功" in logs
            or check_gateway_ws_connected(started_utc)
        )

        if logged_in:
            # Read existing qr_mtime to preserve field
            qr_mtime = 0
            try:
                if os.path.exists(STATUS_PATH):
                    qr_mtime = int(json.load(open(STATUS_PATH, "r", encoding="utf-8")).get("qr_mtime", 0))
            except Exception:
                pass
            write_status({
                "logged_in": True,
                "qr_ready": False,
                "restarting": False,
                "qr_mtime": qr_mtime,
                "qr_age_sec": 0,
                "status_msg": "🎉 扫码登录成功！小沈 QQ 已上线并重连网关！",
                "updated_at": now_cst_str()
            })
            time.sleep(10)
            continue

        # 3. Waiting for QR login -> inspect qrcode.png mtime
        rc_stat, stat_out, _ = run_cmd(["sudo", "docker", "exec", "napcat", "stat", "-c", "%Y", "/app/napcat/cache/qrcode.png"], timeout=6)
        qr_mtime = 0
        if rc_stat == 0 and stat_out.strip().isdigit():
            qr_mtime = int(stat_out.strip())

        qr_fresh = (qr_mtime > 0 and qr_mtime >= int(started_unix) - 2)
        qr_age_sec = max(0, int(now_ts) - qr_mtime) if qr_fresh else 0

        err_match = re.search(r"Login Error\s*,\s*ErrInfo:\s*(\[.*\])", logs)
        has_server_err = ("serverErrorCode" in logs) or (err_match is not None)
        need_restart = False

        if has_server_err and (now_ts - last_restart_wall > 15):
            msg_detail = "扫码被拦截或已失效"
            m_msg = re.search(r'"message":"([^"]+)"', logs)
            m_code = re.search(r'"serverErrorCode":(\d+)', logs)
            if m_msg:
                code_str = f"错误码 {m_code.group(1)}: " if m_code else ""
                msg_detail = code_str + m_msg.group(1)
            status_msg = f"⚠️ {msg_detail} —— 正在自动重启 NapCat 生成新二维码..."
            need_restart = True
        elif not qr_fresh:
            status_msg = "NapCat 启动初始化中，正在生成全新二维码..."
        elif qr_age_sec > 135 and (now_ts - last_restart_wall > 20):
            status_msg = f"二维码已超时 ({qr_age_sec}s) 未自动换新，正在自动重启 NapCat 刷新二维码..."
            need_restart = True
        else:
            if qr_age_sec >= 115:
                status_msg = f"当前二维码即将过期（已生成 {qr_age_sec} 秒），建议等待下一张新码"
            else:
                status_msg = f"等待手机 QQ 扫码确认（当前二维码生成于 {qr_age_sec} 秒前）"

        write_status({
            "logged_in": False,
            "qr_ready": bool(qr_fresh and not need_restart),
            "restarting": bool(need_restart or not qr_fresh),
            "qr_mtime": qr_mtime if qr_fresh else 0,
            "qr_age_sec": qr_age_sec,
            "status_msg": status_msg,
            "updated_at": now_cst_str()
        })

        if need_restart:
            last_restart_wall = time.time()
            print(f"[{now_cst_str()}] Restarting napcat: {status_msg}", flush=True)
            run_cmd(["sudo", "docker", "restart", "napcat"], timeout=25)
            time.sleep(4)
            continue

        time.sleep(2)

if __name__ == "__main__":
    main()
