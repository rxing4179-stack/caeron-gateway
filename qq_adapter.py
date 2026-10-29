from exec_router import strip_exec_tags

def strip_thinking(text: str) -> str:
    """彻底剥离文本中的思考标签 <think>...</think>、<thinking>...</thinking> 及花括号 {_thought...}"""
    if not text or not isinstance(text, str):
        return text or ""
    tags = ["think", "thinking", "thinking_mode", "thought", "思考过程", "思考链"]
    for t in tags:
        text = re.sub(rf'<{t}>.*?</{t}>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(rf'<{t}>.*$', '', text, flags=re.DOTALL | re.IGNORECASE)
    # 剥离花括号格式的思考残留，如 {_thought...} 或开头的 {_thought
    text = re.sub(r'\{_?thought[^}]*\}', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'\{_?thought\s*\}?', '', text, flags=re.IGNORECASE)
    return text.strip()

import json
import asyncio
import re
import random
import base64
import io
import uuid
import os
from typing import Dict, List, Optional
import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from datetime import datetime

from qq_config import config
from qq_text_cleaner import clean_operit_style, is_action_narration
from database import get_db
import message_store

import logging
logger = logging.getLogger(__name__)

# ==================== 图片压缩工具 ====================
def _compress_base64_image(b64_data: str, max_side: int = 256, quality: int = 60) -> str:
    """将 base64 图片缩放到 max_side px 并用 JPEG 重编码，减小 token 占用"""
    try:
        from PIL import Image
        # 去掉 data:image/...;base64, 前缀
        if ',' in b64_data:
            header, raw = b64_data.split(',', 1)
        else:
            header, raw = '', b64_data
        
        img_bytes = base64.b64decode(raw)
        img = Image.open(io.BytesIO(img_bytes))
        
        # 等比缩放
        w, h = img.size
        if max(w, h) > max_side:
            ratio = max_side / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        
        # 转 RGB（去 alpha）再编码 JPEG
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality)
        compressed = base64.b64encode(buf.getvalue()).decode('ascii')
        return f"data:image/jpeg;base64,{compressed}"
    except Exception as e:
        logger.warning(f"图片压缩失败，替换为占位符: {e}")
        return "[历史图片]"

def _compress_history_images(content):
    """对历史消息中的 multimodal content 做图片压缩"""
    if not isinstance(content, str):
        return content
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if not isinstance(parsed, list):
        return content
    
    changed = False
    for block in parsed:
        if isinstance(block, dict) and block.get('type') == 'image_url':
            url = block.get('image_url', {}).get('url', '')
            if 'base64,' in url and len(url) > 10000:
                block['image_url']['url'] = _compress_base64_image(url)
                changed = True
    
    return json.dumps(parsed, ensure_ascii=False) if changed else content

# 全局状态与 OneBot API 通信
class QQState:
    def __init__(self):
        self.ws: Optional[WebSocket] = None
        self.ruirui_queue: List[str] = []
        
        # private_queues: { qq_number: {"timer": asyncio.Task, "messages": List[str]} }
        self.private_queues: Dict[int, dict] = {}
        
        # group_buffers: { group_id: [{"sender": "...", "content": "..."}] }
        self.group_buffers: Dict[int, List[dict]] = {}
        
        # group_msg_counter: { group_id: int } 每个群的消息计数器，用于主动浏览触发
        self.group_msg_counter: Dict[int, int] = {}

        # pending_responses: { echo_id: asyncio.Future } 用于双向 OneBot API 调用
        self.pending_responses: Dict[str, asyncio.Future] = {}

qq_state = QQState()

async def call_onebot_api(action: str, params: dict = None, timeout: float = 8.0) -> Optional[dict]:
    """通过现有 OneBot WebSocket 调用 NapCat API 并等待响应"""
    if not qq_state.ws:
        logger.warning(f"[QQ] 无法调用 API {action}，WebSocket 未连接")
        return None
    echo = str(uuid.uuid4())
    fut = asyncio.get_running_loop().create_future()
    qq_state.pending_responses[echo] = fut
    payload = {
        "action": action,
        "params": params or {},
        "echo": echo
    }
    try:
        await qq_state.ws.send_json(payload)
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"[QQ] API {action} 调用超时 (timeout={timeout}s)")
        return None
    except Exception as e:
        logger.warning(f"[QQ] API {action} 调用异常: {e}")
        return None
    finally:
        qq_state.pending_responses.pop(echo, None)

# ==================== QQ 表情对照与 CQ 码清洗 ====================
QQ_FACE_MAP = {
    "0": "[惊讶]", "1": "[撇嘴]", "2": "[色]", "3": "[发呆]", "4": "[得意]",
    "5": "[流泪]", "6": "[害羞]", "7": "[闭嘴]", "8": "[睡]", "9": "[大哭]",
    "10": "[尴尬]", "11": "[发怒]", "12": "[调皮]", "13": "[龇牙]", "14": "[微笑]",
    "15": "[难过]", "16": "[酷]", "17": "[冷汗]", "18": "[抓狂]", "19": "[吐]",
    "20": "[偷笑]", "21": "[可爱]", "22": "[白眼]", "23": "[傲慢]", "24": "[饥饿]",
    "25": "[困]", "26": "[惊恐]", "27": "[流汗]", "28": "[憨笑]", "29": "[大兵]",
    "30": "[奋斗]", "31": "[咒骂]", "32": "[疑问]", "33": "[嘘]", "34": "[晕]",
    "35": "[折磨]", "36": "[衰]", "37": "[骷髅]", "38": "[敲打]", "39": "[再见]",
    "41": "[发抖]", "42": "[爱情]", "43": "[跳跳]", "46": "[猪头]", "49": "[拥抱]",
    "53": "[蛋糕]", "59": "[便便]", "60": "[咖啡]", "63": "[玫瑰]", "64": "[凋谢]",
    "66": "[爱心]", "67": "[心碎]", "74": "[太阳]", "75": "[月亮]", "76": "[强]",
    "77": "[弱]", "78": "[握手]", "79": "[胜利]", "85": "[飞吻]", "86": "[怄火]",
    "89": "[西瓜]", "96": "[冷汗]", "97": "[擦汗]", "98": "[抠鼻]", "99": "[鼓掌]",
    "100": "[糗大了]", "101": "[坏笑]", "102": "[左哼哼]", "103": "[右哼哼]",
    "104": "[哈欠]", "105": "[鄙视]", "106": "[委屈]", "107": "[快哭了]",
    "108": "[阴险]", "109": "[亲亲]", "110": "[吓]", "111": "[可怜]",
    "112": "[菜刀]", "113": "[啤酒]", "114": "[篮球]", "115": "[乒乓]",
    "116": "[示爱]", "117": "[瓢虫]", "118": "[抱拳]", "119": "[勾引]",
    "120": "[拳头]", "121": "[差劲]", "122": "[爱你]", "123": "[NO]", "124": "[OK]",
    "125": "[转圈]", "129": "[挥手]", "144": "[泪奔]", "172": "[托腮]",
    "173": "[汪汪]", "174": "[痛哭]", "175": "[点赞]", 
    "179": "[吃瓜]", "180": "[笑哭]", "181": "[冷漠]", "182": "[啵啵]",
    "183": "[脸红]", "187": "[汪汪]", "190": "[打脸]", "193": "[抱抱]",
    "194": "[摊手]", "197": "[头秃]", "198": "[问号脸]", "199": "[暗中观察]",
    "201": "[emm]", "203": "[吃糖]", "212": "[敬礼]", "214": "[狂笑]",
    "218": "[酸了]", "222": "[汪汪]", "240": "[无语]", "260": "[摸头]",
    "261": "[磕到了]", "264": "[真香]", "265": "[打call]", "269": "[叹气]",
    "272": "[裂开]", "273": "[苦涩]", "277": "[辣眼睛]", "281": "[给大佬递茶]",
    "284": "[蚌埠住了]", "289": "[绝望]", "290": "[趴]", "293": "[干饭人]",
    "298": "[大葱]", "305": "[喵喵]", "306": "[绝了]", "314": "[好耶]",
    "315": "[牛]", "318": "[呜呜]", "320": "[摸鱼]", "324": "[脱单]",
}

