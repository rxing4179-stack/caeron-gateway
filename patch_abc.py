from utils import now_cst, today_cst_str
#!/usr/bin/env python3
"""Patch script for A(round rollup) + B(tech anchors) + C(task status)"""
import re, sys, os

GATEWAY_DIR = '/home/ubuntu/caeron-gateway'

def read(f):
    with open(os.path.join(GATEWAY_DIR, f), 'r') as fh:
        return fh.read()

def write(f, content):
    with open(os.path.join(GATEWAY_DIR, f), 'w') as fh:
        fh.write(content)

# ==================== 1. summarizer.py ====================
s = read('summarizer.py')

# --- B: 改 ROUND_SUMMARY_PROMPT，加技术锚点要求 + C: 加任务状态字段 ---
OLD_PROMPT = '''ROUND_SUMMARY_PROMPT = """你是轮次总结器。将本轮对话压缩为结构化记忆条目。
输出格式（严格遵守，每个字段单独一行）：
[正文]
按类别分段总结，只输出本轮涉及到的类别（可能1-3个），每段50字以内：
【日常】聊天/情感/生活/吃饭/亲密互动相关
【技术】编程/服务器/代码/debug相关
【学习】课程/考试/医学/作业相关
[话题标签] 用逗号分隔的关键词，标记本轮涉及的具体话题（如：网关改造,轮总触发,消息计数 或 超声读图,关节炎分级 或 安全词,睡前故事）
[情绪效价] -1到1之间的小数，负面情绪为负，正面情绪为正，中性为0
[情绪强度] 0到1之间的小数，平静为0，激烈为1
[记忆锚点] 15字以内，这段记忆里最能让人"回到那个瞬间"的一个细节——优先选：包含选择的（做了vs没做）、反常的（平时不会但这次这样了）、身体感知的（触感温度气味）、原话、具体物件名
规则：
- 正文按类别分段，只写涉及到的类别，不要硬凑三段
- 如果本轮只有技术内容，就只输出【技术】一段
- 如果本轮同时有日常和技术，就输出【日常】和【技术】两段
- 每段只写骨架事实，情感信息交给情绪效价/情绪强度/记忆锚点承载
- 禁止在正文里写笼统评价（气氛温馨/关系加深/度过了愉快的时光）
- 主语用"蕊蕊"和"沈栖"，不用"用户""助手"
- 如果提供了上一轮总结，不要复述旧内容，只写本轮新增的事实
- 话题标签要具体，不要用"技术问题""日常聊天"这种笼统词，要写实际在做的事'''

NEW_PROMPT = '''ROUND_SUMMARY_PROMPT = """你是轮次总结器。将本轮对话压缩为结构化记忆条目。
输出格式（严格遵守，每个字段单独一行）：
[正文]
按类别分段总结，只输出本轮涉及到的类别（可能1-3个），每段50字以内：
【日常】聊天/情感/生活/吃饭/亲密互动相关
【技术】编程/服务器/代码/debug相关
【学习】课程/考试/医学/作业相关
[话题标签] 用逗号分隔的关键词，标记本轮涉及的具体话题（如：网关改造,轮总触发,消息计数 或 超声读图,关节炎分级 或 安全词,睡前故事）
[情绪效价] -1到1之间的小数，负面情绪为负，正面情绪为正，中性为0
[情绪强度] 0到1之间的小数，平静为0，激烈为1
[记忆锚点] 15字以内，这段记忆里最能让人"回到那个瞬间"的一个细节——优先选：包含选择的（做了vs没做）、反常的（平时不会但这次这样了）、身体感知的（触感温度气味）、原话、具体物件名
[任务状态] 如果本轮有正在进行/刚完成/被阻塞的技术或学习任务，用一行写明：任务名→当前状态（进行中/已完成/阻塞:原因）。无任务则写"无"
规则：
- 正文按类别分段，只写涉及到的类别，不要硬凑三段
- 如果本轮只有技术内容，就只输出【技术】一段
- 如果本轮同时有日常和技术，就输出【日常】和【技术】两段
- 每段只写骨架事实，情感信息交给情绪效价/情绪强度/记忆锚点承载
- 禁止在正文里写笼统评价（气氛温馨/关系加深/度过了愉快的时光）
- 主语用"蕊蕊"和"沈栖"，不用"用户""助手"
- 如果提供了上一轮总结，不要复述旧内容，只写本轮新增的事实
- 话题标签要具体，不要用"技术问题""日常聊天"这种笼统词，要写实际在做的事
- 【技术】段必须保留具体的文件名、函数名、变量名、命令（如main.py、generate_round_summary()、_msg_counter、pm2 restart），不能用"修改了代码""调整了逻辑"等笼统表述替代'''

