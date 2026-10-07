"""翻译文本导入：JSON / 翻译Word 回导译文

- JSON（本工具导出格式）：按原文匹配，顺序无关；同原文多条全部写回
  （与 game_export.build_translation_dict 的 dict 化语义互逆）
- 翻译Word（tword，仅原文、每条一段）：按段落顺序与条目一一对应，
  条目顺序与 tword 导出完全同源（ordered_entries），段落数不一致直接报错

两段式导入：build_*_plan 生成计划 + digest（预览），apply_plan 前重算
比对 digest（确认），防止预览后库内数据变化造成错配。
"""

import hashlib
import io
import json

from .text_export import EXPORT_TYPES

# json 导出文件的顶层键与行内字段（见 text_export.SECTION_KEYS / COLUMN_DEFS）
_JSON_SECTIONS = {
    'dialogue': ('dialogues', 'original', 'translated'),
    'ui': ('ui_texts', 'original', 'translated'),
    'names': ('names', 'original', 'translated'),
    'glossary': ('glossary', 'en', 'cn'),
}


def ordered_entries(db, types: list[str], flt: dict | None = None) -> list[dict]:
    """tword 导出/导入对齐的条目序列：规范类型序 + 剧情书写序。

    每项 {type, key, original}：对话/UI/人名 key 为 id，术语 key 为 en_term。
    读取路径与 text_export.collect_data 完全一致（同 repo 方法、同排序）。
    """
    entries = []
    for t in EXPORT_TYPES:
        if t not in types:
            continue
        if t == 'dialogue':
            rows = (db.get_dialogues_filtered(
                        flt.get('filter_mode', 'all'), flt.get('character', ''),
                        flt.get('search', ''))
                    if flt and flt.get('content_type') == 'dialogue'
                    else db.get_all_dialogues())
            entries += [{'type': t, 'key': r['id'], 'original': r['original_text']}
                        for r in rows]
        elif t == 'ui':
            rows = (db.get_ui_texts_filtered(
                        flt.get('filter_mode', 'all'), flt.get('search', ''))
                    if flt and flt.get('content_type') == 'ui'
                    else db.get_all_ui_texts())
            entries += [{'type': t, 'key': r['id'], 'original': r['original_text']}
                        for r in rows]
        elif t == 'names':
            entries += [{'type': t, 'key': r['id'], 'original': r['display_name']}
                        for r in db.get_characters()]
        else:
            entries += [{'type': t, 'key': r['en_term'], 'original': r['en_term']}
                        for r in db.get_glossary_rows()]
    return entries


# ---- 解析 ----

def parse_json_import(text: str) -> dict[str, list[tuple[str, str]]]:
    """解析导出的 JSON：{type: [(原文, 译文)]}（同原文取最后出现，空译文跳过）"""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f'JSON 解析失败: {e}') from e
    if not isinstance(doc, dict) or 'format_version' not in doc:
        raise ValueError('不是本工具导出的 JSON（缺少 format_version）')
    out = {}
    for t, (section, key_o, key_t) in _JSON_SECTIONS.items():
        rows = doc.get(section)
        if not isinstance(rows, list):
            continue
        latest = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            o, tr = r.get(key_o), r.get(key_t)
            if o and tr:
                latest[o] = tr
        if latest:
            out[t] = list(latest.items())
    if not out:
        raise ValueError('JSON 中没有可导入的译文（需要原文与译文均非空的条目）')
    return out


def read_tword_lines(content: bytes) -> list[str]:
    """读翻译 Word 的段落文本（<w:br/> 还原为换行）；去掉末尾空段
    （Word 保存常带尾部空段），中间空段保留以维持对齐。"""
    from docx import Document

    doc = Document(io.BytesIO(content))
    lines = [p.text for p in doc.paragraphs]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


# ---- 计划（预览） ----

