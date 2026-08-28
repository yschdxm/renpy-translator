"""剧情图构建服务：控制流解析 → 场景缩略图合成 → 场景聚合 → AI 语义 → 入库

两级产出：label 级（story_nodes/story_edges，下钻与聚合中间数据）
和场景级（story_scenes/story_scene_edges，故事图主体）。
AI 场景标题/摘要为可选阶段：无模型配置时跳过（stats.ai_titles=0）。
同步函数，由 job 在线程池中调用。
缩略图写入 projects/<name>/graph_cache/（派生物，删除项目时随目录清除）。
"""

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from embedded_strings import resolve_source_root
from flow_parser import FlowParser
from renpy_parser import RenpyParser
from scene_builder import build_scenes
from scene_composer import ImageResolver, compose_scene

_unescape = RenpyParser._unescape_renpy


def _translation_map(db) -> dict:
    """原文 → 译文 映射（dialogues + ui_texts 两表合并，键统一反转义）

    menu 选项原文存于 ui_texts（SDK strings 块），台词存于 dialogues；
    同一原文多译本（上下文差异）取先遇到的非空译文。
    """
    trans = {}
    for rows in (db.get_all_dialogues(), db.get_all_ui_texts()):
        for r in rows:
            if not r['is_translated'] or not r['translated_text']:
                continue
            key = _unescape(r['original_text'])
            trans.setdefault(key, r['translated_text'])
    return trans


def build_story_graph(db, game_root: str, cache_dir: str,
                      translator=None,
                      progress: Callable[[float, str], None] = None,
                      cancel_event: threading.Event = None) -> dict:
    """构建剧情图（label 级 + 场景级）并入库，返回统计"""
    source_root = resolve_source_root(Path(game_root))

    if progress:
        progress(0.02, '解析控制流')
    result = FlowParser(str(source_root)).parse()
    nodes, edges = result['nodes'], result['edges']

    if progress:
        progress(0.10, '构建图像索引')
    resolver = ImageResolver(str(source_root))

    # 逐节点合成缩略图（10% ~ 60% 进度段）
    cache = Path(cache_dir)
    total = max(len(nodes), 1)
    for i, n in enumerate(nodes):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError('剧情图构建已取消')
        if n.scene_ops:
            out = cache / f'{n.label}.webp'
            if compose_scene(resolver, n.scene_ops, n.first_dlg_line,
                             n.last_dlg_line, str(out)):
                n.thumb_file = out.name
        if progress and i % 20 == 0:
            progress(0.10 + 0.50 * i / total, f'合成场景缩略图 {i}/{total}')

    if progress:
        progress(0.62, '解析角色立绘')
    # 角色立绘：对 characters 表全量角色解析（speaker 必在其中，
    # 关系图谱也复用这份映射）
    avatars = {}
    char_name = {}
    for c in db.get_characters():
        if c['is_placeholder'] or not c['variable']:
            continue
        char_name[c['variable']] = c['cn_name'] or c['display_name']
        sprite = resolver.resolve_char_sprite(c['variable'],
                                              c['display_name'])
        if sprite:
            avatars[c['variable']] = sprite.relative_to(
                source_root).as_posix()

    # 译文回填到 flow 对象（label 级入库与场景聚合共用）
    if progress:
        progress(0.66, '映射已翻译文本')
    trans = _translation_map(db)
    for n in nodes:
        n.first_text_cn = trans.get(n.first_text, '')
    for e in edges:
        if e.branch == 'menu':
            e.text_cn = trans.get(e.text, '')

    # ---- label 级入库 ----
    if progress:
        progress(0.70, '写入 label 级数据')
    node_rows = [{
        'label': n.label, 'file': n.file,
        'line_start': n.line_start, 'line_end': n.line_end,
        'speakers_json': json.dumps(n.speakers, ensure_ascii=False),
        'dialogue_count': n.dialogue_count,
        'first_text': n.first_text,
        'first_text_cn': n.first_text_cn,
        'is_entry': n.is_entry, 'has_return': n.has_return,
        'is_terminal': n.is_terminal,
        'thumb_file': n.thumb_file,
    } for n in nodes]
    edge_rows = [{
        'source': e.source, 'target': e.target, 'kind': e.kind,
        'branch': e.branch, 'text': e.text,
        'text_cn': e.text_cn,
        'expr': e.expr, 'line': e.line,
    } for e in edges]
    db.replace_story_graph(node_rows, edge_rows)
    db.replace_char_avatars(avatars)

    # ---- 场景聚合 ----
    if progress:
        progress(0.76, '聚合剧情场景')
    agg = build_scenes(nodes, edges)
    scenes, scene_edges = agg['scenes'], agg['edges']

    # 翻译进度：dialogues 表按 label 统计 → 场景内累加
    tl_stats = db.get_label_translation_stats()
    for s in scenes:
        translated = 0
        for lb in s.labels:
            translated += tl_stats.get(lb, {}).get('translated', 0)
        s.translated_count = translated

    # ---- AI 场景标题/摘要（可选阶段，无模型跳过）----
    # 增量保留：已有标题的场景不重新生成（重建只补新场景，零重复 token 成本）
    existing_titles = {
        s['scene_id']: {'title': s['title'], 'summary': s['summary']}
        for s in db.get_story_scenes()['scenes'] if s['title']
    }
    titles = dict(existing_titles)
    need_ai = [s for s in scenes if s.scene_id not in existing_titles]
    if translator is not None and need_ai:
        from services.scene_summarizer import summarize_scenes
        base = 0.80

        def _ai_progress(frac, text):
            if progress:
                progress(base + 0.14 * frac, text)

        titles.update(summarize_scenes(
            need_ai, translator,
            char_name=lambda v: char_name.get(v, v),
            progress=_ai_progress, cancel_event=cancel_event))

    if progress:
        progress(0.95, '写入场景数据')
    scene_rows = [{
        'scene_id': s.scene_id,
        'title': titles.get(s.scene_id, {}).get('title', ''),
        'summary': titles.get(s.scene_id, {}).get('summary', ''),
        'labels_json': json.dumps(s.labels, ensure_ascii=False),
        'speakers_json': json.dumps(s.speakers, ensure_ascii=False),
        'first_text': s.first_text, 'first_text_cn': s.first_text_cn,
        'dialogue_count': s.dialogue_count,
        'translated_count': s.translated_count,
        'thumb_file': s.thumb_file,
        'is_entry': s.is_entry, 'is_ending': s.is_ending,
        'is_return': s.is_return,
        'file_path': s.file_path, 'line_start': s.line_start,
    } for s in scenes]
    scene_edge_rows = [{
        'source': e.source, 'target': e.target,
        'texts_json': json.dumps(e.texts, ensure_ascii=False),
        'texts_cn_json': json.dumps(e.texts_cn, ensure_ascii=False),
        'branch': e.branch, 'has_call': e.has_call,
        'unresolved': e.unresolved,
    } for e in scene_edges]
    db.replace_story_scenes(scene_rows, scene_edge_rows)

    stats = {'nodes': len(nodes), 'edges': len(edges),
             'unresolved': sum(1 for e in edges if e.target is None),
             'thumbs': sum(1 for n in nodes if n.thumb_file),
             'avatars': len(avatars),
             'scenes': len(scenes),
             'scene_edges': len(scene_edges),
             'endings': sum(1 for s in scenes if s.is_ending),
             'unreachable': len(agg['unreachable']),
             'ai_titles': len(titles)}
    if progress:
        progress(1.0, '完成')
    return stats