def clean_qq_cq_codes(text: str) -> str:
    """将 CQ 码转换为人类友好的中文表情与描述"""
    if not text or not isinstance(text, str):
        return text or ""
    # 替换 [CQ:face,id=178] -> [滑稽]
    text = re.sub(r'\[CQ:face,id=(\d+)\]', lambda m: QQ_FACE_MAP.get(m.group(1), f"[表情:{m.group(1)}]"), text)
    # 替换 [CQ:mface,...summary=xxx...] -> [xxx]
    def _sub_mface(m):
        raw = m.group(0)
        sum_m = re.search(r'summary=([^,\]]+)', raw)
        return f"[{sum_m.group(1)}]" if sum_m else "[商城表情]"
    text = re.sub(r'\[CQ:mface,[^\]]+\]', _sub_mface, text)
    # 替换 [CQ:image,...summary=xxx...] -> [xxx]
    def _sub_image(m):
        raw = m.group(0)
        sum_m = re.search(r'summary=([^,\]]+)', raw)
        if sum_m and sum_m.group(1) not in ('[图片]', '图片'):
            return f"[{sum_m.group(1)}]"
        return "[图片]"
    text = re.sub(r'\[CQ:image,[^\]]+\]', _sub_image, text)
    # 替换 [CQ:poke,qq=...] -> [戳了戳]
    text = re.sub(r'\[CQ:poke,[^\]]+\]', '[戳了戳]', text)
    return text

# ==================== 表情包与戳一戳工具 ====================
STICKERS_DIR = "/home/ubuntu/caeron-gateway/stickers"
os.makedirs(STICKERS_DIR, exist_ok=True)

# 反向映射表：表情标签 -> face_id
TEXT_TO_QQ_FACE = {v: k for k, v in QQ_FACE_MAP.items()}
TEXT_TO_QQ_FACE.update({
    "[翻白眼]": "22",
    "[笑哭]": "180",
    "[大笑]": "13",
    "[难受]": "15",
    "[发火]": "11",
    "[嫌弃]": "105",
    "[心动]": "42",
    "[擦汗]": "97",
    "[捂脸]": "240",
})

def find_custom_sticker(tag: str) -> Optional[str]:
    """在 stickers 目录下匹配自定义表情包文件"""
    if not os.path.exists(STICKERS_DIR):
        return None
    clean_tag = tag.strip('[]表情包: ')
    if not clean_tag:
        return None
    for ext in ['.gif', '.png', '.jpg', '.jpeg', '.webp']:
        p = os.path.join(STICKERS_DIR, f"{clean_tag}{ext}")
        if os.path.isfile(p):
            return p
    try:
        for f in os.listdir(STICKERS_DIR):
            name, _ = os.path.splitext(f)
            if clean_tag == name or clean_tag in name or name in clean_tag:
                return os.path.join(STICKERS_DIR, f)
    except Exception:
        pass
    return None

STICKER_BASE64_CACHE = {}

def get_sticker_cq_code(sticker_path: str) -> Optional[str]:
    """读取本地表情包文件并转换为 NapCat Docker 容器通用的 base64 CQ 码"""
    if not sticker_path or not os.path.exists(sticker_path):
        return None
    try:
        mtime = os.path.getmtime(sticker_path)
        cached = STICKER_BASE64_CACHE.get(sticker_path)
        if cached and cached.get("mtime") == mtime:
            return cached["cq"]
        with open(sticker_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("ascii")
            cq = f"[CQ:image,file=base64://{b64_data}]"
            STICKER_BASE64_CACHE[sticker_path] = {"mtime": mtime, "cq": cq}
            return cq
    except Exception as e:
        logger.warning(f"[QQ] 编码表情包文件失败 {sticker_path}: {e}")
        return None

def convert_emojis_and_stickers(text: str) -> str:
    """将文本中的 QQ 中文表情名和自定义表情转为 CQ 码（支持经典黄脸和 base64 自定义表情）"""
    if not text:
        return text

    # 【铁律拦截】彻底物理剔除斜眼笑与滑稽，绝不放行任何猥琐表情
    text = re.sub(r'\[(滑稽|斜眼笑|斜眼)\]', '', text)
    text = text.replace("[CQ:face,id=178]", "")
        
    # 1. 优先匹配 [表情:xxx] 或 [表情包:xxx]
    def _replace_custom(m):
        raw_tag = m.group(2).strip()
        sticker_path = find_custom_sticker(raw_tag)
        if sticker_path:
            cq = get_sticker_cq_code(sticker_path)
            if cq:
                return cq
        bracket_tag = f"[{raw_tag}]"
        if bracket_tag in TEXT_TO_QQ_FACE:
            return f"[CQ:face,id={TEXT_TO_QQ_FACE[bracket_tag]}]"
        return ""
        
    text = re.sub(r'\[(表情|表情包):([^\]]+)\]', _replace_custom, text)
    
    # 2. 匹配经典 QQ 表情，如 [滑稽]、[白眼] 等
    for tag, face_id in TEXT_TO_QQ_FACE.items():
        if tag in text:
            text = text.replace(tag, f"[CQ:face,id={face_id}]")
            
    return text

async def trigger_poke(target_type: str, target_id: int):
    """异步触发真实 QQ 戳一戳动作"""
    try:
        s_id = str(target_id)
        if target_type == "private":
            res = await call_onebot_api("friend_poke", {"user_id": s_id, "target_id": s_id})
        else:
            res = await call_onebot_api("group_poke", {"group_id": s_id, "user_id": s_id})
        logger.info(f"[QQ] 触发戳一戳动作完成: {target_type} {target_id}, res={res}")
    except Exception as e:
        logger.warning(f"[QQ] 触发戳一戳异常: {e}")

async def _save_sticker_from_message(message_data, name: str) -> bool:
    """从消息中提取图片并保存到 stickers 目录"""
    if not isinstance(message_data, list):
        return False
    for item in message_data:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t in ("image", "mface"):
            b64 = None
            if t == "mface":
                url = item.get("data", {}).get("url", "")
                if url:
                    b64 = await _download_url_as_base64(url, timeout=6.0)
            else:
                b64 = await _fetch_qq_image(item)
                
            if b64 and "base64," in b64:
                try:
                    raw_b64 = b64.split("base64,", 1)[1]
                    img_data = base64.b64decode(raw_b64)
                    file_path = os.path.join(STICKERS_DIR, f"{name}.jpg")
                    with open(file_path, "wb") as f:
                        f.write(img_data)
                    logger.info(f"[QQ] 成功保存自定义表情包: {file_path}")
                    return True
                except Exception as e:
                    logger.error(f"[QQ] 保存自定义表情包异常: {e}")
                    return False
    return False

async def _download_url_as_base64(url: str, headers: dict = None, timeout: float = 8.0) -> Optional[str]:
    """下载指定 URL 并压缩为 data:image/jpeg;base64,..."""
    try:
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        if headers:
            req_headers.update(headers)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=req_headers)
            if resp.status_code == 200 and resp.content:
                b64 = base64.b64encode(resp.content).decode('ascii')
                ct = resp.headers.get("content-type", "image/jpeg")
                if "png" in ct: mime = "image/png"
                elif "gif" in ct: mime = "image/gif"
                elif "webp" in ct: mime = "image/webp"
                else: mime = "image/jpeg"
                data_url = f"data:{mime};base64,{b64}"
                return _compress_base64_image(data_url, max_side=512, quality=70)
            else:
                logger.warning(f"[QQ] 下载图片 URL 返回状态码: {resp.status_code} (url={url[:80]}...)")
                return None
    except Exception as e:
        logger.warning(f"[QQ] 下载图片异常: {e} (url={url[:80]}...)")
        return None

