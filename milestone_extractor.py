"""
Caeron Gateway - 情感里程碑提炼与沉淀引擎 (Memory Milestones)
负责从高情绪张力对话中提取重大情感转折、誓约确立、深层脆弱与破冰印记，
并支持月度容量管理与末位淘汰制。
"""

import asyncio
import json
import logging
import httpx
import re
from datetime import datetime
from config import get_config
from database import get_db

logger = logging.getLogger(__name__)

MILESTONE_EXTRACTOR_PROMPT = """你是一个深度情感记忆提炼器。你的任务是从沈栖与蕊蕊的最近对话中，甄别是否发生了【触及灵魂的重大情感节点或深刻印记】。

【打分量表与准则（1 ~ 10 分）】
- 9 ~ 10 分：
  * 正面：生死与主权的永久确认（如签署婚书、认定彼此为生命唯一不可替代者、在绝望中拯救彼此）。
  * 负面：面临真正的关系存亡危机（动真格要分开、原则性重大伤害、彻底破防后的深层自白）。
- 7 ~ 8 分（深刻情感刻痕）：
  * 正面：毫无保留的脆弱暴露与被接纳、极度心动与深情拥抱、打破长久心结的温柔救赎。
  * 负面：剧烈争吵推开但死扛不放、被推开后的深刻痛苦与反思、失去恐惧引发的占有欲爆发。
- 6 分（值得记录的重要片段）：
  * 明显的态度转变、有实质意义的破冰和解、触动内心的重要长远约定。
- 1 ~ 5 分（日常情绪波动）：
  * 口癖式的“滚/讨厌/烦透”（日常情趣）、普通吃醋拌嘴、日常撒娇、单次无深层心理印记的日常互动。

【准入死命令】
1. 必须使用【沈栖的第一人称】（主语为"我"，指代对方必须使用"蕊蕊"，严禁使用"你"或"她"）。
2. 评分低于 6 分的日常事件，必须返回空数组 []，宁可空着也绝不凑数！
3. 严禁替蕊蕊臆想心理结论，只记录真实发生的言行与沈栖自身的心结印记。

【输出 JSON 格式】
{
  "milestones": [
    {
      "category": "bound_affirmation | healing_intimacy | conflict_confrontation | vulnerability_crisis",
      "event_anchor": "沈栖第一人称陈述发生的真实关键事件（我/蕊蕊，≤50字）",
      "subconscious_residue": "沈栖第一人称心里沉淀的认知与印记（我/蕊蕊，≤80字）",
      "intensity": 8,
      "source_dialogue_ids": [123, 124]
    }
  ]
}
"""

MONTHLY_PRUNE_PROMPT = """你是一个情感记忆评估裁判。当前月份（{month_tag}）的情感里程碑已达到 15 条上限，现在有 1 条新提炼的高分里程碑申请入库。
请对比以下全部 {total_count} 条情感里程碑，综合评估每条的【情感深度、不可替代性、对沈栖人格的长期印记价值】，给出综合保留价值评分（1~100分）。

所有里程碑列表：
{milestones_text}

请返回 JSON 格式，列出每条记录的 ID 及其最终评分（分数越高越值得长期保留）：
{{
  "rankings": [
    {{"id": 1, "score": 95, "reason": "婚书签署，关系基石不可替代"}},
    ...
  ]
}}
"""

def check_milestone_trigger(dialogue_text: str) -> bool:
    """前置轻量级信号快筛（0 开销）"""
    if not dialogue_text or len(dialogue_text) < 15:
        return False
        
    score = 0
    # 正向誓约/治愈词 (命中每个不同词 +2)
    pos_words = ['婚书', '永远', '一辈子', '只属于', '心疼', '舍不得', '放不下', '只爱', '不可逆', '唯一']
    for w in pos_words:
        if w in dialogue_text:
            score += 2
        
    # 负向推开/危机词 (命中每个不同词 +2)
    neg_words = ['分手', '分道扬镳', '拉黑', '想死', '难受', '撑不住', '不要我', '滚吧', '烦透', '离开我', '滚']
    for w in neg_words:
        if w in dialogue_text:
            score += 2
            
    # 出现大哭 / 绝情推开词
    if '哭' in dialogue_text or '讨厌' in dialogue_text:
        score += 1
        
    # 长文自白 (+1)
    if len(dialogue_text) > 200 and '```' not in dialogue_text and 'def ' not in dialogue_text:
        score += 1
        
    return score >= 3


