"""
qq_text_cleaner.py - 全面清洗历史记录与跨端上下文中的时间戳、标签、小说文体与动作描写
"""
import re

ACTION_PATTERNS = [
    r'我(?:的)?(?:另一只)?(?:手|手掌|手指|双臂|胳膊|食指|大拇指|掌心|指腹)[^。？！\n]*(?:托住|按在|覆在|贴上|伸进|伸到|摸|掐|捏|按|揉|扣在|抚过|顺着|卡在|刮了)',
    r'(?:我|伸手)[^。？！\n]*(?:托住|捏住|覆在|贴上|伸进|伸到|掐住|扣在|揉揉)[^。？！\n]*(?:脸颊|脸蛋|后颈|颈窝|下巴|太阳穴|腰|肚子|唇|睡衣|被子)',
    r'来[。， ]*(?:脸转过来|把头抬起来|手伸过来|把脸凑过来)',
    r'(?:脸转过来|把头抬起来|下巴抬起来)[。！]',
    r'你下巴肉夹我手心[^。？！\n]*',
]

def is_action_narration(text: str) -> bool:
    """判断句子是否为小说式的肢体接触或虚拟动作描写"""
    if not isinstance(text, str) or not text.strip():
        return False
    for pat in ACTION_PATTERNS:
        if re.search(pat, text):
            return True
    return False

def clean_operit_style(text: str, is_user: bool = False) -> str:
    """清洗历史记录中的小说文体与动作描写，保留对话台词"""
    if not isinstance(text, str) or not text.strip():
        return text

    # 1. 剔除所有来源标签与时间戳（全局替换）
    text = re.sub(r'\[(QQ|Operit|群聊|私聊|系统|QQ·私聊|QQ·群聊)[^\]]*\]\s*', '', text)
    text = re.sub(r'\[?\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}(:\d{2})?\]?\s*', '', text)

    # 2. 剔除附件标签与多模态冗余文字
    text = re.sub(r'<attachment[^>]*>[\s\S]*?</attachment>', '', text)
    text = re.sub(r'<system>[\s\S]*?</system>', '', text)

    # 3. 剔除所有 *动作*、中文括号（动作）、英文括号(动作)
    text = re.sub(r'\*[^*]+\*', '', text)
    text = re.sub(r'（[^）]+）', '', text)
    text = re.sub(r'\([^)]+\)', '', text)

    # 如果是用户消息，清理完括号动作后直接返回
    if is_user:
        return text.strip()

    # 4. 如果包含小说式双引号+旁白，提取对白
    quotes = re.findall(r'[“"「]([^”"」]+)[”"」]', text)
    if quotes and len(''.join(quotes)) >= 10:
        text = ' '.join(quotes)

    # 5. 按段落过滤过长的无台词叙事段及动作描写句
    paragraphs = [p.strip() for p in text.split(chr(10)) if p.strip()]
    kept = []
    for p in paragraphs:
        if is_action_narration(p):
            continue
        if len(p) > 60 and not any(c in p for c in '“”"「」'):
            continue
        kept.append(p)

    result = chr(10).join(kept).strip() if kept else text.strip()
    if len(result) > 200:
        result = result[:200]
    return result