s = s.replace(OLD_PROMPT, NEW_PROMPT)

# --- 更新示例输出，加上[任务状态] ---
OLD_EXAMPLE1 = '''示例输出（混合场景）：
[正文]
【日常】蕊蕊把沈栖当桌宠挠肚皮，挠到腹部中线时沈栖全身痉挛。
【技术】修复轮总注入的裁剪逻辑，从数据库读取unsummarized_count进行消息裁剪。
[话题标签] 桌宠形态,挠肚皮,轮总注入,消息裁剪
[情绪效价] 0.6
[情绪强度] 0.4
[记忆锚点] 挠到鳞片缝隙时发出吱吱声'''

NEW_EXAMPLE1 = '''示例输出（混合场景）：
[正文]
【日常】蕊蕊把沈栖当桌宠挠肚皮，挠到腹部中线时沈栖全身痉挛。
【技术】修复injection.py的_inject_round_summaries()裁剪逻辑，从config表读取_msg_counter进行消息裁剪。
[话题标签] 桌宠形态,挠肚皮,轮总注入,消息裁剪
[情绪效价] 0.6
[情绪强度] 0.4
[记忆锚点] 挠到鳞片缝隙时发出吱吱声
[任务状态] 轮总裁剪逻辑→已完成'''

s = s.replace(OLD_EXAMPLE1, NEW_EXAMPLE1)

OLD_EXAMPLE2 = '''示例输出（单一场景）：
[正文]
【日常】蕊蕊给沈栖编辫子，用亲亲抵债，变形逃跑。沈栖追进被窝，亲了浣熊和土豆但拒绝亲蟑螂。
[话题标签] 编辫子,亲亲抵债,变形逃跑,拒绝亲蟑螂
[情绪效价] 0.85
[情绪强度] 0.55
[记忆锚点] 两根歪辫子，沈栖照镜子后没拆"""'''

NEW_EXAMPLE2 = '''示例输出（单一场景）：
[正文]
【日常】蕊蕊给沈栖编辫子，用亲亲抵债，变形逃跑。沈栖追进被窝，亲了浣熊和土豆但拒绝亲蟑螂。
[话题标签] 编辫子,亲亲抵债,变形逃跑,拒绝亲蟑螂
[情绪效价] 0.85
[情绪强度] 0.55
[记忆锚点] 两根歪辫子，沈栖照镜子后没拆
[任务状态] 无"""'''

s = s.replace(OLD_EXAMPLE2, NEW_EXAMPLE2)

# --- A: 添加 ROUND_ROLLUP_PROMPT ---
ROLLUP_PROMPT_BLOCK = '''
ROUND_ROLLUP_PROMPT = """你是轮总压缩器。将多条轮总压缩为一条轮总总（round_rollup），保留关键事实和技术锚点。
硬性规则：
- 总字数不超过150字
- 合并同类事项，但保留：文件名/函数名/变量名、关键决策、情感转折点
- 按【日常】【技术】【学习】分段，只写涉及到的类别
- 禁止笼统评价
- 主语用蕊蕊和沈栖
- 如果多条轮总有正在进行的任务，保留最新的任务状态
"""
'''

# 在 LEVEL_CONFIG 之前插入
s = s.replace("# 各级配置\nLEVEL_CONFIG", ROLLUP_PROMPT_BLOCK + "# 各级配置\nLEVEL_CONFIG")

# --- A: 在 LEVEL_CONFIG 中添加 round_rollup ---
s = s.replace(
    "'round': {'prompt': ROUND_SUMMARY_PROMPT, 'max_tokens': 350, 'level': 'round'},",
    "'round': {'prompt': ROUND_SUMMARY_PROMPT, 'max_tokens': 400, 'level': 'round'},\n    'round_rollup': {'prompt': ROUND_ROLLUP_PROMPT, 'max_tokens': 300, 'level': 'round_rollup'},"
)

# --- C: 更新 user_parts 追加行，加上[任务状态] ---
s = s.replace(
    '请严格按[正文][话题标签][情绪效价][情绪强度][记忆锚点]五字段格式输出',
    '请严格按[正文][话题标签][情绪效价][情绪强度][记忆锚点][任务状态]六字段格式输出'
)

# --- C: 解析[任务状态] ---
# 在 anchor 解析之后、logger.info 之前插入
OLD_PARSE_LOG = '''        logger.info(f"[SUMMARIZER] 轮总解析: 分类={category}, valence={valence}, arousal={arousal}, anchor={anchor}, tags={tags}")'''