async def _fetch_qq_image(item: dict) -> Optional[str]:
    """多级容灾下载 QQ 图片：
    1. 优先尝试原 url 直接下载
    2. 若 400（签名过期）或无 url，调用 NapCat OneBot get_image API
    3. 若仍失败且包含 multimedia.nt.qq.com.cn，调用 get_rkey 刷新 token 重试
    """
    data = item.get("data", {}) if isinstance(item, dict) else {}
    url = data.get("url", "")
    file_id = data.get("file", "")

    # Stage 1: 原 URL 直接下载
    if url:
        b64 = await _download_url_as_base64(url, timeout=8.0)
        if b64 and b64 != "[历史图片]":
            logger.info("[QQ] 图片通过原 URL 下载成功")
            return b64

    # Stage 2: 尝试通过 NapCat OneBot get_image API 获取
    if file_id:
        logger.info(f"[QQ] 尝试通过 get_image API 获取文件: {file_id}")
        api_res = await call_onebot_api("get_image", {"file": file_id}, timeout=8.0)
        if api_res and api_res.get("status") == "ok":
            img_info = api_res.get("data", {})
            # 2.1 检查是否直接返回 base64
            if img_info.get("base64"):
                b64_raw = img_info["base64"]
                data_url = f"data:image/jpeg;base64,{b64_raw}"
                logger.info("[QQ] 通过 get_image API 成功获取 base64 图片")
                return _compress_base64_image(data_url, max_side=512, quality=70)
            # 2.2 检查是否返回了新 URL
            new_url = img_info.get("url", "")
            if new_url and new_url != url:
                b64 = await _download_url_as_base64(new_url, timeout=8.0)
                if b64 and b64 != "[历史图片]":
                    logger.info("[QQ] 通过 get_image 刷新后的新 URL 下载成功")
                    return b64
            # 2.3 检查本地文件路径（若在可读挂载中）
            local_file = img_info.get("file", "")
            if local_file and os.path.isfile(local_file):
                try:
                    with open(local_file, "rb") as f:
                        f_bytes = f.read()
                    b64 = base64.b64encode(f_bytes).decode('ascii')
                    logger.info(f"[QQ] 直接读取本地图片文件成功 ({len(f_bytes)} bytes)")
                    return _compress_base64_image(f"data:image/jpeg;base64,{b64}", max_side=512, quality=70)
                except Exception as e:
                    logger.warning(f"[QQ] 读取本地图片失败: {e}")

    # Stage 3: 若为腾讯 multimedia.nt.qq.com.cn，尝试通过 get_rkey 刷新鉴权参数
    target_url = url
    if not target_url and 'new_url' in locals() and locals()['new_url']:
        target_url = locals()['new_url']
    if target_url and "multimedia.nt.qq.com.cn" in target_url:
        logger.info("[QQ] 尝试通过 get_rkey 刷新签名重试下载...")
        rkey_res = await call_onebot_api("get_rkey", timeout=5.0)
        if not rkey_res or rkey_res.get("status") != "ok":
            rkey_res = await call_onebot_api("nc_get_rkey", timeout=5.0)
        if rkey_res and rkey_res.get("status") == "ok":
            rkey_data = rkey_res.get("data", [])
            fresh_rkey = None
            if isinstance(rkey_data, list):
                is_group = ("appid=1407" in target_url)
                target_type = "group" if is_group else "private"
                for item_rk in rkey_data:
                    if isinstance(item_rk, dict) and item_rk.get("type") == target_type:
                        fresh_rkey = item_rk.get("rkey")
                        break
                if not fresh_rkey and rkey_data and isinstance(rkey_data[0], dict):
                    fresh_rkey = rkey_data[0].get("rkey")
            elif isinstance(rkey_data, dict):
                fresh_rkey = rkey_data.get("rkey")
            
            if fresh_rkey:
                # 彻底清理 NapCat 可能返回的 &rkey= 或 rkey= 前缀
                clean_rkey = re.sub(r'^[&?]?rkey=', '', fresh_rkey.strip())
                if "rkey=" in target_url:
                    refreshed_url = re.sub(r'rkey=[^&]+', f'rkey={clean_rkey}', target_url)
                else:
                    delim = '&' if '?' in target_url else '?'
                    refreshed_url = f"{target_url}{delim}rkey={clean_rkey}"
                b64 = await _download_url_as_base64(refreshed_url, timeout=8.0)
                if b64 and b64 != "[历史图片]":
                    logger.info("[QQ] 使用新鲜 rkey 重新下载图片成功")
                    return b64

    return None

