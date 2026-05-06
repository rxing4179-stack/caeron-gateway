import httpx
import numpy as np
import logging
from config import get_config

logger = logging.getLogger(__name__)

async def get_embedding(text: str) -> list[float]:
    provider = await get_config('embedding_provider', 'siliconflow')
    model = await get_config('embedding_model', 'BAAI/bge-large-zh-v1.5')
    api_key = await get_config('embedding_api_key', '')
    
    if not api_key:
        logger.error("Embedding API key is not set in config.")
        return []
    
    url = "https://api.siliconflow.cn/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "input": text,
        "encoding_format": "float"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        logger.error(f"Error fetching embedding for '{text}': {e}")
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