def build_json_plan(db, pairs_by_type: dict) -> tuple[list[dict], dict]:
    """JSON 导入计划：按原文匹配生成变更项 [{type, key, original, translated}]，
    返回 (plan, 各类型统计)。当前译文已相同的跳过（unchanged）。"""
    plan, stats = [], {}
    for t, pairs in pairs_by_type.items():
        matched = unmatched = unchanged = 0
        if t == 'dialogue' or t == 'ui':
            rows = (db.get_all_dialogues() if t == 'dialogue'
                    else db.get_all_ui_texts())
            by_orig = {}
            for r in rows:
                by_orig.setdefault(r['original_text'], []).append(r)
            for o, tr in pairs:
                hit = by_orig.get(o)
                if not hit:
                    unmatched += 1
                    continue
                for r in hit:
                    if (r['translated_text'] or '') == tr:
                        unchanged += 1
                        continue
                    plan.append({'type': t, 'key': r['id'],
                                 'original': o, 'translated': tr})
                    matched += 1
        elif t == 'names':
            by_name = {}
            for c in db.get_characters():
                by_name.setdefault(c['display_name'], []).append(c)
            for o, tr in pairs:
                hit = by_name.get(o)
                if not hit:
                    unmatched += 1
                    continue
                for c in hit:
                    if (c['cn_name'] or '') == tr:
                        unchanged += 1
                        continue
                    plan.append({'type': t, 'key': c['id'],
                                 'original': o, 'translated': tr})
                    matched += 1
        else:
            existing = {r['en_term']: r for r in db.get_glossary_rows()}
            for o, tr in pairs:
                cur = existing.get(o)
                if cur is None:
                    plan.append({'type': t, 'key': o,
                                 'original': o, 'translated': tr})
                    matched += 1  # 新增术语
                elif (cur['cn_term'] or '') == tr:
                    unchanged += 1
                else:
                    plan.append({'type': t, 'key': o,
                                 'original': o, 'translated': tr})
                    matched += 1
        stats[t] = {'matched': matched, 'unmatched': unmatched,
                    'unchanged': unchanged}
    return plan, stats


def build_tword_plan(db, lines: list[str], types: list[str],
                     flt: dict | None = None) -> tuple[list[dict], dict]:
    """翻译Word 导入计划：段落 i 对应条目 i（顺序对齐）。"""
    entries = ordered_entries(db, types, flt)
    if len(lines) != len(entries):
        raise ValueError(
            f'段落数（{len(lines)}）与条目数（{len(entries)}）不一致：'
            '导入的文件需与所选内容类型、范围同导出时完全一致')
    plan, stats = [], {}
    for e, text in zip(entries, lines):
        tr = text.strip()
        st = stats.setdefault(e['type'], {'matched': 0, 'unchanged': 0,
                                          'skipped_empty': 0})
        if not tr:
            st['skipped_empty'] += 1  # 空段不写回，但占位维持对齐
            continue
        if tr == (e['original'] or '').strip():
            st['unchanged'] += 1  # 译者未改动该行（保留原文）
            continue
        plan.append({'type': e['type'], 'key': e['key'],
                     'original': e['original'], 'translated': text})
        st['matched'] += 1
    return plan, stats


def plan_digest(kind: str, plan: list[dict]) -> str:
    """计划签名：apply 时重算比对，防止预览后数据变化错配"""
    return hashlib.sha256(json.dumps(
        [kind, plan], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# ---- 应用（确认） ----

def apply_plan(db, plan: list[dict]) -> dict:
    """按计划写回译文，返回各类型写回条数（术语含新增）"""
    groups: dict[str, list] = {t: [] for t in EXPORT_TYPES}
    for p in plan:
        groups[p['type']].append((p['key'], p['translated']))
    counts = {}
    if groups['dialogue']:
        db.update_dialogues_batch(groups['dialogue'])
        counts['dialogue'] = len(groups['dialogue'])
    if groups['ui']:
        db.update_ui_texts_batch(groups['ui'])
        counts['ui'] = len(groups['ui'])
    if groups['names']:
        db.update_cn_names_batch(groups['names'])
        counts['names'] = len(groups['names'])
    if groups['glossary']:
        updated, inserted = db.upsert_glossary_batch(groups['glossary'])
        counts['glossary'] = updated + inserted
    return counts
