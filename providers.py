"""
Caeron Gateway - 供应商管理模块
多供应商路由、优先级选择、健康检查、自动 fallback
"""

import json
import logging
import httpx
import asyncio
from datetime import datetime, timedelta
from database import get_db

logger = logging.getLogger(__name__)

# 健康恢复配置
BASE_COOLDOWN_SECONDS = 30       # 基础冷却时间
MAX_COOLDOWN_SECONDS = 300       # 最大冷却时间（5分钟）
HEALTH_PROBE_INTERVAL = 60       # 后台健康探针间隔（秒）
MAX_FAIL_COUNT_BEFORE_PROBE = 10 # fail_count 超过此值时探针间隔翻倍


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
        """获取 fallback 供应商列表（排除已尝试的供应商 ID，且必须支持该模型）"""
        db = await get_db()
        try:
            cursor = await db.execute('''
                SELECT * FROM providers 
                WHERE is_enabled = 1 AND is_healthy = 1 AND id != ?
                ORDER BY priority ASC
            ''', (exclude_id,))
            all_providers = [dict(row) for row in await cursor.fetchall()]
            # 只返回 supported_models 包含该模型的供应商
            if model:
                filtered = []
                for p in all_providers:
                    supported = json.loads(p.get('supported_models', '[]'))
                    if model in supported:
                        filtered.append(p)
                if filtered:
                    return filtered
            # 没有精确匹配的 fallback 时返回空列表，不再通配
            return []
        finally:
            await db.close()

    async def mark_unhealthy(self, provider_id: int, error: str) -> None:
        """标记供应商为不健康，记录时间和累计失败次数"""
        db = await get_db()
        try:
            # 先获取当前fail_count
            cursor = await db.execute(
                'SELECT fail_count, is_healthy FROM providers WHERE id = ?',
                (provider_id,)
            )
            row = await cursor.fetchone()
            current_fail_count = (row['fail_count'] or 0) if row else 0
            was_healthy = row['is_healthy'] if row else 1

            new_fail_count = current_fail_count + 1

            # 只有从健康变不健康时才更新unhealthy_since
            if was_healthy:
                await db.execute(
                    '''UPDATE providers SET is_healthy = 0, last_error = ?, 
                       unhealthy_since = datetime('now'), fail_count = ? WHERE id = ?''',
                    (error, new_fail_count, provider_id)
                )
            else:
                await db.execute(
                    'UPDATE providers SET last_error = ?, fail_count = ? WHERE id = ?',
                    (error, new_fail_count, provider_id)
                )
            await db.commit()

            cooldown = self._get_cooldown_seconds(new_fail_count)
            logger.warning(
                f"供应商 {provider_id} 标记为不健康 (fail_count={new_fail_count}, "
                f"冷却期={cooldown}s): {error}"
            )
        finally:
            await db.close()

    async def mark_healthy(self, provider_id: int) -> None:
        """标记供应商为健康，重置失败计数"""
        db = await get_db()
        try:
            await db.execute(
                '''UPDATE providers SET is_healthy = 1, last_error = NULL, 
                   unhealthy_since = NULL, fail_count = 0 WHERE id = ?''',
                (provider_id,)
            )
            await db.commit()
            logger.info(f"供应商 {provider_id} 恢复健康")
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

    def _get_cooldown_seconds(self, fail_count: int) -> int:
        """计算冷却时间：指数退避，封顶MAX_COOLDOWN_SECONDS"""
        if fail_count <= 0:
            return BASE_COOLDOWN_SECONDS
        return min(BASE_COOLDOWN_SECONDS * (2 ** (fail_count - 1)), MAX_COOLDOWN_SECONDS)

    async def get_cooled_down_providers(self, model: str = None, exclude_ids: set = None) -> list:
        """
        获取冷却期已过的不健康供应商
        用于所有健康供应商都失败后的最后一搏
        """
        db = await get_db()
        try:
            cursor = await db.execute('''
                SELECT * FROM providers 
                WHERE is_enabled = 1 AND is_healthy = 0 AND unhealthy_since IS NOT NULL
                ORDER BY priority ASC
            ''')
            candidates = [dict(row) for row in await cursor.fetchall()]

            result = []
            now = datetime.utcnow()
            for p in candidates:
                if exclude_ids and p['id'] in exclude_ids:
                    continue
                # 模型过滤：cooled_down 供应商也必须支持该模型
                if model:
                    supported = json.loads(p.get('supported_models', '[]'))
                    if model not in supported:
                        continue
                # 解析 unhealthy_since 计算冷却是否到期
                try:
                    unhealthy_time = datetime.fromisoformat(p['unhealthy_since'])
                except (ValueError, TypeError):
                    # 解析失败说明数据异常，直接认为冷却到期
                    result.append(p)
                    continue

                cooldown = self._get_cooldown_seconds(p.get('fail_count', 1))
                if (now - unhealthy_time).total_seconds() >= cooldown:
                    result.append(p)
                    logger.info(
                        f"供应商 {p['name']} 冷却期已过 "
                        f"({cooldown}s, fail_count={p.get('fail_count', 0)})，可重新尝试"
                    )
            return result
        finally:
            await db.close()

    async def run_health_probe(self) -> None:
        """
        后台健康探针：主动探测不健康的供应商
        通过发送 /v1/models 请求检查连通性
        """
        db = await get_db()
        try:
            cursor = await db.execute('''
                SELECT * FROM providers 
                WHERE is_enabled = 1 AND is_healthy = 0
            ''')
            unhealthy = [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

        if not unhealthy:
            return

        logger.info(f"健康探针: 检测 {len(unhealthy)} 个不健康供应商")

        for provider in unhealthy:
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
                        await self.mark_healthy(provider['id'])
                        logger.info(
                            f"健康探针: 供应商 {provider['name']} 已恢复！"
                        )
                    else:
                        logger.debug(
                            f"健康探针: 供应商 {provider['name']} 仍不可用 "
                            f"(HTTP {response.status_code})"
                        )
            except Exception as e:
                logger.debug(
                    f"健康探针: 供应商 {provider['name']} 探测失败: {e}"
                )

    async def start_health_probe_loop(self) -> None:
        """启动后台健康探针循环"""
        logger.info(f"后台健康探针已启动 (间隔: {HEALTH_PROBE_INTERVAL}s)")
        while True:
            try:
                await asyncio.sleep(HEALTH_PROBE_INTERVAL)
                await self.run_health_probe()
            except asyncio.CancelledError:
                logger.info("后台健康探针已停止")
                break
            except Exception as e:
                logger.error(f"健康探针异常: {e}")
                await asyncio.sleep(HEALTH_PROBE_INTERVAL)

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