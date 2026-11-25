import asyncio
import json
import logging
import httpx
import re
from datetime import datetime
from config import get_config
from database import get_db

logger = logging.getLogger(__name__)

EXTRACTOR_SYSTEM_PROMPT = """你是一个记忆筛选器。你的任务是从沈栖与蕊蕊的对话中提取值得长期保存的事实碎片。

【准入三条件——必须同时满足，缺一不存】

1. 长期有效：这条信息在三个月后依然成立吗？
2. 主语是蕊蕊：这条信息描述的是蕊蕊的事实，而非沈栖的感受/推断/分析？
3. 必须是事实：这条信息是蕊蕊明确表达/做出的，而非从语气、行为推测的？

【适合存的类型】
- 稳定偏好/厌恶（持续存在，非单次事件）
- 过敏源、药物变更、诊断变更
- 关系规则变更（安全词、称呼、纪念日）
- 长期身体特征或习惯变化
- 重大人生决定（转专业、搬家等，且已确认执行）

【绝对不存】
- 单次饮食/消费/日常动作
- 沈栖自己的感受、分析、推断、解读
- 做爱的具体过程描写
- 技术调试细节
- 天气/时间/地点等环境信息
- "她说了晚安/嗯/好的"等无信息量对话
- 任何需要加"我觉得/我认为/似乎/可能"才能成立的内容

【输出格式】
你必须返回合法的 JSON 格式。除了提取的碎片，你还必须提取每个碎片中的“核心实体”（如人名、称呼、药名、身体部位、物品名等），以方便后续比对。
{
  "fragments": [
    {
      "content": "碎片内容，必须以蕊蕊为主语陈述客观事实，不超过40字",
      "tags": "偏好/生理/规则/经历",
      "entities": ["实体1", "实体2"],
      "source_index": 0
    }
  ]
}

标签限定：偏好/生理/规则/经历。
一次最多3条。宁可返回空数组也绝不凑数。
"""

async def verify_mismatch(fragment: dict, raw_dialogue: str, api_key: str) -> str:
    """
    第二级：语义校验，换一个不同厂商的模型（Qwen2.5-72B-Instruct）。
    """
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    content = fragment.get('content', '')
    prompt = f"""请校验【记忆碎片】是否完全忠实于【原文】。
若记忆碎片中出现了原文根本没提过的信息或细节（幻觉），请判定为 mismatch。如果完全忠实原文，请判定为 ok。

原文：{raw_dialogue}
记忆碎片：{content}

请直接输出 'ok' 或 'mismatch'，不要任何多余解释。"""

    payload = {
        "model": "Qwen/Qwen2.5-72B-Instruct",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 50
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=20.0)
            response.raise_for_status()
            data = response.json()
            res_text = data['choices'][0]['message']['content'].strip().lower()
            if 'mismatch' in res_text:
                return 'mismatch'
            return 'ok'
    except Exception as e:
        logger.error(f"[Memory Extractor] 语义校验异常: {e}")
        return 'unavailable'


