"""剧情图/人物关系图谱 API：查询、构建任务、图片服务、关系边人工编辑"""
import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..deps import require_project
from ..errors import ApiError
from ..state import AppState

router = APIRouter(prefix='/current/graph', tags=['graph'])


def _source_root(state: AppState) -> Path:
    """当前项目的源码根（game/game 存在则下钻），与构建时基准一致"""
    from embedded_strings import resolve_source_root
    game_root = state.project_manager.project_dir(state.current_project) / 'game'
    return resolve_source_root(game_root)


def _cache_dir(state: AppState) -> Path:
    return (state.project_manager.project_dir(state.current_project)
            / 'graph_cache')


def _safe_file(base: Path, rel: str, what: str) -> Path:
    """resolve 后必须仍在 base 内（防目录穿越），存在才返回"""
    target = (base / rel).resolve()
    if not target.is_relative_to(base.resolve()) or not target.is_file():
        raise ApiError(404, 'NOT_FOUND', f'{what}不存在')
    return target


# ========== 剧情图 ==========

@router.get('/story')
async def get_story(state: AppState = Depends(require_project)):
    graph = await state.db_call(state.db.get_story_graph)
    stats = await state.db_call(state.db.story_graph_stats)
    # speaker 变量 → 展示信息（显示名/中文名/有无立绘），前端免二次请求
    characters = await state.db_call(state.db.get_characters)
    avatars = await state.db_call(state.db.get_char_avatars)
    char_map = {
        c['variable']: {
            'name': c['cn_name'] or c['display_name'] or c['variable'],
            'original': c['display_name'] or c['variable'],
            'avatar': bool(avatars.get(c['variable'] or '')),
        }
        for c in characters if c['variable']
    }
    return {**graph, 'stats': stats, 'characters': char_map}


@router.post('/story/build')
async def build_story(state: AppState = Depends(require_project)):
    game_root = (state.project_manager.project_dir(state.current_project)
                 / 'game')
    cache_dir = _cache_dir(state)
    translator = state.translator  # 无模型时 AI 场景标题阶段自动跳过

    async def body(job):
        def _build():
            from services.story_graph import build_story_graph
            return build_story_graph(
                state.db, str(game_root), str(cache_dir),
                translator=translator,
                progress=job.emit_progress, cancel_event=job.cancel_event)
        # 构建是纯 CPU/IO 同步流程，放线程池跑
        result = await state.run_sync(_build)
        job.check_cancelled()
        job.emit_progress(1.0, '完成')
        return result

    job = state.jobs.create('graph.story-build', '构建剧情图', {}, body,
                            exclusive=True)
    return {'job_id': job.id}


@router.get('/scenes')
async def get_scenes(state: AppState = Depends(require_project)):
    graph = await state.db_call(state.db.get_story_scenes)
    stats = await state.db_call(state.db.story_graph_stats)
    # speaker 变量 → 展示信息（显示名/中文名/有无立绘），前端免二次请求
    characters = await state.db_call(state.db.get_characters)
    avatars = await state.db_call(state.db.get_char_avatars)
    char_map = {
        c['variable']: {
            'name': c['cn_name'] or c['display_name'] or c['variable'],
            'original': c['display_name'] or c['variable'],
            'avatar': bool(avatars.get(c['variable'] or '')),
        }
        for c in characters if c['variable']
    }
    # 翻译进度实时计算（不取构建时快照）：
    # 1) 翻译是在图构建之后推进的，快照会永久过期
    # 2) 分母用 dialogues 表的可译条目数（tl 模板对重复原文去重，
    #    而源码行数不去重，混用导致永远到不了 100%）
    import json as _json
    tl_stats = await state.db_call(state.db.get_label_translation_stats)
    for s in graph['scenes']:
        total = translated = 0
        for lb in _json.loads(s['labels_json'] or '[]'):
            st = tl_stats.get(lb)
            if st:
                total += st['total']
                translated += st['translated']
        s['tl_total'] = total
        s['translated_count'] = translated
    return {**graph, 'stats': stats, 'characters': char_map}


@router.get('/scenes/{scene_id}/dialogue')
async def scene_dialogue(scene_id: str,
                         state: AppState = Depends(require_project)):
    """场景完整台词（下钻详情）：speaker 显示名随对话变量映射"""
    graph = await state.db_call(state.db.get_story_scenes)
    scene = next((s for s in graph['scenes'] if s['scene_id'] == scene_id),
                 None)
    if scene is None:
        raise ApiError(404, 'NOT_FOUND', f'场景不存在: {scene_id}')
    import json
    labels = json.loads(scene['labels_json'] or '[]')
    lines = await state.db_call(state.db.get_scene_dialogue, labels)
    # 对话表 character 存的是说话者变量名（翻译模板按变量记录），
    # 中文名要同时按变量名/显示名双键映射
    characters = await state.db_call(state.db.get_characters)
    cn_by_key = {}
    for c in characters:
        if c['cn_name']:
            if c['variable']:
                cn_by_key[c['variable']] = c['cn_name']
            cn_by_key[c['display_name']] = c['cn_name']
    for line in lines:
        ch = line['character']
        line['character_cn'] = cn_by_key.get(ch, '') or ch
    return {'scene_id': scene_id, 'lines': lines}