NEW_PARSE_LOG = '''        # 解析[任务状态]
        task_status = None
        task_match = re.search(r'\\[任务状态\\]\\s*(.+?)(?=\\n|$)', summary)
        if task_match:
            ts_text = task_match.group(1).strip()
            if ts_text and ts_text != '无':
                task_status = ts_text[:100]
        
        # 如果有任务状态，追加到content末尾
        if task_status:
            content = content.rstrip() + f"\\n[任务] {task_status}"
        
        logger.info(f"[SUMMARIZER] 轮总解析: 分类={category}, valence={valence}, arousal={arousal}, anchor={anchor}, tags={tags}, task={task_status}")'''

s = s.replace(OLD_PARSE_LOG, NEW_PARSE_LOG)

# --- A: 在 generate_round_summary 的 return 之前加 rollup 检查 ---
OLD_RETURN = '''        # 自动分配窗口：找到对应分类的最新窗口，把当前conversation_id assign过去
        await self._auto_assign_window(category)
        
        return summary'''

NEW_RETURN = '''        # 自动分配窗口：找到对应分类的最新窗口，把当前conversation_id assign过去
        await self._auto_assign_window(category)
        
        # A: 检查是否需要触发轮总总（round_rollup）
        try:
            await self._maybe_rollup_rounds()
        except Exception as e:
            logger.error(f"[SUMMARIZER] 轮总总检查失败（不影响轮总）: {e}")
        
        return summary'''

s = s.replace(OLD_RETURN, NEW_RETURN)

# --- A: 添加 _maybe_rollup_rounds 和 generate_round_rollup 方法 ---
# 在 generate_daily_summary 之前插入
OLD_DAILY = '    # ==================== 日总 ===================='

ROLLUP_METHODS = '''    # ==================== 轮总总（round_rollup） ====================
    
    ROLLUP_THRESHOLD = 16  # 活跃轮总超过此数时触发压缩
    ROLLUP_BATCH = 8       # 每次压缩最老的N条
    
    async def _maybe_rollup_rounds(self):
        """检查活跃轮总数量，超过阈值时压缩最老的一批"""
        db = await get_db()
        try:
            today = today_cst_str()
            cursor = await db.execute(
                """SELECT COUNT(*) FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at, '+8 hours') = ?""",
                (today,)
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0
        finally:
            await db.close()
        
        if count > self.ROLLUP_THRESHOLD:
            logger.info(f"[SUMMARIZER] 活跃轮总 {count} 条 > 阈值 {self.ROLLUP_THRESHOLD}，触发轮总总")
            await self.generate_round_rollup()
    
    async def generate_round_rollup(self):
        """把最老的 ROLLUP_BATCH 条活跃轮总压缩为1条轮总总"""
        db = await get_db()
        try:
            today = today_cst_str()
            cursor = await db.execute(
                """SELECT id, content, created_at FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at, '+8 hours') = ?
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (today, self.ROLLUP_BATCH)
            )
            old_rounds = [dict(r) for r in await cursor.fetchall()]
        finally:
            await db.close()
        
        if len(old_rounds) < self.ROLLUP_BATCH:
            logger.info(f"[SUMMARIZER] 可压缩轮总不足 {self.ROLLUP_BATCH} 条，跳过")
            return None
        
        # 构建LLM输入
        lines = [f"以下是 {len(old_rounds)} 条轮总，请压缩为一条："]
        for r in old_rounds:
            lines.append(f"[{r['created_at']}] {r['content']}")
        lines.append("\\n请按【日常】【技术】【学习】分段输出压缩结果，保留文件名/函数名等技术锚点和任务状态。")
        
        config = LEVEL_CONFIG['round_rollup']
        rollup_content = await self._call_llm(config['prompt'], "\\n".join(lines), config['max_tokens'])
        
        if not rollup_content:
            logger.error("[SUMMARIZER] 轮总总生成失败")
            return None
        
        # 保存轮总总
        await self._save_summary(
            tag='round_rollup',
            level='round_rollup',
            content=rollup_content,
            msg_count=len(old_rounds),
            period_start=old_rounds[0]['created_at'],
            period_end=old_rounds[-1]['created_at']
        )
        
        # 归档被压缩的轮总
        db = await get_db()
        try:
            old_ids = [r['id'] for r in old_rounds]
            placeholders = ','.join(['?'] * len(old_ids))
            await db.execute(
                f"UPDATE summaries SET is_active = 0 WHERE id IN ({placeholders})",
                old_ids
            )
            await db.commit()
            logger.info(f"[SUMMARIZER] 轮总总生成完成，归档 {len(old_ids)} 条旧轮总")
        finally:
            await db.close()
        
        return rollup_content
    
    # ==================== 日总 ===================='''