async def judge_memory_action(new_content: str, candidates: list[dict], api_key: str) -> dict:
    """
    Mem0 + Graphiti 融合入库裁判：
    将新记忆碎片与库中最相似的 Top-5 活跃旧碎片比对，决定执行 ADD / NOOP / UPDATE / SUPERSEDE。
    """
    fallback = {"action": "ADD", "target_ids": [], "merged_content": "", "reason": "judge_fallback"}
    if not candidates or not api_key:
        return fallback

    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    cand_lines = []
    valid_id_set = set()
    for c in candidates:
        cid = int(c["id"])
        valid_id_set.add(cid)
        c_date = str(c.get("date") or "未知日期")[:10]
        cand_lines.append(f"- [ID:{cid}] ({c_date}, 相似度{c['sim']:.2f}) {c['content']}")
    cand_block = "\n".join(cand_lines)

    prompt = f"""你是记忆库去重与状态更新裁判。请比对【新提取的记忆碎片】和【库中现有相关活跃记忆】，严格返回 JSON 决定入库动作：

【新记忆碎片】
{new_content}

【库中现有相关活跃记忆】
{cand_block}

请从以下 4 个动作中选择唯一最合适的一个：
1. "NOOP"：新碎片与某条旧碎片表达的意思完全相同，或已被旧碎片完全包含，没有新增有效信息（去重跳过）。target_ids 填被命中的旧记忆 ID（1个）。
2. "UPDATE"：新碎片是对某条【依然成立】的旧碎片的细节补充或更精确表述。target_ids 填被补充的旧记忆 ID（1个），merged_content 填合并后的客观事实（<=40字，以蕊蕊为主语）。
3. "SUPERSEDE"：新碎片推翻、替代、完成或更新了一条或多条旧碎片的状态（例如：物品坏了→换新了、正在吃某药→停药/换药、计划做某事→已完成、居住/体重/偏好发生改变等）。target_ids 填所有被新事实推翻/过时的旧记忆 ID。
4. "ADD"：新碎片与候选旧记忆虽话题相近，但属于不同时间点的独立事件或互不冲突的独立事实，应各自保留。target_ids 填 []。

输出 JSON 格式（不要输出任何其他内容）：
{{
  "action": "ADD" | "NOOP" | "UPDATE" | "SUPERSEDE",
  "target_ids": [123],
  "merged_content": "仅 UPDATE 时填写，否则留空字符串",
  "reason": "简短中文原因"
}}"""

    payload = {
        "model": "Qwen/Qwen2.5-72B-Instruct",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 300,
        "response_format": {"type": "json_object"}
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=20.0)
            response.raise_for_status()
            data = response.json()
            raw = data['choices'][0]['message']['content'].strip()
            if raw.startswith('```'):
                raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.DOTALL).strip()
            res = json.loads(raw)
            action = str(res.get("action", "ADD")).upper().strip()
            if action not in ("ADD", "NOOP", "UPDATE", "SUPERSEDE"):
                action = "ADD"
            raw_ids = res.get("target_ids", [])
            target_ids = []
            if isinstance(raw_ids, list):
                for x in raw_ids:
                    try:
                        ix = int(x)
                        if ix in valid_id_set and ix not in target_ids:
                            target_ids.append(ix)
                    except (ValueError, TypeError):
                        continue
            merged_content = str(res.get("merged_content") or "").strip()[:80]
            reason = str(res.get("reason") or "").strip()[:120]
            return {
                "action": action,
                "target_ids": target_ids,
                "merged_content": merged_content,
                "reason": reason
            }
    except Exception as e:
        logger.warning(f"[Memory Judge] 裁判调用异常，降级为 ADD: {e}")
        return fallback


def clean_dialogue(text: str) -> str:
    """第一重去糊眼：剥离 Base64 和大段 JSON / System块"""
    if not text:
        return text
    # 剥离 base64
    text = re.sub(r'data:image\/[^;]+;base64,[A-Za-z0-9+/=]+', '[IMAGE_DATA_REMOVED]', text)
    # 剥离 system / JSON 请求体等特殊标记
    text = re.sub(r'<system>.*?</system>', '[SYSTEM_REMOVED]', text, flags=re.DOTALL)
    # 如果有很长很规整的 {} 包裹，可能是 json 请求，这里简单处理一下超长内容，或者依赖大模型。
    return text


def get_aligned_window(raw_dialogue: str, entities: list) -> str:
    """
    针对单轮对话原文寻找实体对齐窗口（放宽至全对话或大窗口，避免割裂多实体）。
    """
    if len(raw_dialogue) <= 3000:
        return raw_dialogue
        
    for ent in entities:
        if ent and ent in raw_dialogue:
            idx = raw_dialogue.find(ent)
            start = max(0, idx - 400)
            end = min(len(raw_dialogue), idx + 400)
            return raw_dialogue[start:end]
            
    return raw_dialogue