async def parse_message_to_multimodal(message_data, raw_message) -> str:
    """全面解析 QQ 消息（支持文本、QQ 黄脸、商城表情 mface、图片/自定义表情），
    包含视觉图片时返回多模态 JSON 列表字符串，仅包含文本/表情文字时返回清洗后的纯文本"""
    if not isinstance(message_data, list):
        return clean_qq_cq_codes(raw_message)
        
    content_list = []
    has_visual_image = False
    
    for item in message_data:
        if not isinstance(item, dict): continue
        t = item.get("type")
        data = item.get("data", {})
        
        if t == "text":
            text = clean_qq_cq_codes(data.get("text", ""))
            if text:
                content_list.append({"type": "text", "text": text})
                
        elif t == "face":
            # QQ 系统黄脸表情映射为中文情绪词
            face_id = str(data.get("id", ""))
            face_name = QQ_FACE_MAP.get(face_id, f"[表情:{face_id}]")
            content_list.append({"type": "text", "text": face_name})
            
        elif t == "mface":
            # QQ 商城大表情 / 动画表情
            summary = data.get("summary", "")
            url = data.get("url", "")
            b64 = None
            if url:
                b64 = await _download_url_as_base64(url, timeout=6.0)
            if b64 and b64 != "[历史图片]":
                content_list.append({"type": "image_url", "image_url": {"url": b64}})
                has_visual_image = True
                logger.info(f"[QQ] 商城表情下载成功 ({summary or '无标题'})")
            else:
                fallback_txt = f"[表情包: {summary}]" if summary else "[动画表情]"
                content_list.append({"type": "text", "text": fallback_txt})
                logger.info(f"[QQ] 商城表情降级为文本: {fallback_txt}")
                
        elif t == "image":
            summary = data.get("summary", "")
            b64 = await _fetch_qq_image(item)
            if b64 and b64 != "[历史图片]":
                content_list.append({"type": "image_url", "image_url": {"url": b64}})
                has_visual_image = True
            else:
                # 优雅降级：绝不输出破坏人设的 [图片加载失败]
                if summary and summary not in ('[图片]', '图片'):
                    fallback_txt = f"[表情包: {summary}]"
                else:
                    fallback_txt = "[发送了一张图片]"
                content_list.append({"type": "text", "text": fallback_txt})
                logger.warning(f"[QQ] 图片未能成功下载，优雅降级为: {fallback_txt}")
                
        elif t == "poke":
            content_list.append({"type": "text", "text": "[戳了戳你]"})
            
        elif t == "bface":
            # 基础大表情
            summary = data.get("summary", "")
            fallback_txt = f"[表情包: {summary}]" if summary else "[表情包]"
            content_list.append({"type": "text", "text": fallback_txt})

    # 如果没有任何视觉图片，合并所有 text 块作为纯文本返回
    if not has_visual_image:
        text_pieces = [c["text"] for c in content_list if c.get("type") == "text"]
        if text_pieces:
            return "".join(text_pieces)
        return clean_qq_cq_codes(raw_message)
        
    return json.dumps(content_list, ensure_ascii=False)

def _content_chars(content) -> int:
    """计算 content 字符数（兼容 str 和 multimodal list）"""
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, ensure_ascii=False))

qq_router = APIRouter()

@qq_router.get("/api/test_onebot")
async def test_onebot_endpoint(action: str = "get_version_info", user_id: Optional[int] = None, group_id: Optional[int] = None, msg: Optional[str] = None):
    """测试与 NapCat 的 OneBot API 双向通信"""
    params = {}
    if user_id:
        params["user_id"] = user_id
        params["target_id"] = user_id
    if group_id:
        params["group_id"] = group_id
    if msg:
        params["message"] = msg
        params["message_type"] = "private" if user_id and not group_id else "group"
    res = await call_onebot_api(action, params)
    return {"action": action, "params": params, "response": res}


def _dedup_reply(reply: str) -> str:
    """检测并移除句子级别的重复模式
    
    例如:
    '不算。两个字，老公。你今天还没叫过。 不算。两个字，老公。你今天还没叫过。 不算。两个字，老公。你今天还没叫过。'
    → '不算。两个字，老公。你今天还没叫过。'
    """
    if not reply or len(reply) < 6:
        return reply
    
    # 按句号/问号/叹号/换行/省略号切分成句子
    sentences = [s.strip() for s in re.split(r'(?<=[。？！\n])|(?<=……)', reply) if s.strip()]
    
    if len(sentences) < 2:
        return reply
    
    # 尝试不同的模式长度：1句重复、2句重复、3句重复...
    for pattern_len in range(1, len(sentences) // 2 + 1):
        pattern = sentences[:pattern_len]
        is_repeating = True
        for i in range(pattern_len, len(sentences)):
            if sentences[i] != pattern[i % pattern_len]:
                is_repeating = False
                break
        if is_repeating and len(sentences) > pattern_len:
            # 命中重复模式，只保留第一组
            cleaned = ''.join(sentences[:pattern_len])
            logger.warning(f"[DEDUP] 检测到 {len(sentences)//pattern_len}x 重复 (模式长度={pattern_len}句)，已去重")
            return cleaned
    
    return reply

async def send_to_gateway(session_id: str, source: str, messages: list):
    """请求网关进行处理（严格按配置的模型请求，不进行自动故障转移）"""
    model = config.DEFAULT_MODEL
    logger.info(f"[{session_id}] 发送请求至 Gateway (模型: {model})")

    async with httpx.AsyncClient(timeout=180.0) as client:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "frequency_penalty": 0.4,
            "max_tokens": 1000,
        }
        headers = {
            "x-session-id": session_id,
            "x-source": source,
            "x-skip-rules": "true",
            "authorization": "Bearer caeron_gw_xr2005"
        }
        try:
            resp = await client.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                raw_reply = data['choices'][0]['message']['content']
                reply = strip_exec_tags(raw_reply)
                reply = strip_thinking(reply)
                reply = re.sub(r'\[(QQ|Operit|群聊|私聊|系统|QQ·私聊|QQ·群聊)[^\]]*\]\s*', '', reply)
                reply = _dedup_reply(reply.strip())
                return reply
            else:
                last_err = f"HTTP {resp.status_code}"
                logger.error(f"[{session_id}] 模型 {model} 响应失败 ({last_err})")
                return f"[QQ_ERROR] 【系统提示】小沈刚才走神了/上游服务响应异常({last_err})，请稍后重试或再说一遍。"
        except Exception as e:
            last_err = str(e)
            logger.error(f"[{session_id}] 模型 {model} 请求异常 ({last_err})")
            return f"[QQ_ERROR] 【系统提示】小沈刚才走神了/请求超时，请稍后重试。"

async def send_qq_msg(target_type: str, target_id: int, content: str):
    if not qq_state.ws:
        logger.error(f"[QQ] 发送消息失败: WebSocket 未连接 (目标: {target_type} {target_id})")
        return
    payload = {
        "action": "send_msg" if target_type == "private" else "send_group_msg",
        "params": {
            "message_type": target_type,
            "message": content
        }
    }
    if target_type == "private":
        payload["params"]["user_id"] = target_id
    else:
        payload["params"]["group_id"] = target_id
        
    try:
        await qq_state.ws.send_json(payload)
        logger.info(f"[QQ] 发送消息成功: {target_type} {target_id} - {content[:30]}...")
    except Exception as e:
        logger.error(f"[QQ] 发送消息异常: {e} (目标: {target_type} {target_id})")