@router.get('/story/thumb/{fname}')
async def story_thumb(fname: str, state: AppState = Depends(require_project)):
    # fname = 缩略图文件名（<label>.webp），防目录穿越由 _safe_file 保证
    return _serve(_cache_dir(state), fname, '缩略图')


def _serve(base: Path, rel: str, what: str):
    return FileResponse(_safe_file(base, rel, what))


# ========== 人物关系图谱 ==========

@router.get('/relations')
async def get_relations(state: AppState = Depends(require_project)):
    characters = await state.db_call(state.db.get_characters)
    relations = await state.db_call(state.db.get_relations)
    avatars = await state.db_call(state.db.get_char_avatars)
    rows = [{
        'key': c['variable'] or c['display_name'],
        'variable': c['variable'] or '',
        'display_name': c['display_name'],
        'cn_name': c['cn_name'] or '',
        'lines': c['lines_count'],
        'faction': c.get('faction', '') or '',
        'has_avatar': (c['variable'] or '') in avatars,
    } for c in characters if not c['is_placeholder']]
    return {'characters': rows, 'relations': relations}


@router.post('/relations/build')
async def build_relations(state: AppState = Depends(require_project)):
    translator = state.translator
    if translator is None:
        raise ApiError(409, 'NO_MODEL', '请先在模型配置中设置 API')

    async def body(job):
        def _build():
            from services.relation_graph import build_relations as _b
            return _b(state.db, translator, state.current_project,
                      progress=job.emit_progress,
                      cancel_event=job.cancel_event)
        result = await state.run_sync(_build)
        job.check_cancelled()
        job.emit_progress(1.0, '完成')
        return result

    job = state.jobs.create('graph.relations-build', '构建人物关系图谱', {},
                            body, exclusive=True)
    return {'job_id': job.id}


class RelationIn(BaseModel):
    id: int = 0
    source_var: str
    target_var: str
    relation: str = ''
    category: str = 'other'
    polarity: str = ''
    description: str = ''


@router.post('/relations/edge')
async def upsert_relation(req: RelationIn,
                          state: AppState = Depends(require_project)):
    rel_id = await state.db_call(state.db.upsert_relation, req.model_dump())
    state.logger.info(
        f'关系边已保存: {req.source_var} -> {req.target_var} ({req.relation})',
        panel='ui')
    return {'ok': True, 'id': rel_id}


@router.delete('/relations/edge/{rel_id}')
async def delete_relation(rel_id: int,
                          state: AppState = Depends(require_project)):
    await state.db_call(state.db.delete_relation, rel_id)
    return {'ok': True}


@router.get('/avatar/{key}')
async def char_avatar(key: str, state: AppState = Depends(require_project)):
    avatars = await state.db_call(state.db.get_char_avatars)
    rel = avatars.get(key)
    if not rel:
        raise ApiError(404, 'NOT_FOUND', '该角色没有立绘')
    return _serve(_source_root(state), rel, '立绘')


@router.get('/avatar_circle/{key}')
async def char_avatar_circle(key: str, ring: str = '',
                             state: AppState = Depends(require_project)):
    """圆形头像：取立绘顶部方形区域（与前端 object-fit:cover;object-position:top
    一致），圆形蒙版，可选 ring=颜色 的内描边（阵营色），256x256 PNG。
    结果缓存到 graph_cache/circles/，源文件更新才重做"""
    avatars = await state.db_call(state.db.get_char_avatars)
    rel = avatars.get(key)
    if not rel:
        raise ApiError(404, 'NOT_FOUND', '该角色没有立绘')
    src = _safe_file(_source_root(state), rel, '立绘')
    ring = ring if ring.startswith('#') and len(ring) in (4, 7) else ''
    suffix = f'_{ring.lstrip("#")}' if ring else ''
    out_dir = _cache_dir(state) / 'circles'
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f'{key}{suffix}_256.png'
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        buf = await asyncio.to_thread(_make_circle_avatar, src, ring)
        out.write_bytes(buf)
    return FileResponse(out)


def _make_circle_avatar(src: Path, ring: str = '', size: int = 256) -> bytes:
    """顶部方形裁圆（可选色环）→ PNG bytes（同步函数，to_thread 调用）"""
    from io import BytesIO

    from PIL import Image, ImageDraw
    with Image.open(src) as im:
        im = im.convert('RGBA')
        w, h = im.size
        side = min(w, h)
        im = im.crop(((w - side) // 2, 0, (w - side) // 2 + side, side))
        im = im.resize((size, size), Image.LANCZOS)
        if ring:
            # 色环画在图内缘（外缘会被圆形蒙版吃掉一半），宽度随尺寸缩放
            w_ring = max(4, round(size * 7 / 128))
            d = ImageDraw.Draw(im)
            d.ellipse((4, 4, size - 4, size - 4), outline=ring, width=w_ring)
        mask = Image.new('L', (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
        im.putalpha(mask)
        buf = BytesIO()
        im.save(buf, 'PNG')
        return buf.getvalue()
