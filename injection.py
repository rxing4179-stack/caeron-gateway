"""
Caeron Gateway - 提示词注入引擎
在请求发送到 LLM 之前，按照预设规则修改/插入 messages
"""

import json
import copy
from datetime import datetime
import logging
from database import get_db

logger = logging.getLogger(__name__)

class InjectionEngine:
    async def inject(self, messages: list[dict], request_info: dict = None) -> list[dict]:
        """
        核心注入方法
        :param messages: 原始请求的 messages 数组
        :param request_info: 请求上下文（例如 model 名称、对话上下文长度等），用于条件匹配
        :return: 注入后的 messages（深拷贝，不修改原数组）
        """
        # 深拷贝以防止污染原请求
        injected_messages = copy.deepcopy(messages)
        if not request_info:
            request_info = {}
            
        model = request_info.get('model', '')
        
        # 从数据库获取所有启用的规则，按优先级排序（数字越小优先级越高）
        db = await get_db()
        try:
            cursor = await db.execute('''
                SELECT * FROM injection_rules 
                WHERE is_enabled = 1 
                ORDER BY priority ASC
            ''')
            rules = [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

        assistant_prefill_content = []

        logger.info(f"注入引擎启动: 加载 {len(rules)} 条启用规则, 消息数={len(injected_messages)}")

        for rule in rules:
            # 条件匹配检查
            condition_str = rule.get('match_condition', '{}')
            try:
                condition = json.loads(condition_str) if condition_str else {}
            except json.JSONDecodeError:
                condition = {}

            # match_model: 逗号分隔的模型列表，如果存在则要求当前模型在列表中
            match_model = condition.get('match_model', '')
            if match_model:
                allowed_models = [m.strip() for m in match_model.split(',') if m.strip()]
                if model and allowed_models and model not in allowed_models:
                    continue  # 模型不匹配，跳过此规则

            # match_length_min: 最小上下文长度（消息条数）
            match_length_min = condition.get('match_length_min', 0)
            try:
                match_length_min = int(match_length_min)
            except ValueError:
                match_length_min = 0
                
            if match_length_min > 0 and len(injected_messages) < match_length_min:
                continue
                
            logger.info(f"规则命中: [{rule['name']}] position={rule['position']}, role={rule['role']}")

            # 执行变量替换
            content = self._replace_variables(rule['content'])
            position = rule['position']
            role = rule['role']

            # 根据 role 生成 message 对象的内容
            if role == 'user_wrapped_system':
                msg_role = 'user'
                msg_content = f"<system>\n{content}\n</system>"
            else:
                # 'system' 角色
                msg_role = 'system'
                msg_content = content

            if role == 'assistant_prefill':
                # 收集 prefill 内容，最后统一放到末尾
                assistant_prefill_content.append(content)
                continue

            # 根据 position 注入
            if position == 'system_prepend':
                # 找到第一条 system 消息
                system_msg = next((m for m in injected_messages if m.get('role') == 'system'), None)
                if system_msg:
                    system_msg['content'] = f"{msg_content}\n\n{system_msg['content']}"
                else:
                    injected_messages.insert(0, {'role': msg_role, 'content': msg_content})
                    
            elif position == 'system_append':
                # 找到最后一条 system 消息
                system_msgs = [m for m in injected_messages if m.get('role') == 'system']
                if system_msgs:
                    system_msgs[-1]['content'] = f"{system_msgs[-1]['content']}\n\n{msg_content}"
                else:
                    injected_messages.insert(0, {'role': msg_role, 'content': msg_content})
                    
            elif position == 'dialog_start':
                # 插入在最后一个 system 之后，如果没 system 就插在最前
                insert_idx = 0
                for i, m in enumerate(injected_messages):
                    if m.get('role') == 'system':
                        insert_idx = i + 1
                injected_messages.insert(insert_idx, {'role': msg_role, 'content': msg_content})
                
            elif position == 'before_latest':
                # 插入在最后一条 user 消息之前
                insert_idx = len(injected_messages)
                for i in range(len(injected_messages) - 1, -1, -1):
                    if injected_messages[i].get('role') == 'user':
                        insert_idx = i
                        break
                injected_messages.insert(insert_idx, {'role': msg_role, 'content': msg_content})
                
            elif position == 'at_depth_N':
                depth = rule.get('depth', 0)
                try:
                    depth = int(depth)
                except ValueError:
                    depth = 0
                # 从底部数起。depth=0 -> 末尾, depth=1 -> 倒数第二之前
                insert_idx = max(0, len(injected_messages) - depth)
                injected_messages.insert(insert_idx, {'role': msg_role, 'content': msg_content})
                
        # ==================== 轮总注入 ====================
        # 根据当前对话的窗口归属，注入对应分类的当天所有活跃轮总
        try:
            await self._inject_round_summaries(injected_messages, request_info)
        except Exception as e:
            logger.error(f"轮总注入异常（不影响请求）: {e}")

        # 处理 assistant_prefill
        if assistant_prefill_content:
            merged_prefill = "\n\n".join(assistant_prefill_content)
            injected_messages.append({'role': 'assistant', 'content': merged_prefill})
            
        logger.info(f"注入完成: 原始消息数={len(messages)}, 注入后消息数={len(injected_messages)}")
        return injected_messages

    async def _inject_round_summaries(self, messages: list[dict], request_info: dict):
        """跨窗口注入当天所有活跃轮总到对话消息中"""
        db = await get_db()
        try:
            # 查当天所有活跃轮总（created_at是UTC，+8转本地日期）
            today = datetime.now().strftime('%Y-%m-%d')
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at, '+8 hours') = ?
                   ORDER BY created_at ASC""",
                (today,)
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()

        if not rows:
            logger.info(f"[轮总注入] 当天无活跃轮总，跳过")
            return

        # 组装轮总内容，每条标注编号
        total = len(rows)
        lines = ["<context_summaries>"]
        lines.append(f"以下是今天（{datetime.now().strftime('%Y-%m-%d')}）的对话记忆摘要，供你参考当前上下文：")
        for idx, r in enumerate(rows, 1):
            r = dict(r)
            lines.append(f"- [轮总 #{idx}/{total}] [{r['created_at']}] {r['content']}")
        lines.append("</context_summaries>")

        summary_text = "\n".join(lines)

        # 注入位置：在最后一个system消息之后（dialog_start位置）
        insert_idx = 0
        for i, m in enumerate(messages):
            if m.get('role') == 'system':
                insert_idx = i + 1
        messages.insert(insert_idx, {'role': 'system', 'content': summary_text})

        logger.info(f"[轮总注入] 注入 {total} 条轮总 (跨窗口全局)")

    def _replace_variables(self, text: str) -> str:
        """替换文本中的预设变量"""
        now = datetime.now()
        replacements = {
            '{cur_datetime}': now.strftime('%Y-%m-%d %H:%M:%S'),
            '{cur_date}': now.strftime('%Y-%m-%d'),
            '{cur_time}': now.strftime('%H:%M:%S'),
            '{cur_weekday}': ['一', '二', '三', '四', '五', '六', '日'][now.weekday()],
            '{user_name}': '蕊蕊',
            '{assistant_name}': '沈栖'
        }
        for k, v in replacements.items():
            text = text.replace(k, str(v))
        return text