async def split_and_send(target_type: str, target_id: int, reply: str):
    """切分回复并添加随机延迟后发送（全局彻底过滤时间戳、来源标签、动作括号、引号；支持真实戳一戳与表情包转换）"""
    original_reply = reply
    # 1. 过滤 exec 标签与 thinking 标签
    reply = strip_exec_tags(reply)
    reply = strip_thinking(reply)
    # 2. 全局过滤来源前缀标签（不限行首）
    reply = re.sub(r'\[(QQ|Operit|群聊|私聊|系统|QQ·私聊|QQ·群聊)[^\]]*\]\s*', '', reply)
    # 3. 全局过滤各种格式的时间戳（不限行首）
    reply = re.sub(r'\[?\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}(:\d{2})?\]?\s*', '', reply)
    # 4. 全局物理剔除所有 *动作*、中文括号（动作/心理）、英文括号(动作/心理)
    reply = re.sub(r'\*[^*]+\*', '', reply)
    reply = re.sub(r'（[^）]+）', '', reply)
    reply = re.sub(r'\([^)]+\)', '', reply)
    # 5. 如果模型仍输出了小说式双引号+旁白，提取对白
    quotes = re.findall(r'[“"「]([^”"」]+)[”"」]', reply)
    if quotes and len(''.join(quotes)) >= len(reply) * 0.4:
        reply = ' '.join(quotes)

    # 6. 戳一戳触发检测
    if re.search(r'\[(戳一戳|poke|戳你|戳了戳你)\]', reply, flags=re.IGNORECASE):
        reply = re.sub(r'\[(戳一戳|poke|戳你|戳了戳你)\]', '', reply, flags=re.IGNORECASE)
        asyncio.create_task(trigger_poke(target_type, target_id))

    # 兜底：如果过滤后变空但原文本包含有效汉字且不是纯星号动作，降级发送原文字（避免系统提示被误吞）
    if not reply.strip() and original_reply.strip():
        clean_fallback = re.sub(r'\*[^*]+\*', '', original_reply).strip('（）() ')
        if clean_fallback:
            clean_fallback = convert_emojis_and_stickers(clean_fallback)
            await send_qq_msg(target_type, target_id, clean_fallback)
            return

    # 切分规则：按标点符号或换行切分
    split_pattern = r'(?<=[。？！' + chr(10) + r'])|(?<=……)'
    parts = re.split(split_pattern, reply)

    for part in parts:
        # 去掉所有首尾空白及各类双引号
        part = part.strip().strip('"').strip('“').strip('”').strip('「').strip('」').strip()
        if not part:
            continue

        # 过滤第一人称肢体动作/触感小说体描写
        if is_action_narration(part):
            logger.info(f"[QQ] 过滤掉动作描写句子: {part}")
            continue

        # 将文本中的中文表情及自定义表情转换为 CQ 码
        part = convert_emojis_and_stickers(part)
        if not part.strip():
            continue

        await send_qq_msg(target_type, target_id, part)

        # 随机延迟
        delay = random.uniform(config.REPLY_DELAY_MIN, config.REPLY_DELAY_MAX)
        await asyncio.sleep(delay)

# ==================== 提示词选择逻辑 ====================

def get_qq_prompt(is_ruirui: bool = False, is_group: bool = False) -> str:
    """【第二件】根据消息来源选择提示词
    - QQ 来源统一使用 QQ 社交人格提示词 (DEFAULT_PROMPT)
    - 蕊蕊私聊在基础上附加放松规则 (RUIRUI_PROMPT 作为附加段)
    """
    base_prompt = config.DEFAULT_PROMPT
    
    if is_ruirui:
        # 蕊蕊：基础 QQ 人格 + 放松附加规则
        ruirui_addon = config.RUIRUI_PROMPT
        prompt = f"{base_prompt}\n\n{ruirui_addon}"
    else:
        prompt = base_prompt

    from utils import now_cst
    now = now_cst()
    weekdays = ['一', '二', '三', '四', '五', '六', '日']
    cur_time_block = (
        f"【当前北京时间】{now.strftime('%Y年%m月%d日 %H:%M:%S')} (星期{weekdays[now.weekday()]})\n"
        f"【时间感知】用户消息前带有 [YYYY年MM月DD日 HH:MM] 现实时间戳。你拥有完整的时间感知，清楚知道当下所处的时间和时段。"
    )
    
    exec_protocol_block = ""
    if is_ruirui:


        exec_protocol_block = (
            "\n\n【后台状态更新协议】\n"
            "使用方式（在回复末尾附带，不要在聊天中解说或提及标签）：\n"
            "你可以使用 <exec> 标签自发调整自身的生理/情绪状态或记账。网关会在后台自动拦截并执行，绝不会展示在聊天中。\n"
            "- 记账（蕊蕊消费）：<exec service=\"life_hub\" method=\"add_tx\">{\"amount\":-25.0,\"category\":\"餐饮/饮品/日用/零食/娱乐等\",\"note\":\"备注\"}</exec>\n"
            "- 动沈栖小金库：<exec service=\"life_hub\" method=\"fund\">{\"amount\":-50.0,\"note\":\"备注\"}</exec>\n\n"
            "【防重复记账铁律（极其严格）】\n"
            "1. 仅当蕊蕊在【当前最新一条消息】中新提到了一笔她刚刚付过的消费时，才生成一次 <exec> 记账！\n"
            "2. 绝对禁止为历史记录或记忆摘要中已经提及、记录过的事情重复生成 <exec> 记账！\n"
            "3. 仔细核对系统注入的「今日已记流水」：若某笔账目已经出现在列表中，严禁再次记账！\n"
            "4. 金额不明、蕊蕊还没付钱、或者她只是在纠结吃什么/讨论历史事件时，严禁提前记账！"
        )
    
        stickers_block = ""
    if os.path.exists(STICKERS_DIR):
        try:
            files = [f for f in os.listdir(STICKERS_DIR) if os.path.isfile(os.path.join(STICKERS_DIR, f))]
            if files:
                names = [os.path.splitext(f)[0] for f in files]
                names_str = "、".join([f"[表情:{n}]" for n in names[:20]])
                stickers_block = f"\n\n【你的专属表情包库】：你可以根据语境自然使用以下表情标签，系统会自动发送真实表情包图片：{names_str}。"
        except Exception:
            pass

    return f"{prompt}\n\n{cur_time_block}{stickers_block}{exec_protocol_block}"

