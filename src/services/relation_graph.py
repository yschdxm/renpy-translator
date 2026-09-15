"""人物关系图谱构建服务：AI 结构化关系 + 剧情节点共现统计

角色身份键 = variable（Character() 变量名），无变量名时用显示名（与
characters 表的身份规则一致）。AI 输出用显示名描述，写库前映射回身份键。

共现统计：同一剧情节点（label）内共同出场次数，来自 story_nodes 的
speakers_json（需先构建剧情图；未构建时共现为 0，不影响关系生成）。
共现 >= COOCUR_MIN 但 AI 未给出关系的角色对，以 source='cooccur' 补边，
前端以弱样式展示（客观出场强度佐证）。

AI 重算只替换 source != 'manual' 的边，人工编辑保留。
失败即抛错（与 ai_screener 一致：响亮失败，不做启发式降级）。
"""

import json
import re
import threading
from itertools import combinations
from pathlib import Path
from typing import Callable

COOCUR_MIN = 3
_BATCH = 40
_PROFILE_LIMIT = 300

CATEGORIES = {'family', 'romantic', 'friendly', 'hostile',
              'master_servant', 'work', 'other'}
POLARITIES = {'positive', 'negative', 'mixed'}

_PROMPT = """你是视觉小说剧情分析专家。以下是游戏《{game}》的角色清单（JSON 数组），每项含：
- name: 角色显示名
- cn: 中文译名（可能为空）
- lines: 台词数
- profile: 已有的角色分析档案（可能为空，含性格/背景/人物关系描述）

请输出一个 JSON 对象，含两个字段：

1. relations: 角色关系数组，每项：
   - source: 角色显示名（必须与清单中的 name 完全一致）
   - target: 另一角色显示名
   - relation: 关系短语（如 母女、师生、恋人、上司下属、情敌、主仆）
   - category: 关系类别，必须是以下之一：family(血缘/亲属) romantic(恋爱/暧昧/夫妻) friendly(友情/同伴) hostile(敌对/仇怨/竞争) master_servant(主从/上下级) work(同事/师生/职业关系) other
   - polarity: 情感极性，positive(亲近/友善) negative(敌视/厌恶) mixed(复杂/亦敌亦友) 之一
   - description: 一句话关系描述（基于档案推断，不确定的标注"推测"）

2. factions: 角色阵营/团体数组，每项：
   - name: 角色显示名
   - faction: 所属阵营/团体/家族/组织的简称（如"学生会""反抗军""主角团"；不属于任何团体填 ""）

只输出有实际关系的角色对，同义反复/纯路人不要输出。关系方向尽量语义自然
（source 是 relation 的主语方，如 source=母亲 target=女儿 relation=母女）。
只输出 JSON 对象，不要输出任何其他文字。

角色清单：
{chars_json}"""

# prompt 缓存优化（与翻译批同一原理：DeepSeek/OpenAI 按 token 0 严格前缀
# 匹配，命中 ~0.1x 计价）：全量花名册作为所有批次共享的常量前缀，批次间
# 只有末尾的"本批角色"小段变化——第 2 批起花名册前缀全部命中。
# 花名册超大时（>400 角色）退回逐批独立提示词（前缀收益不敌上下文膨胀）
_ROSTER_PREFIX_MAX = 400