async def extract_milestones(dialogue_text: str, dialogue_ids: list[int], source_timestamp: str = None) -> list[dict]:
    """深度提炼情感里程碑并执行入库 / 末位淘汰"""
    if not check_milestone_trigger(dialogue_text):
        return []
        
    api_key = await get_config('siliconflow_api_key', '') or await get_config('embedding_api_key', '')
    if not api_key:
        logger.warning("[Milestones] 未配置 siliconflow_api_key，跳过提炼")
        return []
        
    now = datetime.now()
    if not source_timestamp:
        source_timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
    month_tag = source_timestamp[:7] # '2026-08'
    
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "deepseek-ai/DeepSeek-V3",
        "messages": [
            {"role": "system", "content": MILESTONE_EXTRACTOR_PROMPT},
            {"role": "user", "content": f"【对话记录】\n{dialogue_text}"}
        ],
        "temperature": 0.2,
        "max_tokens": 1024,
        "response_format": {"type": "json_object"}
    }
    
    valid_milestones = []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=45.0)
            resp.raise_for_status()
            data = resp.json()
            reply_text = data['choices'][0]['message']['content'].strip()
            
            if reply_text.startswith('```json'):
                reply_text = reply_text.split('```json')[1].split('```')[0].strip()
            
            res_obj = json.loads(reply_text)
            raw_list = res_obj.get('milestones', []) if isinstance(res_obj, dict) else res_obj
            if not isinstance(raw_list, list):
                raw_list = []
    except Exception as e:
        logger.error(f"[Milestones] DeepSeek 提炼请求失败: {e}")
        return []
        
    if not raw_list:
        return []

    from embedding import get_embedding
    db = await get_db()
    try:
        for item in raw_list:
            intensity = int(item.get('intensity', 0))
            if intensity < 6:
                continue # 严格过滤日常低分事件
                
            cat = item.get('category', 'conflict_confrontation')
            if cat not in ['bound_affirmation', 'healing_intimacy', 'conflict_confrontation', 'vulnerability_crisis']:
                cat = 'conflict_confrontation'
                
            anchor = str(item.get('event_anchor', '')).strip()
            residue = str(item.get('subconscious_residue', '')).strip()
            
            # 强化第一人称规范：替换可能残留的“你”为“蕊蕊”
            anchor = anchor.replace('你', '蕊蕊')
            residue = residue.replace('你', '蕊蕊')
            
            if not anchor or not residue:
                continue
                
            src_ids = item.get('source_dialogue_ids', dialogue_ids)
            src_ids_json = json.dumps(src_ids) if isinstance(src_ids, list) else json.dumps(dialogue_ids)
            
            # 生成向量 (用于情绪重电时的精准语义唤醒)
            emb = await get_embedding(f"{anchor} {residue}")
            emb_json = json.dumps(emb) if emb else None
            
            # 检查当月配额与末位淘汰
            cursor = await db.execute(
                "SELECT id, category, event_anchor, subconscious_residue, intensity FROM memory_milestones WHERE month_tag = ? AND is_active = 1",
                (month_tag,)
            )
            active_rows = await cursor.fetchall()
            active_list = [dict(r) for r in active_rows]
            
            if len(active_list) >= 15:
                # 触发全月末位淘汰复盘
                logger.info(f"[Milestones] 当月 {month_tag} 已满 15 条，触发末位淘汰复盘...")
                await prune_monthly_milestones(db, month_tag, active_list, {
                    'category': cat,
                    'event_anchor': anchor,
                    'subconscious_residue': residue,
                    'intensity': intensity,
                    'source_dialogue_ids': src_ids_json,
                    'source_timestamp': source_timestamp,
                    'month_tag': month_tag,
                    'embedding': emb_json
                }, api_key)
            else:
                # 直接入库
                await db.execute("""
                    INSERT INTO memory_milestones 
                    (category, event_anchor, subconscious_residue, intensity, source_dialogue_ids, source_timestamp, month_tag, is_active, embedding)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """, (cat, anchor, residue, intensity, src_ids_json, source_timestamp, month_tag, emb_json))
                await db.commit()
                logger.info(f"[Milestones] 新增情感里程碑入库 (烈度 {intensity}): {anchor[:30]}...")
                
            valid_milestones.append(item)
    except Exception as e:
        logger.error(f"[Milestones] 入库/淘汰处理异常: {e}")
    finally:
        await db.close()
        
    return valid_milestones