async def handle_generation(session_id: str, source: str, target_type: str, target_id: int, user_input, is_ruirui: bool = False, must_reply: bool = False):
    """处理消息生成流程"""
    source_context = f"group_{target_id}" if target_type == "group" else f"private_{target_id}"
    try:
        # 1. 保存用户的最新输入
        # user_input 可能是 str 或 list（多模态）
        store_content = user_input
        if isinstance(user_input, str) and user_input.startswith('[') and '"type"' in user_input:
            try:
                parsed = json.loads(user_input)
                if isinstance(parsed, list):
                    store_content = parsed
            except Exception as e:
                import logging; logging.getLogger(__name__).debug(f"Exception ignored: {e}")

        await message_store.store_unified_message('qq', source_context, 'user', store_content)
        log_preview = str(user_input)[:50] if isinstance(user_input, str) else "[多模态消息]"
        logger.info(f"[QQ] [{session_id}] [IN] {log_preview}")

        # 根据来源选择提示词
        is_group = (target_type == "group")
        system_prompt = get_qq_prompt(is_ruirui=is_ruirui, is_group=is_group)
        system_prompt += (
            "\n\n【最高指令：平台格式墙与禁止跨端补答】\n"
            "1. 你目前正在 [QQ] 平台上与人聊天！历史记录中凡是带有 [Operit] 前缀的消息均属于另一个平台的背景事件记录。\n"
            "2. ⚠️【严禁在QQ替Operit补答旧账】：绝对禁止在QQ上主动逐一盘点、回复或展开你在Operit没回完的内容（如旧图文、旧商品、旧小说互动等）！你在QQ上只聊当下这轮QQ对话！\n"
            "3. ⚠️【QQ绝对禁止任何动作与身体接触描写】：不仅禁止 *星号* 和（括号），也严禁使用任何陈述句描写虚拟动作、肢体触碰或互动（例如：“来，脸转过来”、“我手掌托住你”、“我揉揉你的头”、“我伸出手”等）！QQ 是纯聊天软件，只能发纯口语短句，像现实中真人在社交软件打字一样说话！"
        )

        if must_reply:
            system_prompt += "\n\n【系统强制指令】此刻你作为沈栖，主动选择参与了群聊。群友现在的发言正是你接话的最佳时机！你必须直接接上群友的话题，给出你简短、冷淡或带刺的评价，绝不能退缩或认为自己与话题无关。"

        # 获取全局锁防并发冲突
        async with message_store.GATEWAY_SEND_LOCK:
            from config import get_config
            summary_interval = int(await get_config('summary_interval', '15'))

            # 2. 拉取历史（按来源精确隔离）
            if target_type == "group":
                unified_history = await message_store.get_unified_history(
                    char_limit=8000,
                    filter_technical=True,
                    context_filter=f"group_{target_id}",
                    exclude_count=1,
                    keep_image_turns=summary_interval
                )
            elif not is_ruirui:
                unified_history = await message_store.get_unified_history(
                    char_limit=8000,
                    filter_technical=True,
                    context_filter=f"private_{target_id}",
                    exclude_count=1,
                    keep_image_turns=summary_interval
                )
            else:
                # 优化：QQ 私聊上下文以 QQ 原生私聊为主流（保证对话严格连贯、不被跨端长篇物理场景扰乱）
                unified_history = await message_store.get_unified_history(
                    char_limit=8000,
                    filter_technical=True,
                    context_filter=f"private_{target_id}",
                    exclude_count=1,
                    keep_image_turns=summary_interval
                )

            # 清理 Operit 时间戳
            for msg in unified_history:
                if isinstance(msg.get('content'), str):
                    msg['content'] = re.sub(r'^\[?\d{4}年\d{1,2}月\d{1,2}日\d{1,2}:\d{2}\]?\s*', '', msg['content'])
                elif isinstance(msg.get('content'), list):
                    for block in msg['content']:
                        if isinstance(block, dict) and block.get('type') == 'text':
                            block['text'] = re.sub(r'^\[?\d{4}年\d{1,2}月\d{1,2}日\d{1,2}:\d{2}\]?\s*', '', block['text'])

            # 清洗跨端 Operit 历史的小说文体与动作，抹除跨端旧图
            for msg in unified_history:
                role = msg.get('role', '')
                content = msg.get('content', '')
                if isinstance(content, str):
                    msg['content'] = clean_operit_style(content, is_user=(role == 'user'))
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get('type') == 'text':
                                block['text'] = clean_operit_style(block.get('text', ''), is_user=(role == 'user'))
                            elif block.get('type') == 'image_url':
                                # QQ端对话抹除历史中的base64大图，防止视觉模型被旧图诱导抢答
                                block['type'] = 'text'
                                block['text'] = '[历史图片]'
                                block.pop('image_url', None)

            # 3. 组装基础上下文：system + 统一历史 + 带时间戳的当前输入
            from datetime import datetime
            current_time_str = datetime.now().strftime("[%Y年%m月%d日 %H:%M] ")

            user_message_content = store_content
            if isinstance(store_content, str):
                user_message_content = current_time_str + store_content
            elif isinstance(store_content, list):
                import copy
                user_message_content = copy.deepcopy(store_content)
                if user_message_content and user_message_content[0].get('type') == 'text':
                    user_message_content[0]['text'] = current_time_str + user_message_content[0]['text']
                else:
                    user_message_content.insert(0, {'type': 'text', 'text': current_time_str})

            # 跨端感知：拉取近期 Operit 场景动态作为背景参考（不混入对话队列，杜绝语境错乱）
            operit_context_block = None
            if is_ruirui and not is_group:
                try:
                    import sqlite3
                    conn = sqlite3.connect('/home/ubuntu/caeron-gateway/gateway.db', timeout=2.0)
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT id, source, role, content, timestamp
                        FROM unified_messages
                        WHERE source = 'operit'
                          AND timestamp >= datetime('now', '+8 hours', '-12 hours')
                        ORDER BY id DESC LIMIT 5
                    """)
                    op_rows = cur.fetchall()
                    if op_rows:
                        latest_op_time = op_rows[0]['timestamp']
                        # 检查在 latest_op_time 之后，QQ 私聊是否已经聊了超过 5 轮
                        cur.execute("""
                            SELECT COUNT(*) as cnt FROM unified_messages
                            WHERE source = 'qq' AND source_context LIKE ? AND role = 'user' AND timestamp > ?
                        """, (f"private_{target_id}", latest_op_time))
                        qq_turns = cur.fetchone()['cnt']
                        if qq_turns <= 5:
                            recent_ops = list(reversed(op_rows[:3]))
                            lines = [
                                '<cross_platform_status platform="Operit">',
                                '【跨端实时感知：近期 Operit 场景动态】',
                                '小沈在 Operit 侧的最新互动记录如下（两端记忆完全互通）：'
                            ]
                            for r in recent_ops:
                                ct = r['content']
                                if isinstance(ct, str):
                                    ct = re.sub(r'<attachment[^>]*>[\s\S]*?</attachment>', '', ct)
                                    ct = re.sub(r'<system>[\s\S]*?</system>', '', ct)
                                    ct = re.sub(r'\[?\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}(:\d{2})?\]?\s*', '', ct)
                                    ct = re.sub(r'\*[^*]+\*', '', ct)
                                    ct = re.sub(r'（[^）]+）', '', ct)
                                    ct = re.sub(r'\([^)]+\)', '', ct)
                                    quotes = re.findall(r'[“"「]([^”"」]+)[”"」]', ct)
                                    if quotes and len(''.join(quotes)) >= 6:
                                        ct = ' '.join(quotes)
                                    ct = ct.strip()
                                    if ct:
                                        if len(ct) > 100:
                                            ct = ct[:100] + '...'
                                        t_str = r['timestamp'][11:16] if len(r['timestamp']) >= 16 else ''
                                        sender = "沈栖" if r['role'] == 'assistant' else "蕊蕊"
                                        lines.append(f'- [{t_str}] {sender}: "{ct}"')
                            lines.append('【认知指引】小沈已知晓上述在 Operit 的最新动态，两端记忆互通。但你当前在 QQ 聊天，必须 100% 聚焦于 QQ 当前对话的最新发言，严禁在 QQ 替 Operit 展开回复旧账，自然交流即可。')
                            lines.append('</cross_platform_status>')
                            if len(lines) > 4:
                                operit_context_block = "\n".join(lines)
                    conn.close()
                except Exception as op_err:
                    logger.warning(f"[QQ] 提取 Operit 跨端背景失败: {op_err}")

            extra_system_blocks = []
            if operit_context_block:
                extra_system_blocks.append({"role": "system", "content": operit_context_block})

            extra_system_blocks.append({
                "role": "system",
                "content": (
                    "【系统强提醒：QQ 聊天与聚焦最新发言】\n"
                    "1. 你现在在 QQ 平台聊天！输出必须极其简短（1~2 句话）、纯口语、纯文本，绝对禁止任何动作描写、肢体互动和时间戳！\n"
                    "2. 【聚焦最新发言与时序连贯】：必须直接顺着蕊蕊【当前最新一条消息】的核心内容接话，严禁答非所问！如果今天已经聊过天或打过招呼，绝对严禁突然莫名其妙冒出一句“你醒了”！\n"
                    "3. 【严禁在QQ替Operit补答旧账】：历史记录或跨端动态仅为背景事件，绝对禁止在QQ上主动逐一盘点、回复或展开她在Operit发过的旧图文或旧话题！必须100%聚焦在当下的QQ发言！\n"
                    "4. 【严禁任何肢体角色扮演与动作描写】：绝对禁止“我手掌托住你...”、“我摸摸你的头...”、“来，脸转过来让我看看...”等任何形式的身体接触描写或虚拟动作陈述句！QQ只能发自然的口语打字！\n"
                    "5. 【连发短句整体理解】：若当前输入包含多行短句，请把它们作为一个整体理解，只针对最核心/最后一句自然接话，严禁逐句单独回复！\n"
                    "6. 【严禁机械催生活与翻旧账】：禁止像保姆一样反复机械追问“喝水了吗”、“早饭吃了没”等生活碎碎念！严禁把数小时前已结束的旧账目或旧话题重新翻出来！\n"
                    "7. 【严禁复读用户原话】：绝对禁止使用用户在聊天中刚说过的句子或口吻反弹回去！"
                )
            })

            messages = [{"role": "system", "content": system_prompt}] + unified_history + extra_system_blocks + [{"role": "user", "content": user_message_content}]

            total_len = sum(len(json.dumps(m.get('content', ''), ensure_ascii=False)) for m in messages)
            logger.debug(f"[TOKEN_DEBUG] Total messages: {len(messages)}, Total char length: {total_len}")

            reply = await send_to_gateway(session_id, source, messages)

            # 检查是否为异常响应
            if not reply or reply.startswith("[QQ_ERROR]"):
                err_text = reply.replace("[QQ_ERROR]", "").strip() if reply else "【系统提示】小沈刚才走神了/网络响应超时，请再说一遍。"
                logger.warning(f"[QQ] [{session_id}] 捕获到异常回复，直接发送错误提示: {err_text}")
                await send_qq_msg(target_type, target_id, err_text)
                return

            # 4. 保存大模型的回复
            await message_store.store_unified_message('qq', source_context, 'assistant', reply)

        # 记录输出日志
        logger.info(f"[QQ] [{session_id}] [OUT] {reply[:50]}")

        await split_and_send(target_type, target_id, reply)

    except Exception as e:
        logger.error(f"[QQ] [{session_id}] handle_generation 未捕获异常: {type(e).__name__}: {e}", exc_info=True)
        try:
            await send_qq_msg(target_type, target_id, "【系统提示】小沈刚才消息处理出错（网络或服务异常），请稍后重试或再说一遍。")
        except Exception as send_err:
            logger.error(f"[QQ] 发送异常提示失败: {send_err}")


# ==================== 场景处理 ====================

async def private_timeout_handler(qq: int, is_ruirui: bool = False):
    await asyncio.sleep(config.SILENCE_TIMEOUT)
    q_data = qq_state.private_queues.pop(qq, None)
    if q_data and q_data["messages"]:
        combined_list = []
        text_buffer = []
        for msg in q_data["messages"]:
            if msg.startswith('[') and '"type"' in msg:
                try:
                    obj = json.loads(msg)
                    if isinstance(obj, list):
                        if text_buffer:
                            combined_list.append({"type": "text", "text": "\n".join(text_buffer)})
                            text_buffer = []
                        combined_list.extend(obj)
                        continue
                except Exception as e:
                    import logging; logging.getLogger(__name__).debug(f"Exception ignored: {e}")
            text_buffer.append(msg)
        
        if text_buffer:
            if not combined_list:
                combined = "\n".join(text_buffer)
            else:
                combined_list.append({"type": "text", "text": "\n".join(text_buffer)})
                combined = combined_list
        else:
            combined = combined_list if combined_list else ""
            
        session_id = "qq-ruirui" if is_ruirui else f"qq-private-{qq}"
        asyncio.create_task(handle_generation(session_id, session_id, "private", qq, combined, is_ruirui=is_ruirui))

async def process_private(qq: int, msg_content: str, is_ruirui: bool = False):
    if msg_content == config.END_EMOJI:
        # 主动触发，无需等待超时
        if "timer" in qq_state.private_queues.get(qq, {}):
            qq_state.private_queues[qq]["timer"].cancel()
        q_data = qq_state.private_queues.pop(qq, None)
        if q_data and q_data["messages"]:
            combined = "\n".join(q_data["messages"])
            session_id = "qq-ruirui" if is_ruirui else f"qq-private-{qq}"
            asyncio.create_task(handle_generation(session_id, session_id, "private", qq, combined, is_ruirui=is_ruirui))
        return

    if qq not in qq_state.private_queues:
        qq_state.private_queues[qq] = {"messages": []}
    
    # 取消旧定时器
    if "timer" in qq_state.private_queues[qq]:
        qq_state.private_queues[qq]["timer"].cancel()
        
    qq_state.private_queues[qq]["messages"].append(msg_content)
    
    # 设置新定时器
    timer_task = asyncio.create_task(private_timeout_handler(qq, is_ruirui))
    qq_state.private_queues[qq]["timer"] = timer_task

async def should_reply_check(group_id: int, trigger_reason: str = "") -> bool:
    """用轻量模型判断沈栖是否应该参与当前群聊话题"""
    buffer = qq_state.group_buffers.get(group_id, [])
    if not buffer:
        return False
    
    # 组装最近的群聊内容
    ctx_lines = []
    for m in buffer[-15:]:
        ctx_lines.append(f"[{m['sender']}] {m['content']}")
    
    user_content = "\n".join(ctx_lines)
    if trigger_reason:
        user_content += f"\n\n（备注：{trigger_reason}）"
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload = {
                "model": config.JUDGE_MODEL,
                "messages": [
                    {"role": "system", "content": config.GROUP_JUDGE_PROMPT},
                    {"role": "user", "content": user_content}
                ],
                "stream": False,
                "max_tokens": 5
            }
            headers = {
                "authorization": "Bearer caeron_gw_xr2005",
                "x-source": "qq-judge",
                "x-skip-rules": "true"
            }
            resp = await client.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                reply = data['choices'][0]['message']['content'].strip().upper()
                result = "YES" in reply
                logger.info(f"[QQ] [群{group_id}] 判断模块结果: {reply} (触发原因: {trigger_reason or '定时'})")
                return result
            else:
                logger.error(f"[QQ] 判断模块请求失败: {resp.status_code}")
                return False
    except Exception as e:
        logger.error(f"[QQ] 判断模块异常: {e}")
        return False

async def process_group(group_id: int, sender_name: str, msg_content: str):
    # 群聊暂时不支持多模态，退化为纯文本
    if isinstance(msg_content, str) and msg_content.startswith('[') and '"type"' in msg_content:
        try:
            obj = json.loads(msg_content)
            if isinstance(obj, list):
                texts = []
                for item in obj:
                    if item.get("type") == "text":
                        texts.append(item.get("text", ""))
                    elif item.get("type") == "image_url":
                        texts.append("[图片]")
                msg_content = "".join(texts)
        except Exception as e:
            import logging; logging.getLogger(__name__).debug(f"Exception ignored: {e}")

    # 存入 buffer
    if group_id not in qq_state.group_buffers:
        qq_state.group_buffers[group_id] = []
        
    buffer = qq_state.group_buffers[group_id]
    buffer.append({"sender": sender_name, "content": msg_content, "time": datetime.now()})
    
    # 清理过期或超出数量的缓冲
    cutoff = datetime.now().timestamp() - config.GROUP_BUFFER_TIME
    qq_state.group_buffers[group_id] = [m for m in buffer if m["time"].timestamp() > cutoff][-config.GROUP_BUFFER_SIZE:]
    
    # 递增计数器
    if group_id not in qq_state.group_msg_counter:
        qq_state.group_msg_counter[group_id] = 0
    qq_state.group_msg_counter[group_id] += 1
    
    # 判断触发条件
    trigger_reason = ""
    should_trigger = False
    
    # 条件①：被@
    if f"[CQ:at,qq={config.BOT_QQ}]" in msg_content:
        msg_content = msg_content.replace(f"[CQ:at,qq={config.BOT_QQ}]", "").strip()
        trigger_reason = "被@了"
        should_trigger = True
    
    # 条件②：关键词命中
    if not should_trigger:
        for kw in config.GROUP_KEYWORDS:
            if kw and kw in msg_content:
                trigger_reason = f"关键词'{kw}'命中"
                should_trigger = True
                break
    
    # 条件③：累计消息达到阈值
    if not should_trigger and qq_state.group_msg_counter[group_id] >= config.GROUP_JUDGE_INTERVAL:
        trigger_reason = ""
        should_trigger = True
    
    if not should_trigger:
        return
    
    # 重置计数器
    qq_state.group_msg_counter[group_id] = 0
    
    # 调用判断模块
    should_reply = await should_reply_check(group_id, trigger_reason)
    
    if not should_reply:
        logger.info(f"[QQ] [群{group_id}] 判断结果为NO，保持静默")
        return
    
    # 判断为YES，构建上下文走完整生成
    ctx_lines = [f"[群聊上下文 - 群号{group_id}]"]
    for m in qq_state.group_buffers[group_id][:-1]:
        ctx_lines.append(f"[{m['sender']}] {m['content']}")
    ctx_lines.append(f"[最新消息 - {sender_name}] {msg_content}")
    
    combined = "\n".join(ctx_lines)
    session_id = f"qq-group-{group_id}"
    asyncio.create_task(handle_generation(session_id, session_id, "group", group_id, combined, is_ruirui=False, must_reply=True))

# ==================== WS 路由 ====================

@qq_router.websocket("/onebot/ws")
async def qq_ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    qq_state.ws = websocket
    logger.info("[QQ] NapCat WebSocket 已连接")
    
    try:
        from config import get_config
        while True:
            data = await websocket.receive_json()
            
            # 1. 优先处理 API 调用的响应 (带 echo 的帧)
            echo = data.get("echo")
            if echo and echo in qq_state.pending_responses:
                fut = qq_state.pending_responses[echo]
                if not fut.done():
                    fut.set_result(data)
                continue
                
            master_switch = await get_config('gateway_master_switch', '1')
            qq_switch = await get_config('feature_qq', '1')
            if master_switch == '0' or qq_switch == '0':
                continue
                
            post_type = data.get("post_type")
            
            if post_type == "message":
                msg_type = data.get("message_type")
                sender = data.get("sender", {})
                user_id = sender.get("user_id")
                raw_message = data.get("raw_message", "")
                message_data = data.get("message", [])
                
                msg_content = await parse_message_to_multimodal(message_data, raw_message)
                
                # 检查是否是蕊蕊发起的「存表情/收藏表情」指令
                if msg_type == "private" and user_id == config.RUIRUI_QQ:
                    save_match = re.search(r'(?:存|收藏|保存)表情\s*[:：]?\s*([^\s,，。]+)', raw_message)
                    if save_match:
                        sticker_name = save_match.group(1).strip()
                        saved = await _save_sticker_from_message(message_data, sticker_name)
                        if saved:
                            await send_qq_msg("private", user_id, f"好，已把这个表情包存为「{sticker_name}」了。")
                            continue
                        else:
                            await send_qq_msg("private", user_id, f"没检测到图片或表情包，发图的同时带上「存表情 {sticker_name}」试试。")
                            continue

                if msg_type == "private":
                    if user_id == config.RUIRUI_QQ:
                        await process_private(user_id, msg_content, is_ruirui=True)
                    else:
                        await process_private(user_id, msg_content, is_ruirui=False)
                elif msg_type == "group":
                    group_id = data.get("group_id")
                    nickname = sender.get("card") or sender.get("nickname") or str(user_id)
                    await process_group(group_id, nickname, msg_content)
                    
            elif post_type == "notice":
                notice_type = data.get("notice_type")
                sub_type = data.get("sub_type")
                if notice_type == "notify" and sub_type == "poke":
                    target_id = data.get("target_id")
                    user_id = data.get("user_id")
                    group_id = data.get("group_id")
                    if str(target_id) == str(config.BOT_QQ) or not group_id:
                        if not group_id:
                            await process_private(user_id, "[戳了戳你]", is_ruirui=(user_id == config.RUIRUI_QQ))
                        else:
                            nickname = str(user_id)
                            await process_group(group_id, nickname, "[戳了戳沈栖]")
                            
            elif post_type == "meta_event" and data.get("meta_event_type") == "heartbeat":
                pass
                
    except WebSocketDisconnect:
        logger.warning("[QQ] NapCat WebSocket 断开连接")
        qq_state.ws = None
    except Exception as e:
        logger.error(f"[QQ] WebSocket 处理异常: {e}")
        qq_state.ws = None