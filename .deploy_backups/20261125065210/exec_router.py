"""
Caeron Gateway - <exec> 标签解析与路由系统
负责从 AI 回复中提取并剥离 <exec> 标签，路由分发到本地微服务，并管理下一轮执行结果的注入
"""

import re
import json
import logging
import httpx
from typing import List, Dict, Any, Tuple, Optional
from database import get_db

logger = logging.getLogger(__name__)

# 服务路由表：service_name -> 本地端口
ROUTER_TABLE: Dict[str, int] = {
    "life_hub": 8081,
    "simlife": 8082,
    "memory": 8081,
}

# 正则表达式
EXEC_TAG_REGEX = re.compile(
    r'<exec\s+service="([^"]+)"\s+method="([^"]+)">([\s\S]*?)</exec>',
    re.IGNORECASE
)

STRIP_EXEC_REGEX = re.compile(
    r'<exec\s+service="[^"]+"\s+method="[^"]+">[\s\S]*?</exec>\s*',
    re.IGNORECASE
)


def extract_exec_tags(text: str) -> List[Dict[str, Any]]:
    """
    从完整文本中提取所有 <exec> 标签块
    返回列表: [{'service': '...', 'method': '...', 'payload': {...}, 'raw': '...'}]
    """
    if not text or '<exec' not in text:
        return []

    blocks = []
    for match in EXEC_TAG_REGEX.finditer(text):
        service = match.group(1).strip()
        method = match.group(2).strip()
        payload_raw = match.group(3).strip()
        payload = {}
        if payload_raw:
            try:
                payload = json.loads(payload_raw)
            except Exception as e:
                logger.warning(f"[EXEC_ROUTER] JSON 解析失败 ({service}.{method}): {payload_raw} -> {e}")
                # 尝试修复一些常见的 json 格式问题或保持为空
                payload = {"raw_text": payload_raw}

        blocks.append({
            "service": service,
            "method": method,
            "payload": payload,
            "raw": match.group(0)
        })
    return blocks


def strip_exec_tags(text: str) -> str:
    """
    从文本中完全剔除所有 <exec>...</exec> 标签块
    """
    if not text or '<exec' not in text:
        return text
    return STRIP_EXEC_REGEX.sub('', text).strip()


class ExecTagStreamFilter:
    """
    流式传输过滤器（透传模式）：
    1. 流式输出原样透传给客户端（不剥离 <exec> 块），让前端实时可见
    2. 在内部累积并解析所有 <exec> 块到 self.exec_blocks 中供网关路由执行
    """
    def __init__(self):
        self.buffer = ""
        self.exec_blocks: List[Dict[str, Any]] = []
        self._parsed_raw_set = set()

    def process(self, chunk: str) -> str:
        """
        输入流式片段，原样透传返回文本片段，同时在内部累积并捕获 exec 块
        """
        if not chunk:
            return ""

        self.buffer += chunk

        # 实时检测是否有闭合的 </exec> 标签
        if "</exec>" in self.buffer.lower():
            blocks = extract_exec_tags(self.buffer)
            for b in blocks:
                b_raw = b.get("raw", "")
                if b_raw and b_raw not in self._parsed_raw_set:
                    self._parsed_raw_set.add(b_raw)
                    self.exec_blocks.append(b)
                    logger.info(f"[EXEC_STREAM] 捕获到 exec 块: {b['service']}.{b['method']}")

        return chunk

    def flush(self) -> str:
        """
        流结束时调用，确保所有 exec 块均已捕获，返回空字符串（内容已实时透传）
        """
        if self.buffer and "<exec" in self.buffer:
            blocks = extract_exec_tags(self.buffer)
            for b in blocks:
                b_raw = b.get("raw", "")
                if b_raw and b_raw not in self._parsed_raw_set:
                    self._parsed_raw_set.add(b_raw)
                    self.exec_blocks.append(b)
                    logger.info(f"[EXEC_STREAM_FLUSH] 最终捕获到 exec 块: {b['service']}.{b['method']}")
        return ""