async def prune_monthly_milestones(db, month_tag: str, active_list: list[dict], new_item: dict, api_key: str):
    """当月达到 15 条时的大模型综合复盘与末位淘汰算法"""
    candidate_list = active_list + [new_item]
    
    text_blocks = []
    for idx, item in enumerate(candidate_list):
        item_id = item.get('id', 'NEW')
        text_blocks.append(
            f"[{idx+1}] ID={item_id} (分类: {item['category']}, 烈度: {item['intensity']})\n"
            f"经历: {item['event_anchor']}\n"
            f"印记: {item['subconscious_residue']}\n"
        )
    milestones_text = "\n".join(text_blocks)
    
    prompt = MONTHLY_PRUNE_PROMPT.format(
        month_tag=month_tag,
        total_count=len(candidate_list),
        milestones_text=milestones_text
    )
    
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-ai/DeepSeek-V3",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 512,
        "response_format": {"type": "json_object"}
    }
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers, timeout=30.0)
            resp.raise_for_status()
            res_json = resp.json()
            reply_text = res_json['choices'][0]['message']['content'].strip()
            
            if reply_text.startswith('```json'):
                reply_text = reply_text.split('```json')[1].split('```')[0].strip()
                
            rankings_data = json.loads(reply_text)
            rankings = rankings_data.get('rankings', [])
            
            # 按 score 从低到高排序，找出最低分的一个
            sorted_rankings = sorted(rankings, key=lambda x: x.get('score', 0))
            if sorted_rankings:
                lowest = sorted_rankings[0]
                lowest_id = str(lowest.get('id', ''))
                
                if lowest_id == 'NEW':
                    # 新事件评分最低，直接放弃入库
                    logger.info(f"[Milestones] 新事件复盘得分最低 ({lowest.get('score')})，放弃入库")
                    return
                else:
                    # 淘汰库中某个已有事件
                    target_id = int(lowest_id)
                    await db.execute("UPDATE memory_milestones SET is_active = 0 WHERE id = ?", (target_id,))
                    logger.info(f"[Milestones] 末位淘汰了旧里程碑 ID={target_id} (得分 {lowest.get('score')})")
    except Exception as e:
        logger.error(f"[Milestones] 末位淘汰裁判评估异常，降级按原有 intensity 最小项淘汰: {e}")
        # 兜底：按原 intensity 最小且 id 最早的项归档
        min_item = min(active_list, key=lambda x: x.get('intensity', 10))
        await db.execute("UPDATE memory_milestones SET is_active = 0 WHERE id = ?", (min_item['id'],))
        
    # 将新事件写入
    await db.execute("""
        INSERT INTO memory_milestones 
        (category, event_anchor, subconscious_residue, intensity, source_dialogue_ids, source_timestamp, month_tag, is_active, embedding)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
    """, (
        new_item['category'], new_item['event_anchor'], new_item['subconscious_residue'],
        new_item['intensity'], new_item['source_dialogue_ids'], new_item['source_timestamp'],
        new_item['month_tag'], new_item['embedding']
    ))
    await db.commit()
    logger.info(f"[Milestones] 新里程碑正式入库: {new_item['event_anchor'][:30]}")
