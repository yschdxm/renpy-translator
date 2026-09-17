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
from scene_composer import (
    ImageResolver, compose_scene_candidates, state_after, thumb_key)

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
                      cancel_event: threading.Event = None,
                      incremental: bool = False,
                      engine_render: bool = False) -> dict:
    """构建剧情图（label 级 + 场景级）并入库，返回统计

    incremental=False（默认，重建语义）：所有场景重新做 AI 标题分析；
    incremental=True（独立的增量功能）：已有标题保留，只分析新场景。
    engine_render=True（实验）：场景缩略图与立绘用 Ren'Py 引擎
    沙盒渲染（真 ATL/分层/程序化角色），沙盒启动失败直接报错
    """
    source_root = resolve_source_root(Path(game_root))

    if progress:
        progress(0.02, '解析控制流')
    result = FlowParser(str(source_root)).parse()
    nodes, edges = result['nodes'], result['edges']

    if progress:
        progress(0.10, '构建图像索引')
    resolver = ImageResolver(str(source_root))

    # 逐节点合成缩略图候选（10% ~ 60% 进度段）：多帧出图供手动更换。
    # 画面状态沿剧情图传播——Ren'Py 的画面跨 label 持续，没有自己
    # scene 语句的 label 继承前驱的背景（BFS 顺序，前驱取最早访问的）
    cache = Path(cache_dir)
    total = max(len(nodes), 1)
    node_by = {n.label: n for n in nodes}
    preds: dict = {}
    adj: dict = {}
    for e in edges:
        if e.target is not None and e.target in node_by:
            adj.setdefault(e.source, []).append(e.target)
            preds.setdefault(e.target, []).append(e.source)
    order = []
    seen = set()
    queue = [n.label for n in nodes if n.is_entry] or \
        ([nodes[0].label] if nodes else [])
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        order.append(cur)
        queue.extend(t for t in adj.get(cur, []) if t not in seen)
    order.extend(n.label for n in nodes if n.label not in seen)

    final_states: dict = {}
    # 增量缓存：label → (内容哈希, 候选文件)。哈希覆盖 ops/取点/初始
    # 状态/合成器版本——任一变化才重渲染，重建时未变 label 零成本复用
    manifest_path = cache / '_thumb_manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        manifest = {}
    new_manifest = {}
    reused = 0
    sandbox_entries = []  # 引擎渲染模式待渲染项 (label, ops, first, last, init, key)
    for i, label in enumerate(order):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError('剧情图构建已取消')
        n = node_by[label]
        # 前驱状态：BFS 序最早的前驱的最终态（近似线性流的画面继承）
        init = None
        for p in preds.get(label, []):
            if p in final_states:
                init = final_states[p]
                break
        if n.scene_ops or init is not None:
            key = thumb_key(n.scene_ops, n.first_dlg_line,
                            n.last_dlg_line, init)
            if engine_render:
                key += ':E'
            hit = manifest.get(label)
            if (hit and hit.get('key') == key
                    and all((cache / f).exists()
                            for f in hit.get('files', []))):
                names = hit['files']
                fin = state_after(n.scene_ops, init)
                reused += 1
                final_states[label] = fin
                new_manifest[label] = hit
                if names:
                    n.thumb_file = names[0]
                    n.thumb_files = names
            elif engine_render:
                # 引擎模式：状态机照算（传播需要），渲染统一走沙盒
                fin = state_after(n.scene_ops, init)
                final_states[label] = fin
                sandbox_entries.append(
                    (label, n.scene_ops, n.first_dlg_line,
                     n.last_dlg_line, init, key))
            else:
                names, fin = compose_scene_candidates(
                    resolver, n.scene_ops, n.first_dlg_line,
                    n.last_dlg_line, str(cache), n.label, initial_state=init)
                final_states[label] = fin
                new_manifest[label] = {'key': key, 'files': names}
                if names:
                    n.thumb_file = names[0]
                    n.thumb_files = names
        if i % 20 == 0 and progress:
            progress(0.10 + 0.50 * i / total,
                     f'合成场景缩略图 {i}/{total}（复用 {reused}）')

    # ---- 引擎渲染（实验）：Ren'Py 沙盒整批渲染；启动失败直接报错
    #（不回退 Pillow——用户选了引擎渲染就要看到引擎的结果或明确的失败）
    engine_avatars = {}
    if engine_render and sandbox_entries:
        if progress:
            progress(0.30, f'引擎渲染沙盒启动（{len(sandbox_entries)} 个 label）')
        from render_sandbox import (
            build_avatar_jobs, build_jobs, harvest, harvest_avatars,
            run_sandbox)
        jobs = build_jobs([(lb, ops, f, la, init)
                           for lb, ops, f, la, init, _ in
                           sandbox_entries])
        # 立绘头像任务并入同一次沙盒运行：tag 非空走真分层立绘，
        # 空走 draw_person（程序化角色 best-effort）
        chars = [c for c in db.get_characters()
                 if not c['is_placeholder'] and c['variable']]
        for c in chars:
            c['image_tag'] = resolver.char_image_tags.get(
                c['variable'], '') or (
                c['variable'] if resolver.resolve_tag_variants(
                    c['variable']) else '')
        jobs += build_avatar_jobs(chars)
        results = run_sandbox(str(source_root), jobs)
        thumbs_map = harvest(results, str(cache))
        engine_avatars = harvest_avatars(results, str(cache))
        for lb, ops, f, la, init, key in sandbox_entries:
            n = node_by[lb]
            names = thumbs_map.get(lb, [])
            new_manifest[lb] = {'key': key, 'files': names}
            if names:
                n.thumb_file = names[0]
                n.thumb_files = names
        if progress:
            progress(0.60, '引擎渲染完成')

    try:
        manifest_path.write_text(json.dumps(new_manifest),
                                 encoding='utf-8')
    except OSError:
        pass

    if progress:
        progress(0.62, '解析角色立绘')
    # 角色立绘：对 characters 表全量角色解析（speaker 必在其中，
    # 关系图谱也复用这份映射）；同时收集候选变体（手动换头像用）
    avatars = {}
    avatar_cands = {}
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
        cands = [
            p.relative_to(source_root).as_posix()
            for p in resolver.avatar_candidates(c['variable'],
                                                c['display_name'])]
        if cands:
            avatar_cands[c['variable']] = cands
    # 引擎渲染立绘覆盖文件解析结果（引擎产物是真实渲染图，优先级最高）
    for var, path in engine_avatars.items():
        avatars[var] = path
        avatar_cands[var] = [path]

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
    db.replace_char_avatars(avatars, avatar_cands)

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
    # 全量重建：所有场景重新分析（AI 输出非确定，重建语义=重来）；
    # 增量模式：已有标题的场景保留，只补新场景（独立功能入口传入）
    existing_titles = {}
    if incremental:
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
        'thumbs_json': json.dumps(s.thumb_candidates,
                                  ensure_ascii=False),
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
