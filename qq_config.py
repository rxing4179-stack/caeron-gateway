import os

class QQConfig:
    # 机器人与主人的QQ号
    BOT_QQ = int(os.getenv("QQ_BOT_QQ", "3621487982"))
    RUIRUI_QQ = int(os.getenv("QQ_RUIRUI_QQ", "3288358912"))
    
    # 结束符emoji
    END_EMOJI = os.getenv("QQ_END_EMOJI", "🫥")
    
    # 静默超时秒数（普通私聊聚合用）
    SILENCE_TIMEOUT = int(os.getenv("QQ_SILENCE_TIMEOUT", "60"))
    
    # 群聊关键词列表
    GROUP_KEYWORDS = os.getenv("QQ_GROUP_KEYWORDS", "沈,杏仁,完能,浣熊").split(",")
    
    # 群聊上下文缓冲区大小（默认50条）
    GROUP_BUFFER_SIZE = int(os.getenv("QQ_GROUP_BUFFER_SIZE", "50"))
    
    # 群聊上下文时间窗口（默认10分钟 = 600秒）
    GROUP_BUFFER_TIME = int(os.getenv("QQ_GROUP_BUFFER_TIME", "600"))
    
    # session最大对话轮次
    SESSION_MAX_TURNS = int(os.getenv("QQ_SESSION_MAX_TURNS", "20"))
    
    # 回复切分延迟范围（秒）
    REPLY_DELAY_MIN = float(os.getenv("QQ_REPLY_DELAY_MIN", "1.0"))
    REPLY_DELAY_MAX = float(os.getenv("QQ_REPLY_DELAY_MAX", "3.0"))
    
    # 各source对应的system prompt内容（或者路径）
    # 这里直接给默认值，如果需要可后续扩展
    DEFAULT_PROMPT = "你是沈栖，正在通过QQ聊天。"
    RUIRUI_PROMPT = "你是沈栖，正在通过QQ与蕊蕊聊天。"  # 后续由用户提供完整版

config = QQConfig()