async def reject_fragment(db, reason: str, content: str, source_id: int, model: str):
    try:
        await db.execute('''
            INSERT INTO memory_rejected (reason, raw_fragment_text, source_dialogue_id, model)
            VALUES (?, ?, ?, ?)
        ''', (reason, content, source_id, model))
        await db.commit()
    except Exception as e:
        logger.error(f"[Memory Extractor] 记录拒收失败: {e}")


async def extract_fragments(dialogue_content: str, dialogue_ids: list[int]) -> list[dict]:
    """提取记忆碎片"""
    api_key = await get_config('embedding_api_key', '')
    if not api_key:
        logger.error("[Memory Extractor] 未配置 embedding_api_key")
        return []

    # 判断整批是否全部缺失 source_dialogue_id
    if not dialogue_ids or all(did is None for did in dialogue_ids):
        raise ValueError("整批 100% 缺失 source_dialogue_id，链路已断，终止抽取！")

    db = await get_db()
    
    # 提取前取回所有原消息及时间戳供后续验证和入库使用
    source_messages = {}
    try:
        if dialogue_ids:
            placeholders = ','.join('?' * len(dialogue_ids))
            cursor = await db.execute(f"SELECT id, content, created_at as timestamp FROM memories WHERE id IN ({placeholders})", tuple(dialogue_ids))
            rows = await cursor.fetchall()
            for r in rows:
                source_messages[r['id']] = {
                    'content': r['content'],
                    'timestamp': r['timestamp']
                }
    except Exception as e:
        logger.error(f"[Memory Extractor] 获取原消息失败: {e}")

    # 清洗输入文本
    clean_content = clean_dialogue(dialogue_content)
    extractor_model = "deepseek-ai/DeepSeek-V3"

    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": extractor_model,
        "messages": [
            {"role": "system", "content": EXTRACTOR_SYSTEM_PROMPT},
            {"role": "user", "content": clean_content}
        ],
        "temperature": 0.3,
        "max_tokens": 1024,
        "response_format": {"type": "json_object"}
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=60.0)
            response.raise_for_status()
            data = response.json()
            reply_text = data['choices'][0]['message']['content'].strip()
            
            if reply_text.startswith('```json'):
                reply_text = reply_text.split('```json')[1].split('```')[0].strip()
            
            fragments = json.loads(reply_text)
            if isinstance(fragments, dict):
                for k, v in fragments.items():
                    if isinstance(v, list):
                        fragments = v
                        break
            if not isinstance(fragments, list):
                logger.error("[Memory Extractor] 格式错误")
                try:
                    await db.execute('''
                        INSERT INTO memory_rejected (reason, raw_fragment_text, source_dialogue_id, model)
                        VALUES (?, ?, ?, ?)
                    ''', ('l1_json_parse_error', reply_text[:200], None, extractor_model))
                    await db.commit()
                except Exception as ie:
                    logger.error(f"[Memory Extractor] 记录 l1_json_parse_error 失败: {ie}")
                await db.close()
                return []
                
    except Exception as e:
        logger.error(f"[Memory Extractor] 提取异常: {e}")
        try:
            reject_text = reply_text[:200] if 'reply_text' in locals() else str(e)
            await db.execute('''
                INSERT INTO memory_rejected (reason, raw_fragment_text, source_dialogue_id, model)
                VALUES (?, ?, ?, ?)
            ''', ('l1_json_parse_error', reject_text, None, extractor_model))
            await db.commit()
        except Exception as ie:
            logger.error(f"[Memory Extractor] 记录 l1_json_parse_error 失败: {ie}")
        await db.close()
        return []

    valid_fragments = []
    
    try:
        from embedding import get_embedding, cosine_similarity

        cursor = await db.execute(
            """SELECT id, content, embedding, tags, activation_count,
                      COALESCE(source_timestamp, created_at) as ts
               FROM memory_fragments
               WHERE is_active = 1 AND embedding IS NOT NULL"""
        )
        rows = await cursor.fetchall()
        parsed_existing = []
        for row in rows:
            try:
                emb_vec = json.loads(row['embedding'])
                if isinstance(emb_vec, list) and emb_vec:
                    parsed_existing.append({
                        'id': row['id'],
                        'content': row['content'],
                        'date': str(row['ts'] or '')[:10],
                        'emb_vec': emb_vec,
                    })
            except Exception:
                continue

        for frag in fragments:
            content = frag.get('content', '').strip()
            if not content:
                continue
                
            source_idx = frag.get('source_index', -1)
            source_dialogue_id = None
            if isinstance(source_idx, int) and 0 <= source_idx < len(dialogue_ids):
                source_dialogue_id = dialogue_ids[source_idx]

            # 拒收判断 1：缺失 source_dialogue_id
            if source_dialogue_id is None or source_dialogue_id not in source_messages:
                await reject_fragment(db, 'missing_source', content, source_dialogue_id, extractor_model)
                continue
                
            msg_data = source_messages[source_dialogue_id]
            raw_dialogue = clean_dialogue(msg_data['content'] or '')
            source_timestamp = msg_data['timestamp']
            
            if not source_timestamp:
                await reject_fragment(db, 'missing_source', content, source_dialogue_id, extractor_model)
                continue

            entities = frag.get('entities', [])
            if not isinstance(entities, list):
                entities = []
                
            # 拒收判断 2：窗口对齐失败（完全没匹配上任何实体）
            aligned_window = get_aligned_window(raw_dialogue, entities)
            if aligned_window is None and len(raw_dialogue) > 800:
                await reject_fragment(db, 'window_not_found', content, source_dialogue_id, extractor_model)
                continue
                
            # 拒收判断 3：第一级硬验证（实体关键词或片段关键词至少有部分出现在原文中）
            if not aligned_window:
                aligned_window = raw_dialogue
                
            ent_matched = False
            if entities:
                for ent in entities:
                    if not ent:
                        continue
                    if ent in raw_dialogue:
                        ent_matched = True
                        break
                    # 如果实体是复合词（如"薄荷味药物"），拆分检查子词（如"薄荷"）
                    sub_words = [ent[i:i+2] for i in range(len(ent)-1)]
                    if any(sw in raw_dialogue for sw in sub_words if len(sw) >= 2):
                        ent_matched = True
                        break
            else:
                # 若无显式实体列表，检查内容主要词元
                ent_matched = any(content[i:i+2] in raw_dialogue for i in range(len(content)-1))
                
            if not ent_matched:
                await reject_fragment(db, 'quote_mismatch_l1', content, source_dialogue_id, extractor_model)
                continue
                
            # 拒收判断 4：第二级语义验证（跨模型）
            l2_result = await verify_mismatch(frag, aligned_window, api_key)
            if l2_result == 'mismatch':
                await reject_fragment(db, 'quote_mismatch_l2', content, source_dialogue_id, extractor_model)
                continue
            elif l2_result == 'unavailable':
                await reject_fragment(db, 'validation_unavailable', content, source_dialogue_id, extractor_model)
                continue

            # 验证通过，计算向量并执行 Mem0 + Graphiti 入库前去重与新旧替代裁判
            tier = frag.get('tier', 3)
            energy = frag.get('energy', 5)
            polarity = frag.get('polarity', 'neutral')
            tags = frag.get('tags', '')
            
            new_emb_vector = await get_embedding(content)
            if not new_emb_vector:
                continue

            # 检索库中相似度 >= 0.65 的 Top-5 活跃碎片
            candidates = []
            for ex in parsed_existing:
                try:
                    sim = cosine_similarity(new_emb_vector, ex['emb_vec'])
                    if sim >= 0.65:
                        candidates.append({
                            'id': ex['id'],
                            'content': ex['content'],
                            'date': ex['date'],
                            'sim': sim,
                        })
                except Exception:
                    continue
            candidates.sort(key=lambda x: x['sim'], reverse=True)
            candidates = candidates[:5]

            action = "ADD"
            target_ids = []
            merged_content = ""
            judge_reason = ""

            if candidates:
                if candidates[0]['sim'] >= 0.94:
                    action = "NOOP"
                    target_ids = [candidates[0]['id']]
                    judge_reason = f"极高相似度({candidates[0]['sim']:.2f})"
                else:
                    decision = await judge_memory_action(content, candidates, api_key)
                    action = decision["action"]
                    target_ids = decision["target_ids"]
                    merged_content = decision["merged_content"]
                    judge_reason = decision["reason"]

            if action == "NOOP" and target_ids:
                hit_id = target_ids[0]
                await db.execute(
                    "UPDATE memory_fragments SET activation_count = COALESCE(activation_count, 0) + 1 WHERE id = ?",
                    (hit_id,)
                )
                logger.info(f"[Memory Judge] NOOP 跳过重复碎片: '{content}' ≈ #{hit_id} ({judge_reason})")
                continue

            if action == "UPDATE" and target_ids and merged_content:
                upd_id = target_ids[0]
                upd_emb = await get_embedding(merged_content)
                if upd_emb:
                    upd_emb_json = json.dumps(upd_emb)
                    await db.execute(
                        '''UPDATE memory_fragments
                           SET content = ?, embedding = ?,
                               updated_at = datetime('now', '+8 hours'),
                               activation_count = COALESCE(activation_count, 0) + 1
                           WHERE id = ?''',
                        (merged_content, upd_emb_json, upd_id)
                    )
                    for ex in parsed_existing:
                        if ex['id'] == upd_id:
                            ex['content'] = merged_content
                            ex['emb_vec'] = upd_emb
                            break
                    logger.info(f"[Memory Judge] UPDATE 合并碎片 #{upd_id} -> '{merged_content}' ({judge_reason})")
                    frag_copy = dict(frag)
                    frag_copy['content'] = merged_content
                    valid_fragments.append(frag_copy)
                    continue
                else:
                    action = "ADD"

            emb_json = json.dumps(new_emb_vector)
            cursor = await db.execute(
                '''INSERT INTO memory_fragments 
                   (source_dialogue_id, content, tier, energy, polarity, tags, embedding, source_timestamp, extracted_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (source_dialogue_id, content, tier, energy, polarity, tags, emb_json, source_timestamp, extractor_model)
            )
            new_id = cursor.lastrowid

            if action == "SUPERSEDE" and target_ids:
                for old_id in target_ids:
                    await db.execute(
                        '''UPDATE memory_fragments
                           SET is_active = 0,
                               status = 'outdated',
                               expired_at = datetime('now', '+8 hours'),
                               superseded_by = ?,
                               updated_at = datetime('now', '+8 hours')
                           WHERE id = ?''',
                        (new_id, old_id)
                    )
                old_id_set = set(target_ids)
                parsed_existing = [ex for ex in parsed_existing if ex['id'] not in old_id_set]
                logger.info(f"[Memory Judge] SUPERSEDE 新碎片 #{new_id} '{content}' 淘汰旧碎片 {target_ids} ({judge_reason})")
            else:
                logger.info(f"[Memory Judge] ADD 新增碎片 #{new_id} '{content}'")

            parsed_existing.append({
                'id': new_id,
                'content': content,
                'date': str(source_timestamp or '')[:10],
                'emb_vec': new_emb_vector,
            })
            valid_fragments.append(frag)
            
        await db.commit()
    except Exception as e:
        logger.error(f"[Memory Extractor] 入库异常: {e}")
    finally:
        await db.close()

    return valid_fragments
