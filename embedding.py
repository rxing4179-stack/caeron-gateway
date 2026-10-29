import httpx
import numpy as np
import logging
from config import get_config

logger = logging.getLogger(__name__)

BGE_QUERY_INSTRUCTION = "为这个句子生成表示用于检索相关文章："

async def get_embedding(text: str, is_query: bool = False) -> list[float]:
    provider = await get_config('embedding_provider', 'siliconflow')
    model = await get_config('embedding_model', 'BAAI/bge-large-zh-v1.5')
    api_key = await get_config('embedding_api_key', '')
    
    if not api_key:
        logger.error("Embedding API key is not set in config.")
        return []
    
    input_text = text
    # BGE 非对称检索：检索 Query 侧增加官方 instruction 前缀，Passage 入库侧保持裸文本
    if is_query and 'bge' in model.lower():
        if not input_text.startswith(BGE_QUERY_INSTRUCTION):
            input_text = BGE_QUERY_INSTRUCTION + input_text

    # 截断过长文本防止413（bge-large-zh-v1.5 最大512 token，约400-500汉字）
    MAX_EMBED_CHARS = 400
    if len(input_text) > MAX_EMBED_CHARS:
        input_text = input_text[:MAX_EMBED_CHARS]
        logger.info(f"[EMBEDDING] 文本截断至 {MAX_EMBED_CHARS} 字符")

    url = "https://api.siliconflow.cn/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model,
        "input": input_text,
        "encoding_format": "float"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        logger.error(f"Error fetching embedding for '{input_text[:30]}...': {e}")
        return []

def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b:
        return 0.0
    a = np.array(vec_a)
    b = np.array(vec_b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))
