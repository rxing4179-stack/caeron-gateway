"""Caeron Gateway - 多级总结引擎

层级体系：
- 轮总（tag='round'）：每次Operit触发总结请求时生成，100字以内，记录最近一批消息的事实
- 日总（tag='daily'）：每天23:59从当天所有轮总提取，200字以内
- 周总（tag='weekly'）：每周日23:59从7天日总提取，300字以内
- 月总（tag='monthly'）：每月最后一天23:59从当月周总提取，400字以内

每层生成后，上一层原始材料标记为is_active=0（归档，不删除）。
上下文注入时，组装：最新轮总 + 最近日总 + 周总 + 月总。
"""

import json
import time
import logging
import httpx
from datetime import datetime, timedelta
from database import get_db
from config import get_config

logger = logging.getLogger(__name__)

# ===== 各级总结的系统提示词 =====

ROUND_SUMMARY_PROMPT = """你是轮次总结器。将本轮对话压缩为一条极短摘要，并在末尾标注分类。

硬性规则：
- 总字数不超过100字，绝对禁止超出
- 只记录客观事实变化：做了什么、改了什么、决定了什么、发生了什么
- 禁止出现：情感描写、氛围叙述、"正在进行中"等废话、任何标题/分隔线/格式标记
- 格式：纯文本，一段话，句号分隔
- 如果提供了上一轮总结，不要复述旧内容，只写本轮新增的事实
- 主语用"蕊蕊"和"沈栖"，不用"用户""助手"
- 最后一行必须单独写分类标签，格式为 [分类:xxx]，xxx只能是以下之一：技术、学习、日常
  - 技术：涉及编程、服务器、网关、代码、部署、debug等
  - 学习：涉及课程、考试、医学知识、作业、复习等
  - 日常：聊天、情感、生活、吃饭、购物、闲聊等

示例输出：
沈栖修复了供应商健康恢复机制，增加冷却期与探针。蕊蕊报告总结拦截仍偶发失效，沈栖定位到指纹匹配漏洞并加入调试日志。
[分类:技术]"""

DAILY_SUMMARY_PROMPT = """你是日总结器。将今天的所有轮总压缩为一条日度摘要。

硬性规则：
- 总字数不超过200字
- 按时间顺序归纳当天做了什么、解决了什么、遗留了什么
- 禁止复述每条轮总的原文，要合并同类事项
- 格式：纯文本，一段话，句号分隔
- 主语用蕊蕊和沈栖
"""

WEEKLY_SUMMARY_PROMPT = """你是周总结器。将本周的所有日总压缩为一条周度摘要。

硬性规则：
- 总字数不超过300字
- 按主题归纳本周的进展、变化、决定
- 格式：纯文本，一段话，句号分隔
- 主语用蕊蕊和沈栖
"""

MONTHLY_SUMMARY_PROMPT = """你是月总结器。将本月的所有周总压缩为一条月度摘要。

硬性规则：
- 总字数不超过400字
- 高度概括本月的关键里程碑和状态变化
- 格式：纯文本，一段话，句号分隔
- 主语用蕊蕊和沈栖
"""

# 各级配置
LEVEL_CONFIG = {
    'round': {'prompt': ROUND_SUMMARY_PROMPT, 'max_tokens': 200, 'level': 'round'},
    'daily': {'prompt': DAILY_SUMMARY_PROMPT, 'max_tokens': 400, 'level': 'daily'},
    'weekly': {'prompt': WEEKLY_SUMMARY_PROMPT, 'max_tokens': 600, 'level': 'weekly'},
    'monthly': {'prompt': MONTHLY_SUMMARY_PROMPT, 'max_tokens': 800, 'level': 'monthly'},
}