async def execute_and_record(service: str, method: str, payload: dict, conversation_id: Optional[str] = None) -> str:
    """
    向目标微服务发起 HTTP POST 请求并记录执行结果
    返回注入格式的 result_text
    """
    port = ROUTER_TABLE.get(service)
    is_error = 0
    result_data: Dict[str, Any] = {}

    if not port:
        logger.warning(f"[EXEC_ROUTER] 未知服务: {service}")
        is_error = 1
        result_data = {"ok": False, "msg": f"unknown service: {service}"}
        result_text = (
            f'[exec_result service="{service}" method="{method}" error="true"]\n'
            f'{json.dumps(result_data, ensure_ascii=False)}\n'
            f'[/exec_result]'
        )
    else:
        url = f"http://127.0.0.1:{port}/api/{method}"
        logger.info(f"[EXEC_ROUTER] 正在调用服务 {service}.{method} -> {url}, payload: {payload}")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    try:
                        result_data = resp.json()
                        if isinstance(result_data, dict) and not result_data.get("ok", True):
                            is_error = 1
                    except Exception:
                        result_data = {"ok": True, "raw_response": resp.text}
                else:
                    is_error = 1
                    result_data = {"ok": False, "status": resp.status_code, "msg": resp.text[:200]}
        except httpx.ConnectError:
            is_error = 1
            result_data = {"ok": False, "msg": "connection refused"}
            logger.error(f"[EXEC_ROUTER] 服务连接失败 (Port {port}): connection refused")
        except httpx.TimeoutException:
            is_error = 1
            result_data = {"ok": False, "msg": "request timeout (5s)"}
            logger.error(f"[EXEC_ROUTER] 服务请求超时 (Port {port})")
        except Exception as e:
            is_error = 1
            result_data = {"ok": False, "msg": str(e)}
            logger.error(f"[EXEC_ROUTER] 服务调用异常 ({service}.{method}): {e}")

        err_attr = ' error="true"' if is_error else ''
        result_text = (
            f'[exec_result service="{service}" method="{method}"{err_attr}]\n'
            f'{json.dumps(result_data, ensure_ascii=False)}\n'
            f'[/exec_result]'
        )

    # 写入数据库 pending_exec_results 表
    try:
        db = await get_db()
        try:
            await db.execute(
                '''
                INSERT INTO pending_exec_results 
                (conversation_id, service, method, payload, result_text, is_error, consumed)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ''',
                (
                    conversation_id,
                    service,
                    method,
                    json.dumps(payload, ensure_ascii=False),
                    result_text,
                    is_error
                )
            )
            await db.commit()
            logger.info(f"[EXEC_ROUTER] 结果已入库 pending_exec_results (conv_id={conversation_id}, err={is_error})")
        finally:
            await db.close()
    except Exception as e:
        logger.error(f"[EXEC_ROUTER] 结果入库失败: {e}")

    return result_text


async def process_and_route(exec_blocks_or_text, conversation_id: Optional[str] = None):
    """
    处理一组 exec_blocks 或一段包含 <exec> 的文本，异步分发执行
    """
    if isinstance(exec_blocks_or_text, str):
        blocks = extract_exec_tags(exec_blocks_or_text)
    elif isinstance(exec_blocks_or_text, list):
        blocks = exec_blocks_or_text
    else:
        blocks = []

    if not blocks:
        return

    logger.info(f"[EXEC_ROUTER] 开始处理 {len(blocks)} 个 exec 块 (conv_id={conversation_id})")
    for b in blocks:
        service = b.get("service", "")
        method = b.get("method", "")
        payload = b.get("payload", {})
        try:
            await execute_and_record(service, method, payload, conversation_id)
        except Exception as e:
            logger.error(f"[EXEC_ROUTER] 执行 exec 块失败 ({service}.{method}): {e}")


async def get_and_consume_pending_results(conversation_id: Optional[str] = None) -> List[str]:
    """
    获取当前会话（或全局未消费）的执行结果，并原子标记为已消费 (consumed=1)
    """
    db = await get_db()
    try:
        if conversation_id:
            cursor = await db.execute(
                '''
                SELECT id, result_text FROM pending_exec_results 
                WHERE (conversation_id = ? OR conversation_id IS NULL OR conversation_id = '') 
                  AND consumed = 0 
                ORDER BY id ASC
                ''',
                (conversation_id,)
            )
        else:
            cursor = await db.execute(
                '''
                SELECT id, result_text FROM pending_exec_results 
                WHERE consumed = 0 
                ORDER BY id ASC
                '''
            )
        rows = await cursor.fetchall()
        if not rows:
            return []

        ids = [r["id"] for r in rows]
        results = [r["result_text"] for r in rows]

        # 批量更新为已消费
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"UPDATE pending_exec_results SET consumed = 1 WHERE id IN ({placeholders})",
            ids
        )
        await db.commit()
        logger.info(f"[EXEC_ROUTER] 消费了 {len(results)} 条 pending_exec_results (IDs: {ids})")
        return results
    except Exception as e:
        logger.error(f"[EXEC_ROUTER] 获取 pending_exec_results 异常: {e}")
        return []
    finally:
        await db.close()
