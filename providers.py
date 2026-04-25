"""
Caeron Gateway - 供应商管理模块
多供应商路由、优先级选择、健康检查、自动 fallback
"""

import json
import logging
import httpx
from database import get_db

logger = logging.getLogger(__name__)


class ProviderManager:
    """供应商管理器：负责供应商的增删改查、路由选择、健康状态管理"""

    async def get_provider(self, model: str) -> dict:
        """
        根据请求的 model 名获取最合适的供应商
        优先精确匹配 supported_models，找不到则返回优先级最高的供应商（通配）
        """
        db = await get_db()
        try:
            # 获取所有启用且健康的供应商，按优先级排序（数字越小越优先）
            cursor = await db.execute('''
                SELECT * FROM providers 
                WHERE is_enabled = 1 AND is_healthy = 1 
                ORDER BY priority ASC
            ''')
            providers = [dict(row) for row in await cursor.fetchall()]

            if not providers:
                raise Exception("没有可用的供应商")

            # 精确匹配：找到 supported_models 包含该 model 的供应商
            if model:
                for provider in providers:
                    supported = json.loads(provider.get('supported_models', '[]'))
                    if model in supported:
                        logger.info(f"模型 {model} 精确匹配供应商: {provider['name']}")
                        return provider

            # 通配：返回优先级最高的供应商
            logger.info(f"模型 {model} 无精确匹配，使用默认供应商: {providers[0]['name']}")
            return providers[0]
        finally:
            await db.close()

    async def get_provider_by_api_key(self, api_key: str) -> dict:
        """
        根据 API Key 精确匹配供应商
        用于 /v1/models 等需要根据客户端认证信息选择供应商的场景
        """
        db = await get_db()
        try:
            cursor = await db.execute(
                'SELECT * FROM providers WHERE api_key = ? AND is_enabled = 1',
                (api_key,)
            )
            row = await cursor.fetchone()
            if row:
                provider = dict(row)
                logger.info(f"API Key 匹配供应商: {provider['name']}")
                return provider
            return None
        finally:
            await db.close()

    async def get_fallback_providers(self, model: str, exclude_id: int) -> list:
        """获取 fallback 供应商列表（排除已尝试的供应商 ID）"""
        db = await get_db()
        try:
            cursor = await db.execute('''
                SELECT * FROM providers 
                WHERE is_enabled = 1 AND is_healthy = 1 AND id != ?
                ORDER BY priority ASC
            ''', (exclude_id,))
            return [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def mark_unhealthy(self, provider_id: int, error: str) -> None:
        """标记供应商为不健康"""
        db = await get_db()
        try:
            await db.execute(
                'UPDATE providers SET is_healthy = 0, last_error = ? WHERE id = ?',
                (error, provider_id)
            )
            await db.commit()
            logger.warning(f"供应商 {provider_id} 标记为不健康: {error}")
        finally:
            await db.close()

    async def mark_healthy(self, provider_id: int) -> None:
        """标记供应商为健康"""
        db = await get_db()
        try:
            await db.execute(
                'UPDATE providers SET is_healthy = 1, last_error = NULL WHERE id = ?',
                (provider_id,)
            )
            await db.commit()
        finally:
            await db.close()

    async def update_last_used(self, provider_id: int) -> None:
        """更新供应商最近使用时间"""
        db = await get_db()
        try:
            await db.execute(
                "UPDATE providers SET last_used_at = datetime('now') WHERE id = ?",
                (provider_id,)
            )
            await db.commit()
        finally:
            await db.close()

    async def list_providers(self) -> list:
        """列出所有供应商"""
        db = await get_db()
        try:
            cursor = await db.execute('SELECT * FROM providers ORDER BY priority ASC')
            return [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def add_provider(self, data: dict) -> int:
        """添加供应商，返回新供应商 ID"""
        db = await get_db()
        try:
            # 处理 supported_models：支持逗号分隔字符串或 JSON 数组
            models = data.get('supported_models', [])
            if isinstance(models, str):
                models = [m.strip() for m in models.split(',') if m.strip()]

            cursor = await db.execute('''
                INSERT INTO providers (name, api_base_url, api_key, supported_models, priority)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                data['name'],
                data['api_base_url'],
                data['api_key'],
                json.dumps(models),
                data.get('priority', 0)
            ))
            await db.commit()
            provider_id = cursor.lastrowid
            logger.info(f"添加供应商: {data['name']} (ID: {provider_id})")
            return provider_id
        finally:
            await db.close()

    async def update_provider(self, id: int, data: dict) -> None:
        """更新供应商信息"""
        db = await get_db()
        try:
            fields = []
            values = []

            # 逐字段更新
            for key in ['name', 'api_base_url', 'api_key', 'priority', 'is_enabled', 'is_healthy']:
                if key in data:
                    fields.append(f'{key} = ?')
                    values.append(data[key])

            # 特殊处理 supported_models
            if 'supported_models' in data:
                models = data['supported_models']
                if isinstance(models, str):
                    models = [m.strip() for m in models.split(',') if m.strip()]
                elif isinstance(models, list):
                    pass  # 已经是列表
                fields.append('supported_models = ?')
                values.append(json.dumps(models))

            if fields:
                values.append(id)
                await db.execute(
                    f'UPDATE providers SET {", ".join(fields)} WHERE id = ?',
                    values
                )
                await db.commit()
                logger.info(f"更新供应商 ID: {id}")
        finally:
            await db.close()

    async def delete_provider(self, id: int) -> None:
        """删除供应商"""
        db = await get_db()
        try:
            await db.execute('DELETE FROM providers WHERE id = ?', (id,))
            await db.commit()
            logger.info(f"删除供应商 ID: {id}")
        finally:
            await db.close()

    async def test_provider(self, id: int) -> dict:
        """测试供应商连通性（发一个 models 列表请求）"""
        db = await get_db()
        try:
            cursor = await db.execute('SELECT * FROM providers WHERE id = ?', (id,))
            row = await cursor.fetchone()
            if not row:
                return {'success': False, 'error': '供应商不存在'}
            provider = dict(row)
        finally:
            await db.close()

        # 构造 /v1/models 测试 URL
        base_url = provider['api_base_url'].rstrip('/')
        if base_url.endswith('/v1'):
            test_url = f"{base_url}/models"
        elif base_url.endswith('/chat/completions'):
            test_url = base_url.rsplit('/chat/completions', 1)[0] + '/models'
        else:
            test_url = f"{base_url}/v1/models"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    test_url,
                    headers={'Authorization': f'Bearer {provider["api_key"]}'}
                )
                if response.status_code == 200:
                    await self.mark_healthy(id)
                    result_data = response.json()
                    # 提取模型名列表
                    model_names = []
                    if 'data' in result_data:
                        model_names = [m.get('id', '') for m in result_data['data'][:20]]
                    return {
                        'success': True,
                        'message': f'连接成功，发现 {len(model_names)} 个模型',
                        'models': model_names
                    }
                else:
                    error = f"HTTP {response.status_code}: {response.text[:200]}"
                    await self.mark_unhealthy(id, error)
                    return {'success': False, 'error': error}
        except Exception as e:
            error = str(e)
            await self.mark_unhealthy(id, error)
            return {'success': False, 'error': error}