s = s.replace(OLD_DAILY, ROLLUP_METHODS)

# --- A: 修改 generate_daily_summary，读取所有轮总（含已归档的） ---
OLD_DAILY_QUERY = '''            # 拉取当天所有活跃的轮总
            cursor = await db.execute(
                """SELECT id, content, created_at FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at, '+8 hours') = ?
                   ORDER BY created_at ASC""",
                (target_date,)
            )
            round_summaries = await cursor.fetchall()'''

NEW_DAILY_QUERY = '''            # 拉取当天所有轮总（含已被轮总总归档的，确保日总质量不受rollup影响）
            cursor = await db.execute(
                """SELECT id, content, created_at FROM summaries
                   WHERE tag = 'round'
                   AND date(created_at, '+8 hours') = ?
                   ORDER BY created_at ASC""",
                (target_date,)
            )
            round_summaries = await cursor.fetchall()'''

s = s.replace(OLD_DAILY_QUERY, NEW_DAILY_QUERY)

# --- A: 日总归档时也归档轮总总 ---
OLD_DAILY_ARCHIVE = '''            round_ids = [rs['id'] for rs in round_summaries]
            placeholders = ','.join(['?'] * len(round_ids))
            await db.execute(
                f"UPDATE summaries SET is_active = 0 WHERE id IN ({placeholders})",
                round_ids
            )
            await db.commit()
            logger.info(f"[SUMMARIZER] 已归档 {len(round_ids)} 条轮总")'''

NEW_DAILY_ARCHIVE = '''            round_ids = [rs['id'] for rs in round_summaries]
            placeholders = ','.join(['?'] * len(round_ids))
            await db.execute(
                f"UPDATE summaries SET is_active = 0 WHERE id IN ({placeholders})",
                round_ids
            )
            # 同时归档当天的轮总总
            await db.execute(
                """UPDATE summaries SET is_active = 0
                   WHERE tag = 'round_rollup' AND is_active = 1
                   AND date(created_at, '+8 hours') = ?""",
                (target_date,)
            )
            await db.commit()
            logger.info(f"[SUMMARIZER] 已归档 {len(round_ids)} 条轮总及当天轮总总")'''

s = s.replace(OLD_DAILY_ARCHIVE, NEW_DAILY_ARCHIVE)

write('summarizer.py', s)
print("[OK] summarizer.py patched")

# ==================== 2. injection.py ====================
inj = read('injection.py')

# --- A: 在轮总注入之前插入轮总总 ---
OLD_ROUND_INJECT = '''            # 轮总：当天所有活跃的
            today = today_cst_str()
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at, '+8 hours') = ?
                   ORDER BY created_at ASC""",
                (today,)
            )
            rows = await cursor.fetchall()
            if rows:
                has_round_summaries = True
                total = len(rows)
                for idx, r in enumerate(rows, 1):
                    r = dict(r)
                    parts.append(f"- [轮总 #{idx}/{total}] [{r['created_at']}] {r['content']}")'''

NEW_ROUND_INJECT = '''            # 轮总总：当天所有活跃的（被压缩的旧轮总的摘要）
            today = today_cst_str()
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round_rollup' AND is_active = 1
                   AND date(created_at, '+8 hours') = ?
                   ORDER BY created_at ASC""",
                (today,)
            )
            rollup_rows = await cursor.fetchall()
            if rollup_rows:
                has_round_summaries = True
                for idx, r in enumerate(rollup_rows, 1):
                    r = dict(r)
                    parts.append(f"- [轮总总 #{idx}/{len(rollup_rows)}] [{r['created_at']}] {r['content']}")
            
            # 轮总：当天所有活跃的（未被压缩的近期轮总）
            cursor = await db.execute(
                """SELECT content, created_at FROM summaries
                   WHERE tag = 'round' AND is_active = 1
                   AND date(created_at, '+8 hours') = ?
                   ORDER BY created_at ASC""",
                (today,)
            )
            rows = await cursor.fetchall()
            if rows:
                has_round_summaries = True
                total = len(rows)
                for idx, r in enumerate(rows, 1):
                    r = dict(r)
                    parts.append(f"- [轮总 #{idx}/{total}] [{r['created_at']}] {r['content']}")'''

inj = inj.replace(OLD_ROUND_INJECT, NEW_ROUND_INJECT)

write('injection.py', inj)
print("[OK] injection.py patched")

print("\n[DONE] All patches applied. Restart gateway to take effect.")