_HEAD = """你是视觉小说剧情分析专家。以下是游戏《{game}》的完整角色清单（JSON 数组），每项含：
- name: 角色显示名
- cn: 中文译名（可能为空）
- lines: 台词数
- profile: 已有的角色分析档案（可能为空，含性格/背景/人物关系描述）

请输出一个 JSON 对象，含两个字段：

1. relations: 角色关系数组，每项：
   - source: 角色显示名（必须与清单中的 name 完全一致）
   - target: 另一角色显示名
   - relation: 关系短语（如 母女、师生、恋人、上司下属、情敌、主仆）
   - category: 关系类别，必须是以下之一：family(血缘/亲属) romantic(恋爱/暧昧/夫妻) friendly(友情/同伴) hostile(敌对/仇怨/竞争) master_servant(主从/上下级) work(同事/师生/职业关系) other
   - polarity: 情感极性，positive(亲近/友善) negative(敌视/厌恶) mixed(复杂/亦敌亦友) 之一
   - description: 一句话关系描述（基于档案推断，不确定的标注"推测"）

2. factions: 角色阵营/团体数组，每项：
   - name: 角色显示名
   - faction: 所属阵营/团体/家族/组织的简称（如"学生会""反抗军""主角团"；不属于任何团体填 ""）

只输出有实际关系的角色对，同义反复/纯路人不要输出。关系方向尽量语义自然
（source 是 relation 的主语方，如 source=母亲 target=女儿 relation=母女）。
只输出 JSON 对象，不要输出任何其他文字。

完整角色清单：
{roster_json}

请只针对以下角色推断他们与清单中任意角色的关系（factions 也只需覆盖他们）："""

# 花名册超大时的逐批回退提示词（同原 _PROMPT 结构）
_FALLBACK_PROMPT = _PROMPT


def _char_key(c: dict) -> str:
    return c['variable'] or c['display_name']


def _cooccurrence(db) -> dict:
    """从 story_nodes 计算角色对共现次数 {(keyA, keyB): n}（key 有序对）"""
    graph = db.get_story_graph()
    pairs = {}
    for n in graph['nodes']:
        try:
            speakers = json.loads(n['speakers_json'] or '[]')
        except json.JSONDecodeError:
            continue
        for a, b in combinations(sorted(set(speakers)), 2):
            pairs[(a, b)] = pairs.get((a, b), 0) + 1
    return pairs


