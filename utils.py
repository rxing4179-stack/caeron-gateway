from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

def now_cst() -> datetime:
    return datetime.now(CST)

def today_cst_str() -> str:
    return now_cst().strftime("%Y-%m-%d")
