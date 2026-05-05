from datetime import datetime, timedelta, timezone

# 北京时间 (UTC+8)
CST = timezone(timedelta(hours=8))

def now_cst() -> datetime:
    """获取当前北京时间"""
    return datetime.now(CST)

def today_cst_str() -> str:
    """获取当前北京日期字符串 (YYYY-MM-DD)"""
    return now_cst().strftime("%Y-%m-%d")

def format_cst(dt: datetime) -> str:
    """格式化北京时间"""
    return dt.strftime("%Y-%m-%d %H:%M:%S")