def _call_ai(translator, chars: list, game_name: str,
             progress: Callable[[float, str], None] = None) -> tuple[list, dict]:
    """分批 AI 调用，返回 (显示名关系列表, {显示名: faction})；
    每批回报进度（批次 i/N + 角色区间），占 0.15~0.78 进度段。
    prompt 缓存：花名册常量前缀共享（见 _ROSTER_PREFIX_MAX），
    命中量经 translator.cache_stats_delta 观测"""
    use_roster = 1 < len(chars) <= _ROSTER_PREFIX_MAX
    roster_prefix = ''
    if use_roster:
        roster_prefix = _HEAD.format(
            game=game_name,
            roster_json=json.dumps(chars, ensure_ascii=False))

    edges = []
    factions = {}
    total_batches = max(1, (len(chars) + _BATCH - 1) // _BATCH)
    cache_before = (translator.cache_hit_total, translator.cache_prompt_total)
    for bi, start in enumerate(range(0, len(chars), _BATCH), 1):
        batch = chars[start:start + _BATCH]
        if progress:
            progress(0.15 + 0.63 * (bi - 1) / total_batches,
                     f'AI 推断关系 第 {bi}/{total_batches} 批'
                     f'（角色 {start + 1}-{start + len(batch)}/{len(chars)}）')
        if use_roster:
            names = [c['name'] for c in batch]
            prompt = roster_prefix + json.dumps(names, ensure_ascii=False)
        else:
            prompt = _FALLBACK_PROMPT.format(
                game=game_name,
                chars_json=json.dumps(batch, ensure_ascii=False))
        # 风控/偶发解析失败重试（每次真实重新调用，与同批翻译口径一致）
        parsed = None
        last_err = None
        for _ in range(3):
            try:
                result = translator.analyze_text(
                    prompt, max_tokens=max(translator.config.max_tokens,
                                           8000))
                m = re.search(r'\{.*\}', result, re.S)
                if not m:
                    raise ValueError(
                        f'关系生成返回无法解析: {result[:100]}')
                parsed = json.loads(m.group(0))
                break
            except Exception as e:
                last_err = e
        if parsed is None:
            raise ValueError(f'关系生成连续失败: {last_err}')
        edges.extend(e for e in parsed.get('relations', [])
                     if isinstance(e, dict))
        for f in parsed.get('factions', []):
            if isinstance(f, dict) and f.get('name'):
                factions.setdefault(str(f['name']),
                                    str(f.get('faction', '') or ''))
    if not edges:
        raise ValueError('关系生成返回为空')
    hit, total = translator.cache_stats_delta(cache_before)
    if total:
        print(f'[关系推断] prompt 缓存命中 {hit}/{total} tokens'
              f'（{100 * hit / total:.0f}%）')
    return edges, factions


def build_relations(db, translator, game_name: str,
                    progress: Callable[[float, str], None] = None,
                    cancel_event: threading.Event = None) -> dict:
    """构建人物关系并入库，返回统计 {relations, cooccur, characters}"""
    characters = [c for c in db.get_characters() if not c['is_placeholder']]
    if not characters:
        raise ValueError('项目中没有角色数据')
    if translator is None:
        raise ValueError('未配置模型，无法生成人物关系')

    if progress:
        progress(0.05, '统计角色共现')
    cooccur = _cooccurrence(db)

    if progress:
        progress(0.15, 'AI 推断关系')
    # 供 AI 的精简角色卡
    cards = []
    for c in characters:
        profile = {}
        if c['profile_json']:
            try:
                profile = json.loads(c['profile_json'])
            except json.JSONDecodeError:
                profile = {}
        profile_text = '; '.join(
            f'{k}:{v}' for k, v in profile.items())[:_PROFILE_LIMIT]
        cards.append({'name': c['display_name'], 'cn': c['cn_name'] or '',
                      'lines': c['lines_count'], 'profile': profile_text})
    ai_edges, ai_factions = _call_ai(translator, cards, game_name,
                                     progress=progress)

    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError('关系图谱构建已取消')

    if progress:
        progress(0.8, '合并写库')
    # 显示名 → 身份键（同名角色并存时取第一个，AI 输出粒度无法区分同名）
    name_to_key = {}
    for c in characters:
        name_to_key.setdefault(c['display_name'], _char_key(c))
    valid_keys = {_char_key(c) for c in characters}

    relations = []
    seen_pairs = set()
    for e in ai_edges:
        sk = name_to_key.get(str(e.get('source', '')))
        tk = name_to_key.get(str(e.get('target', '')))
        if not sk or not tk or sk == tk:
            continue
        pair = tuple(sorted((sk, tk)))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        category = str(e.get('category', ''))
        polarity = str(e.get('polarity', ''))
        relations.append({
            'source_var': sk, 'target_var': tk,
            'relation': str(e.get('relation', ''))[:50],
            'category': category if category in CATEGORIES else 'other',
            'polarity': polarity if polarity in POLARITIES else '',
            'description': str(e.get('description', ''))[:500],
            'cooccurrence': cooccur.get(pair, 0),
            'source': 'ai',
        })

    # 共现补边：AI 未覆盖的高共现角色对（身份键需都在有效集合内）
    n_cooccur = 0
    for (a, b), count in cooccur.items():
        if count < COOCUR_MIN or (a, b) in seen_pairs:
            continue
        if a not in valid_keys or b not in valid_keys:
            continue
        relations.append({'source_var': a, 'target_var': b, 'relation': '',
                          'category': 'cooccur', 'polarity': '',
                          'description': '', 'cooccurrence': count,
                          'source': 'cooccur'})
        n_cooccur += 1

    # 阵营：显示名 → 身份键后按角色写入（空 faction 不覆盖已有值）
    faction_map = {}
    for name, faction in ai_factions.items():
        key = name_to_key.get(name)
        if key and faction:
            faction_map[key] = faction[:30]

    db.replace_ai_relations(relations)
    db.set_character_factions(faction_map)
    if progress:
        progress(1.0, '完成')
    return {'relations': len(relations) - n_cooccur, 'cooccur': n_cooccur,
            'characters': len(characters), 'factions': len(faction_map)}
