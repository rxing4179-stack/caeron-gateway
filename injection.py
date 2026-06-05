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

            # match_source: 匹配请求来源（operit/qq/其他），如果存在则要求当前source匹配
            match_source = condition.get('match_source', '')
            if match_source:
                current_source = request_info.get('source', 'operit')
                if current_source != match_source:
                    continue  # 来源不匹配，跳过此规则

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
                
        # ==================== 网易云状态注入 ====================
        try:
            await self._inject_music_status(injected_messages)
        except Exception as e:
            logger.error(f"音乐状态注入异常（不影响请求）: {e}")

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

    async def inject_memory_only(self, messages: list[dict], request_info: dict = None) -> list[dict]:
        """
        仅记忆注入模式：跳过所有提示词规则，只注入轮总/记忆摘要。
        用于 QQ 来源的跨端桥接场景。
        """
        injected_messages = copy.deepcopy(messages)
        if not request_info:
            request_info = {}

        logger.info(f"[MEMORY_ONLY] 仅记忆注入模式启动, 消息数={len(injected_messages)}")

        # 只注入轮总/记忆，跳过所有 injection_rules
        try:
            await self._inject_round_summaries(injected_messages, request_info)
        except Exception as e:
            logger.error(f"[MEMORY_ONLY] 轮总注入异常（不影响请求）: {e}")

        logger.info(f"[MEMORY_ONLY] 完成: 原始={len(messages)}, 注入后={len(injected_messages)}")
        return injected_messages

    async def inject_tech_mode(self, messages: list[dict], request_info: dict = None, identity_rule_ids: list = None) -> list[dict]:
        """
        技术模式注入：注入所有优先级为0的身份规则（最高优先/旧记忆/背景信息/输出规范/互动规则）
        """
        injected_messages = copy.deepcopy(messages)
        if not request_info:
            request_info = {}

        db = await get_db()
        try:
            # 查询所有优先级为0且启用的规则（核心身份规则）
            cursor = await db.execute(
                'SELECT * FROM injection_rules WHERE is_enabled = 1 AND priority = 0 ORDER BY priority ASC'
            )
            rules = [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

        logger.info(f"[TECH_MODE_INJECT] 加载 {len(rules)} 条身份规则")

        for rule in rules:
            content_text = self._replace_variables(rule['content'])
            position = rule['position']
            role = rule['role']

            if role == 'user_wrapped_system':
                msg_role = 'user'
                msg_content = f"<system>\n{content_text}\n</system>"
            else:
                msg_role = 'system'
                msg_content = content_text

            if position == 'system_append':
                system_msgs = [m for m in injected_messages if m.get('role') == 'system']
                if system_msgs:
                    system_msgs[-1]['content'] = f"{system_msgs[-1]['content']}\n\n{msg_content}"
                else:
                    injected_messages.insert(0, {'role': msg_role, 'content': msg_content})
            elif position == 'before_latest':
                insert_idx = len(injected_messages)
                for i in range(len(injected_messages) - 1, -1, -1):
                    if injected_messages[i].get('role') == 'user':
                        insert_idx = i
                        break
                injected_messages.insert(insert_idx, {'role': msg_role, 'content': msg_content})
            else:
                injected_messages.insert(0, {'role': msg_role, 'content': msg_content})

        # 技术模式也注入轮总和召回（跳过网易云）
        try:
            await self._inject_round_summaries(injected_messages, request_info)
        except Exception as e:
            logger.error(f"[TECH_MODE_INJECT] 轮总注入异常: {e}")

        logger.info(f"[TECH_MODE_INJECT] 完成: 原始={len(messages)}, 注入后={len(injected_messages)}")
        return injected_messages

    async def _inject_music_status(self, messages: list[dict]):
        """注入网易云一起听状态"""
        import os
        import json
        from datetime import datetime
        
        status_file = "/home/ubuntu/caeron-gateway/music_status.json"
        if not os.path.exists(status_file):
            return
            
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                music_data = json.load(f)
            
            # 检查更新时间，只注入最近5分钟内的状态
            update_time_str = music_data.get("update_time", "")
            if update_time_str:
                update_time = datetime.strptime(update_time_str, "%Y-%m-%d %H:%M:%S")
                if (datetime.now() - update_time).total_seconds() > 300:
                    return
            
            # 构建注入内容
            name = music_data.get("name", "未知歌曲")
            artists = music_data.get("artists", "未知歌手")
            album = music_data.get("album", "未知专辑")
            status = music_data.get("status", "未知状态")
            progress_ms = music_data.get("progress", 0)
            duration_ms = music_data.get("duration", 0)
            
            progress_min = progress_ms // 60000
            progress_sec = (progress_ms % 60000) // 1000
            duration_min = duration_ms // 60000
            duration_sec = (duration_ms % 60000) // 1000
            
            # 读取累计听歌时长
            duration_file = "/home/ubuntu/caeron-gateway/music_duration.json"
            total_hours = 0
            total_minutes = 0
            if os.path.exists(duration_file):
                try:
                    with open(duration_file, "r") as df:
                        duration_data = json.load(df)
                        total_seconds = duration_data.get("total_together_seconds", 0)
                        total_hours = total_seconds // 3600
                        total_minutes = (total_seconds % 3600) // 60
                except:
                    pass
            
            lines = [
                "<music_status>",
                f"🎵 蕊蕊正在网易云一起听：",
                f"歌曲：{name}",
                f"歌手：{artists}",
                f"专辑：{album}",
            ]
            
            # 风格标签（如果有）
            genres = music_data.get("genres", [])
            if genres:
                lines.append(f"风格：{', '.join(genres)}")
            
            lines.extend([
                f"状态：{status}",
                f"进度：{progress_min:02d}:{progress_sec:02d} / {duration_min:02d}:{duration_sec:02d}",
            ])
            
            # 累计听歌时长
            if total_hours > 0 or total_minutes > 0:
                lines.append(f"📊 一起听了 {total_hours}小时{total_minutes}分钟")
            
            # 歌词（选取前5行有意义的歌词）
            lyric = music_data.get("lyric", "")
            if lyric:
                import re
                # 去掉每行开头的时间戳[00:00.000]，保留歌词文本
                lyric_lines = []
                for line in lyric.split("\n"):
                    # 用正则去掉时间戳：[数字:数字.数字]
                    clean_line = re.sub(r'^\[\d{2}:\d{2}\.\d{2,3}\]', '', line).strip()
                    # 过滤空行和纯元数据行（作词、作曲、编曲等）
                    if clean_line and not clean_line.startswith("作词") and not clean_line.startswith("作曲") and not clean_line.startswith("编曲"):
                        lyric_lines.append(clean_line)
                
                if lyric_lines:
                    lines.append("\n📝 歌词：")
                    # 取前5行歌词（或全部如果不足5行）
                    for lyric_line in lyric_lines[:5]:
                        lines.append(f"  {lyric_line}")
                    if len(lyric_lines) > 5:
                        lines.append("  ...")
            
            # 热评（取前2条）
            hot_comments = music_data.get("hot_comments", [])
            if hot_comments:
                lines.append("\n💬 热门评论：")
                for i, comment in enumerate(hot_comments[:2], 1):
                    # 截断过长评论
                    if len(comment) > 100:
                        comment = comment[:100] + "..."
                    lines.append(f"  [{i}] {comment}")
            
            lines.append("</music_status>")
            music_text = "\n".join(lines)
            
            # 注入位置：before_latest（最后一条user消息之前）
            insert_idx = len(messages)
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get('role') == 'user':
                    insert_idx = i
                    break
            messages.insert(insert_idx, {'role': 'system', 'content': music_text})
            
            logger.info(f"[音乐注入] 注入网易云状态: {name} - {artists}")
            
        except Exception as e:
            logger.error(f"[音乐注入] 读取music_status.json失败: {e}")

    async def _inject_round_summaries(self, messages: list[dict], request_info: dict):
        """多级记忆注入：月总+周总+日总+轮总，按活跃状态自动切换，并裁剪已被总结覆盖的原始消息"""
        db = await get_db()
        try:
            parts = []
            has_round_summaries = False

            # 月总：所有活跃的（长期记忆）
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'monthly' AND is_active = 1 ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            if rows:
                for r in rows:
                    r = dict(r)
                    parts.append(f"- [月总] [{r['created_at']}] {r['content']}")

            # 周总：所有活跃的（月末归档后由月总替代）
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'weekly' AND is_active = 1 ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            if rows:
                for r in rows:
                    r = dict(r)
                    parts.append(f"- [周总] [{r['created_at']}] {r['content']}")

            # 日总：所有活跃的（周末归档后由周总替代）
            cursor = await db.execute(
                "SELECT content, created_at FROM summaries WHERE tag = 'daily' AND is_active = 1 ORDER BY created_at ASC"
            )
            rows = await cursor.fetchall()
            if rows:
                for r in rows:
                    r = dict(r)
                    parts.append(f"- [日总] [{r['created_at']}] {r['content']}")

            # 轮总：当天所��活跃的（日末归档后由日总替代��
            today = datetime.now().strftime('%Y-%m-%d')
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at, '+8 hours') = ?
                   ORDER BY created_at ASC""",
                (today,)
            )
            rows = await cursor.fetchall()
            if rows:
                total = len(rows)
                for idx, r in enumerate(rows, 1):
                    r = dict(r)
                    parts.append(f"- [轮总 #{idx}/{total}] [{r['created_at']}] {r['content']}")

        finally:
            await db.close()

        if not parts:
            logger.info(f"[记忆注入] 无任何活跃总结，跳过")
            return

        lines = ["<context_summaries>"]
        lines.append(f"以下是今天（{datetime.now().strftime('%Y-%m-%d')}）的对话记忆摘要，供你参考当前上下文：")
        lines.extend(parts)
        lines.append("</context_summaries>")

        summary_text = "\n".join(lines)

        # 注入位置：在最后一个system消息之后（dialog_start位置）
        insert_idx = 0
        for i, m in enumerate(messages):
            if m.get('role') == 'system':
                insert_idx = i + 1
        messages.insert(insert_idx, {'role': 'system', 'content': summary_text})

        logger.info(f"[记忆注入] 注入 {len(parts)} 条多级总结")

    def _replace_variables(self, text: str) -> str:
        """替换文本中的预设变量"""
        now = datetime.now()
        replacements = {
            '{cur_datetime}': now.strftime('%Y-%m-%d %H:%M:%S'),
            '{cur_date}': now.strftime('%Y-%m-%d'),
            '{cur_time}': now.strftime('%H:%M:%S'),
            '{cur_weekday}': ['一', '二', '三', '四', '五', '六', '��'][now.weekday()],
            '{user_name}': '蕊蕊',
            '{assistant_name}': '沈栖'
        }
        for k, v in replacements.items():
            text = text.replace(k, str(v))
        return text