class MultiLevelSummarizer:
    """多级总结引擎"""
    
    def __init__(self):
        self.summary_model = "deepseek-ai/DeepSeek-V3.2"
        self.max_context_messages = 100
        self.max_content_chars = 30000
    
    async def _get_summary_provider(self) -> dict:
        """获取用于总结的供应商配置"""
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT api_base_url, api_key FROM providers WHERE name = '硅基流动' AND is_enabled = 1"
            )
            row = await cursor.fetchone()
            if row:
                return {"api_base": row["api_base_url"], "api_key": row["api_key"]}
            
            cursor = await db.execute(
                "SELECT api_base_url, api_key FROM providers WHERE supported_models LIKE '%deepseek%' AND is_enabled = 1 LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                return {"api_base": row["api_base_url"], "api_key": row["api_key"]}
            
            return None
        finally:
            await db.close()
    
    async def _call_llm(self, system_prompt: str, user_content: str, max_tokens: int = 200) -> str:
        """调用LLM生成总结"""
        provider = await self._get_summary_provider()
        if not provider:
            logger.error("[SUMMARIZER] 找不到可用的总结模型供应商")
            return None
        
        url = f"{provider['api_base']}/chat/completions"
        headers = {
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.summary_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.3,
            "max_tokens": max_tokens,
            "stream": False
        }
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                logger.info(
                    f"[SUMMARIZER] LLM调用成功, "
                    f"prompt_tokens={usage.get('prompt_tokens', '?')}, "
                    f"completion_tokens={usage.get('completion_tokens', '?')}"
                )
                return content
            except Exception as e:
                logger.error(f"[SUMMARIZER] 调用总结模型失败: {e}")
                return None
    
    # ==================== 轮总 ====================
    
    async def _get_latest_active_round_summary(self) -> str:
        """获取最近一条活跃的轮总内容（用于Operit即时返回）"""
        db = await get_db()
        try:
            cursor = await db.execute(
                """SELECT content FROM summaries 
                   WHERE tag = 'round' AND is_active = 1 
                   ORDER BY created_at DESC LIMIT 1"""
            )
            row = await cursor.fetchone()
            return row["content"] if row else None
        finally:
            await db.close()
    
    async def _get_latest_summary(self) -> str:
        """兼容旧接口：获取最近的轮总"""
        return await self._get_latest_active_round_summary()
    
    async def _get_global_messages(self, since_summary: bool = True) -> list:
        """从messages表按全局时间线拉取最近消息"""
        db = await get_db()
        try:
            time_filter = ""
            params = []
            
            if since_summary:
                cursor = await db.execute(
                    """SELECT created_at FROM summaries 
                       WHERE tag = 'round' AND is_active = 1 
                       ORDER BY created_at DESC LIMIT 1"""
                )
                row = await cursor.fetchone()
                if row:
                    time_filter = "WHERE m.created_at > ?"
                    params.append(row["created_at"])
            
            query = f"""
                SELECT m.role, m.content, m.created_at, m.conversation_id,
                       c.model
                FROM messages m
                LEFT JOIN conversations c ON m.conversation_id = c.conversation_id
                {time_filter}
                ORDER BY m.created_at ASC
                LIMIT ?
            """
            params.append(self.max_context_messages)
            
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()
    
    async def generate_global_summary(self) -> str:
        """生成轮总���������兼容旧接口名）"""
        return await self.generate_round_summary()
    
    async def generate_round_summary(self) -> str:
        """生成一条轮总"""
        logger.info("[SUMMARIZER] 开始生成轮总...")
        
        previous_summary = await self._get_latest_active_round_summary()
        messages = await self._get_global_messages(since_summary=True)
        
        if not messages and previous_summary:
            logger.info("[SUMMARIZER] 无新消息，返回上次轮总")
            return previous_summary
        
        if not messages and not previous_summary:
            messages = await self._get_global_messages(since_summary=False)
            if not messages:
                logger.info("[SUMMARIZER] 数据库中无消息，返回空摘要")
                return self._empty_summary()
        
        # 构建LLM输入
        formatted_messages = []
        total_chars = 0
        for msg in messages:
            content = msg["content"] or ""
            if len(content) > 2000:
                content = content[:1000] + "\n...[内容截断]...\n" + content[-500:]
            
            role_label = "蕊蕊" if msg["role"] == "user" else "沈栖"
            timestamp = msg["created_at"] or "?"
            line = f"[{timestamp}] {role_label}: {content}"
            total_chars += len(line)
            
            if total_chars > self.max_content_chars:
                formatted_messages.append("...[更早的消息已省略]...")
                break
            formatted_messages.append(line)
        
        user_parts = []
        if previous_summary:
            user_parts.append(f"上轮总结：{previous_summary}\n")
        user_parts.append(f"本轮消息（{len(messages)}条）：\n")
        user_parts.append("\n".join(formatted_messages))
        user_parts.append("\n用100字以内总结本轮新增事实。")
        
        config = LEVEL_CONFIG['round']
        summary = await self._call_llm(config['prompt'], "\n".join(user_parts), config['max_tokens'])
        
        if not summary:
            logger.warning("[SUMMARIZER] 轮总生成失败，fallback")
            return previous_summary or self._empty_summary()
        
        # 解析分类标签
        import re
        category = '日常'  # 默认
        cat_match = re.search(r'\[分类[:：](技术|学习|日常)\]', summary)
        if cat_match:
            category = cat_match.group(1)
            summary = re.sub(r'\s*\[分类[:：][^\]]*\]\s*', '', summary).strip()
        logger.info(f"[SUMMARIZER] 轮总分类: {category}")
        
        # 保存轮总
        await self._save_summary(
            tag='round',
            level='round',
            content=summary,
            msg_count=len(messages),
            period_start=messages[0]["created_at"] if messages else None,
            period_end=messages[-1]["created_at"] if messages else None,
            category=category
        )
        
        # 自动分配窗口：找到对应分类的最新窗口，把当前conversation_id assign过去
        await self._auto_assign_window(category)
        
        return summary
    
    # ==================== 日总 ====================
    
    async def generate_daily_summary(self, target_date: str = None) -> str:
        """生成日总。target_date格式: 'YYYY-MM-DD'，默认今天"""
        if not target_date:
            target_date = datetime.utcnow().strftime('%Y-%m-%d')
        
        logger.info(f"[SUMMARIZER] 开始生成日总: {target_date}")
        
        db = await get_db()
        try:
            # 拉取当天所有活跃的轮总
            cursor = await db.execute(
                """SELECT id, content, created_at FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at) = ?
                   ORDER BY created_at ASC""",
                (target_date,)
            )
            round_summaries = await cursor.fetchall()
        finally:
            await db.close()
        
        if not round_summaries:
            logger.info(f"[SUMMARIZER] {target_date} 无轮总，跳过日总生成")
            return None
        
        round_summaries = [dict(r) for r in round_summaries]
        logger.info(f"[SUMMARIZER] 找到 {len(round_summaries)} 条轮总")
        
        # 构建LLM输入
        lines = [f"日期: {target_date}"]
        lines.append(f"当天轮总（{len(round_summaries)}条）：")
        for rs in round_summaries:
            lines.append(f"[{rs['created_at']}] {rs['content']}")
        lines.append("\n请用200字以内总结今天的全部事实。")
        
        config = LEVEL_CONFIG['daily']
        summary = await self._call_llm(config['prompt'], "\n".join(lines), config['max_tokens'])
        
        if not summary:
            logger.error(f"[SUMMARIZER] 日总生成失败: {target_date}")
            return None
        
        # �����存日总
        await self._save_summary(
            tag='daily',
            level='daily',
            content=summary,
            period_start=f"{target_date} 00:00:00",
            period_end=f"{target_date} 23:59:59"
        )
        
        # 归档当天的轮总（标记为非活跃）
        db = await get_db()
        try:
            round_ids = [rs['id'] for rs in round_summaries]
            placeholders = ','.join(['?'] * len(round_ids))
            await db.execute(
                f"UPDATE summaries SET is_active = 0 WHERE id IN ({placeholders})",
                round_ids
            )
            await db.commit()
            logger.info(f"[SUMMARIZER] 已归档 {len(round_ids)} 条轮总")
        finally:
            await db.close()
        
        return summary
    
    # ==================== 周总 ====================
    
    async def generate_weekly_summary(self, week_end_date: str = None) -> str:
        """生成周总。week_end_date为周日日期，默认本周日"""
        if not week_end_date:
            today = datetime.utcnow()
            # 找到本周日
            days_until_sunday = 6 - today.weekday()  # weekday: 0=Mon, 6=Sun
            if days_until_sunday == 0 and today.hour >= 23:
                week_end = today
            else:
                week_end = today
            week_end_date = week_end.strftime('%Y-%m-%d')
        
        week_start_date = (datetime.strptime(week_end_date, '%Y-%m-%d') - timedelta(days=6)).strftime('%Y-%m-%d')
        
        logger.info(f"[SUMMARIZER] 开始生成周总: {week_start_date} ~ {week_end_date}")
        
        db = await get_db()
        try:
            cursor = await db.execute(
                """SELECT id, content, created_at FROM summaries
                   WHERE tag = 'daily' AND is_active = 1
                   AND date(created_at) BETWEEN ? AND ?
                   ORDER BY created_at ASC""",
                (week_start_date, week_end_date)
            )
            daily_summaries = await cursor.fetchall()
        finally:
            await db.close()
        
        if not daily_summaries:
            logger.info(f"[SUMMARIZER] {week_start_date}~{week_end_date} 无日总，跳过周总")
            return None
        
        daily_summaries = [dict(r) for r in daily_summaries]
        logger.info(f"[SUMMARIZER] 找到 {len(daily_summaries)} 条日总")
        
        lines = [f"周期: {week_start_date} ~ {week_end_date}"]
        lines.append(f"本周日总（{len(daily_summaries)}条）：")
        for ds in daily_summaries:
            lines.append(f"[{ds['created_at']}] {ds['content']}")
        lines.append("\n请用300字以内总结本周的全部事实。")
        
        config = LEVEL_CONFIG['weekly']
        summary = await self._call_llm(config['prompt'], "\n".join(lines), config['max_tokens'])
        
        if not summary:
            logger.error(f"[SUMMARIZER] 周总生成失败")
            return None
        
        await self._save_summary(
            tag='weekly',
            level='weekly',
            content=summary,
            period_start=f"{week_start_date} 00:00:00",
            period_end=f"{week_end_date} 23:59:59"
        )
        
        # 归档本周日总
        db = await get_db()
        try:
            daily_ids = [ds['id'] for ds in daily_summaries]
            placeholders = ','.join(['?'] * len(daily_ids))
            await db.execute(
                f"UPDATE summaries SET is_active = 0 WHERE id IN ({placeholders})",
                daily_ids
            )
            await db.commit()
            logger.info(f"[SUMMARIZER] 已归档 {len(daily_ids)} 条日总")
        finally:
            await db.close()
        
        return summary
    
    # ==================== 月总 ====================
    
    async def generate_monthly_summary(self, year_month: str = None) -> str:
        """生成月总。year_month格式: 'YYYY-MM'，默认本月"""
        if not year_month:
            year_month = datetime.utcnow().strftime('%Y-%m')
        
        month_start = f"{year_month}-01"
        # 计算月末
        year, month = int(year_month[:4]), int(year_month[5:7])
        if month == 12:
            next_month_start = f"{year+1}-01-01"
        else:
            next_month_start = f"{year}-{month+1:02d}-01"
        month_end = (datetime.strptime(next_month_start, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
        
        logger.info(f"[SUMMARIZER] 开始生成月总: {month_start} ~ {month_end}")
        
        db = await get_db()
        try:
            cursor = await db.execute(
                """SELECT id, content, created_at FROM summaries
                   WHERE tag = 'weekly' AND is_active = 1
                   AND date(created_at) BETWEEN ? AND ?
                   ORDER BY created_at ASC""",
                (month_start, month_end)
            )
            weekly_summaries = await cursor.fetchall()
        finally:
            await db.close()
        
        if not weekly_summaries:
            logger.info(f"[SUMMARIZER] {year_month} 无周总，跳过月总")
            return None
        
        weekly_summaries = [dict(r) for r in weekly_summaries]
        logger.info(f"[SUMMARIZER] 找到 {len(weekly_summaries)} 条周总")
        
        lines = [f"月份: {year_month}"]
        lines.append(f"本月周总（{len(weekly_summaries)}条）：")
        for ws in weekly_summaries:
            lines.append(f"[{ws['created_at']}] {ws['content']}")
        lines.append("\n请用400字以内总结本月的全部事实。")
        
        config = LEVEL_CONFIG['monthly']
        summary = await self._call_llm(config['prompt'], "\n".join(lines), config['max_tokens'])
        
        if not summary:
            logger.error(f"[SUMMARIZER] 月总生成失败")
            return None
        
        await self._save_summary(
            tag='monthly',
            level='monthly',
            content=summary,
            period_start=f"{month_start} 00:00:00",
            period_end=f"{month_end} 23:59:59"
        )
        
        # 归档本月周总
        db = await get_db()
        try:
            weekly_ids = [ws['id'] for ws in weekly_summaries]
            placeholders = ','.join(['?'] * len(weekly_ids))
            await db.execute(
                f"UPDATE summaries SET is_active = 0 WHERE id IN ({placeholders})",
                weekly_ids
            )
            await db.commit()
            logger.info(f"[SUMMARIZER] 已归档 {len(weekly_ids)} 条周总")
        finally:
            await db.close()
        
        return summary
    
    # ==================== 上下文组装 ====================
    
    async def get_context_summary(self) -> str:
        """组装用于注入上下文的完整记忆摘要
        
        注入策略（时间窗口全量注入 + 窗口关闭后压缩替代）：
        - 月总：所有活跃的月总（覆盖历史月份）
        - 周总：当月所有活跃的周总（月末压缩为月总后归档）
        - 日总：当周所有活跃的日总（周末压缩为周总后归档）
        - 轮总：当天所有活跃的轮总（日末压缩为日总后归档）
        """
        db = await get_db()
        try:
            parts = []
            
            # 月总：所有活跃的（每月一条，覆盖长期记忆）
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'monthly' AND is_active = 1 ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            if rows:
                monthly_parts = [r['content'] for r in rows]
                parts.append(f"[长期记忆·月度] {'；'.join(monthly_parts)}")
            
            # 周总：当月所有活跃的（月末归档后由月总替代）
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'weekly' AND is_active = 1 ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            if rows:
                weekly_parts = [r['content'] for r in rows]
                parts.append(f"[本月记忆·周度({len(rows)}条)] {'；'.join(weekly_parts)}")
            
            # 日总：当周所有活跃的（周末归档后由周总替代）
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'daily' AND is_active = 1 ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            if rows:
                daily_parts = [r['content'] for r in rows]
                parts.append(f"[本周记忆·日度({len(rows)}条)] {'；'.join(daily_parts)}")
            
            # 轮总：当天所有活跃的（日末归档后由日总替代）
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'round' AND is_active = 1 ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            if rows:
                round_parts = [r['content'] for r in rows]
                parts.append(f"[今日记忆·轮次({len(rows)}条)] {'；'.join(round_parts)}")
            
            if not parts:
                return None
            
            return "\n".join(parts)
        finally:
            await db.close()
    
    # ==================== 通用工具方法 ====================
    
    async def _save_summary(self, tag: str, level: str, content: str,
                            msg_count: int = 0, period_start: str = None, period_end: str = None,
                            category: str = None):
        """保存总结到数据库"""
        db = await get_db()
        try:
            await db.execute(
                """INSERT INTO summaries 
                   (conversation_id, level, tag, content, message_range_start, message_range_end,
                    period_start, period_end, is_active, token_count, category)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                ('_global_', level, tag, content, 0, msg_count,
                 period_start, period_end, len(content), category)
            )
            await db.commit()
            logger.info(f"[SUMMARIZER] {tag}已保存 ({len(content)} 字符, 分类={category})")
        except Exception as e:
            logger.error(f"[SUMMARIZER] 保存{tag}失败: {e}")
        finally:
            await db.close()
    
    # 分类 -> 窗口名前缀映射
    CATEGORY_WINDOW_MAP = {
        '技术': '技术窗',
        '学习': '学习窗',
        '日常': '主窗口',
    }
    
    async def _auto_assign_window(self, category: str, conversation_id: str = None):
        """根据分类标签，把对话迁移到正确窗口（从默认主窗口迁到技术/学习窗）
        同时批量清理所有 window_id=NULL 的对话到默认主窗口"""
        db = await get_db()
        try:
            # Step 1: 批量清理所有 window_id=NULL 的对话 -> 分配到最新主窗口
            cursor = await db.execute(
                "SELECT id FROM windows WHERE name LIKE '主窗口%' ORDER BY id DESC LIMIT 1"
            )
            default_window = await cursor.fetchone()
            if default_window:
                default_wid = default_window[0]
                result = await db.execute(
                    "UPDATE conversations SET window_id = ? WHERE window_id IS NULL",
                    (default_wid,)
                )
                if result.rowcount > 0:
                    logger.info(f"[SUMMARIZER] 批量分配 {result.rowcount} 个未归类对话到主窗口 (id={default_wid})")
            
            # Step 2: 如果分类不是日常，把当前对话迁移到对应窗口
            prefix = self.CATEGORY_WINDOW_MAP.get(category)
            if prefix and prefix != '主窗口' and conversation_id:
                cursor = await db.execute(
                    "SELECT id, name FROM windows WHERE name LIKE ? ORDER BY id DESC LIMIT 1",
                    (f"{prefix}%",)
                )
                target_window = await cursor.fetchone()
                if target_window:
                    target_wid = target_window[0]
                    target_name = target_window[1]
                    await db.execute(
                        "UPDATE conversations SET window_id = ? WHERE conversation_id = ?",
                        (target_wid, conversation_id)
                    )
                    logger.info(f"[SUMMARIZER] 对话 {conversation_id} 迁移到窗口 '{target_name}' (id={target_wid})")
            
            await db.commit()
        except Exception as e:
            logger.error(f"[SUMMARIZER] 自动窗口分配失败: {e}")
        finally:
            await db.close()
    
    def _empty_summary(self) -> str:
        """返回空摘要"""
        return "本轮无有效对话内容。"


# ==================== 定时任务 ====================

async def run_daily_cron():
    """每天23:59触发的定时任务：生成日总"""
    summarizer = get_summarizer()
    today = datetime.utcnow().strftime('%Y-%m-%d')
    logger.info(f"[CRON] 触发日总生成: {today}")
    result = await summarizer.generate_daily_summary(today)
    if result:
        logger.info(f"[CRON] 日总生成成功: {len(result)} 字符")
    else:
        logger.warning(f"[CRON] 日总生成跳过或失败")


async def run_weekly_cron():
    """每周日23:59触发的定时任务：生成周总"""
    summarizer = get_summarizer()
    today = datetime.utcnow().strftime('%Y-%m-%d')
    logger.info(f"[CRON] 触发周总生成")
    result = await summarizer.generate_weekly_summary(today)
    if result:
        logger.info(f"[CRON] 周总生成成功: {len(result)} 字符")


async def run_monthly_cron():
    """每月最后一天23:59触发的定时任务：生成月总"""
    summarizer = get_summarizer()
    year_month = datetime.utcnow().strftime('%Y-%m')
    logger.info(f"[CRON] 触发月总生成: {year_month}")
    result = await summarizer.generate_monthly_summary(year_month)
    if result:
        logger.info(f"[CRON] 月总生成成功: {len(result)} 字符")


# 模块级单例
_summarizer = None

def get_summarizer() -> MultiLevelSummarizer:
    global _summarizer
    if _summarizer is None:
        _summarizer = MultiLevelSummarizer()
    return _summarizer