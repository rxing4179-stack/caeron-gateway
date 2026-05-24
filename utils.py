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

import re

def clean_chat_text(text: str) -> str:
    """
    清洗消息中的无用元数据，防止污染记忆空间
    """
    if not text:
        return ""
    
    # 1. 移除 <attachment> 标签及其内容
    text = re.sub(r'<attachment[^>]*>[\s\S]*?</attachment>', '', text)
    
    # 2. 移除系统自动注入的元数据块
    text = re.sub(r'【当前(时间|天气|位置|屏幕应用)】[\s\S]*?(?=(【|$))', '', text)
    
    # 3. 移除 [发送了 N 张图片] 占位符
    text = re.sub(r'\[发送了.*?图片\]', '', text)
    
    # 4. 移除连续空行，压缩为单个换行
    text = re.sub(r'\n\s*\n', '\n', text)
    
    return text.strip()

def smart_truncate(text: str, max_chars: int = 200) -> str:
    """对单段文本进行前后截断保留核心"""
    if len(text) <= max_chars:
        return text
        
    front_len = int(max_chars * 0.6)
    back_len = max_chars - front_len - 5 # 减去 [...] 的长度
    
    return text[:front_len] + " [...] " + text[-back_len:]

def smart_truncate_dialogue(text: str, max_chars_per_side: int = 200) -> str:
    """
    从 "User: xxx\nAssistant: yyy" 格式中提取并分别智能截断
    """
    parts = text.split('\nAssistant: ', 1)
    if len(parts) != 2:
        # 如果格式不是标准的，直接截断整体
        return smart_truncate(text, max_chars_per_side * 2)
        
    user_part = parts[0]
    if user_part.startswith('User: '):
        user_part = user_part[6:]
        
    assistant_part = parts[1]
    
    trunc_user = smart_truncate(user_part, max_chars_per_side)
    trunc_ast = smart_truncate(assistant_part, max_chars_per_side)
    
    # 重新组装
    return f"User: {trunc_user}\nAssistant: {trunc_ast}"
