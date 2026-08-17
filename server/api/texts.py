"""文本翻译 API：UI 字符串与对话（分页/编辑/单翻/批翻任务/上下文/风格指南/重建出处）"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import require_project
from ..errors import ApiError
from ..state import AppState

router = APIRouter(prefix='/current', tags=['texts'])

CONTENT_TYPES = ('ui', 'dialogue')


def _check_ct(content_type: str):
    if content_type not in CONTENT_TYPES:
        raise ApiError(404, 'BAD_TYPE', f'未知内容类型: {content_type}')


def _service(state: AppState):
    if not state.translation_service:
        raise ApiError(409, 'NO_TRANSLATOR', '请先配置翻译器（模型配置）')
    return state.translation_service


# ---- 查询与编辑 ----

@router.get('/texts/{content_type}')
async def list_texts(content_type: str, page: int = 0, size: int = 50,
                     filter_mode: str = 'all', search: str = '',
                     character: str = '',
                     sort_by: str = '', sort_order: str = 'asc',
                     state: AppState = Depends(require_project)):
    _check_ct(content_type)
    if content_type == 'dialogue':
        rows, total = await state.db_call(
            state.db.get_dialogues_page, page, size, filter_mode, character,
            search, sort_by, sort_order)
    else:
        rows, total = await state.db_call(
            state.db.get_ui_texts_page, page, size, filter_mode, search,
            sort_by, sort_order)
    return {'rows': rows, 'total': total}


class UpdateTextIn(BaseModel):
    translated_text: str


@router.patch('/texts/{content_type}/{item_id}')
async def update_text(content_type: str, item_id: int, req: UpdateTextIn,
                      state: AppState = Depends(require_project)):
    _check_ct(content_type)
    if content_type == 'dialogue':
        await state.db_call(state.db.update_dialogue, item_id, req.translated_text)
    else:
        await state.db_call(state.db.update_ui_text, item_id, req.translated_text)
    return {'ok': True}


@router.get('/texts/{content_type}/characters')
async def list_characters(content_type: str,
                          state: AppState = Depends(require_project)):
    _check_ct(content_type)
    chars = await state.db_call(state.db.get_dialogue_characters)
    var_map = await state.db_call(state.db.get_variable_map)
    return {'characters': chars, 'variable_map': var_map}


@router.get('/texts/{content_type}/{item_id}/context')
async def get_context(content_type: str, item_id: int, n: int = 5,
                      state: AppState = Depends(require_project)):
    _check_ct(content_type)
    before, after = await state.db_call(
        state.db.get_dialogue_context, item_id, content_type, n)
    return {'before': before, 'after': after}


# ---- 翻译 ----

@router.post('/texts/{content_type}/{item_id}/translate')
async def translate_one(content_type: str, item_id: int,
                        state: AppState = Depends(require_project)):
    _check_ct(content_type)
    service = _service(state)
    if content_type == 'dialogue':
        item = await state.db_call(state.db.get_dialogue, item_id)
    else:
        item = await state.db_call(state.db.get_ui_text, item_id)
    if not item:
        raise ApiError(404, 'NOT_FOUND', f'条目不存在: {item_id}')
    ok = await service.translate_single(
        item_id=item_id, content_type=content_type,
        original_text=item['original_text'],
        character=item.get('character', '') or '')
    if not ok:
        raise ApiError(502, 'TRANSLATE_FAILED', '翻译失败（详见日志）')
    if content_type == 'dialogue':
        item = await state.db_call(state.db.get_dialogue, item_id)
    else:
        item = await state.db_call(state.db.get_ui_text, item_id)
    return {'translated_text': item['translated_text']}


async def _check_dialogue_prerequisites(state: AppState):
    """对话翻译前置：人名全部翻译 + 角色全部分析（移植 _check_prerequisites）"""
    names = await state.db_call(state.db.get_char_dict_count)
    if names['untranslated'] > 0:
        raise ApiError(
            409, 'PREREQUISITE',
            f'还有 {names["untranslated"]} 个人名未翻译，请先在「人名翻译」完成')
    profiles = await state.db_call(state.db.get_all_profiles)
    chars = await state.db_call(state.db.get_characters)
    unanalyzed = [c['display_name'] for c in chars
                  if not c['is_placeholder'] and c['display_name'] not in profiles]
    if unanalyzed:
        raise ApiError(
            409, 'PREREQUISITE',
            f'还有 {len(unanalyzed)} 个角色未分析（{", ".join(unanalyzed[:5])}…），'
            '请先在「人名翻译」完成分析')


def _make_translate_job(state: AppState, content_type: str, items: list):
    """批翻译任务体：逐批翻译，批间响应取消

    单批解析失败（句数不匹配）由 service 暂存到 failed_batches 并返回空 dict，
    任务继续后续批次；结束时汇总暂存数量。
    """
    async def body(job):
        service = _service(state)
        batches = await service.prepare_batches(items, content_type)
        total_items = len(items)
        done = 0
        stashed = 0
        job.emit_log(f'共 {total_items} 条，分 {len(batches)} 批')
        for i, batch in enumerate(batches):
            job.check_cancelled()
            results = await service.translate_batch(batch, content_type)
            stashed += len(batch) - len(results)
            done += len(batch)
            job.emit_progress(done / total_items,
                              f'已翻译 {done}/{total_items}（第 {i+1}/{len(batches)} 批）')
        if stashed:
            job.emit_log(f'{stashed} 条未译出已暂存，'
                         '可在页面「失败条目」中重试/单翻/手动')
        return {'translated': done - stashed, 'total': total_items,
                'stashed': stashed}
    return body


@router.post('/texts/{content_type}/translate-all')
async def translate_all(content_type: str,
                        state: AppState = Depends(require_project)):
    _check_ct(content_type)
    _service(state)
    if content_type == 'dialogue':
        await _check_dialogue_prerequisites(state)
        items = await state.db_call(state.db.get_untranslated_dialogues)
    else:
        items = await state.db_call(state.db.get_untranslated_ui_texts)
    if not items:
        return {'job_id': None, 'message': '没有待翻译的内容'}

    job = state.jobs.create(
        f'texts.translate-all.{content_type}',
        f'全部翻译（{"对话" if content_type == "dialogue" else "字符串"}）',
        {'content_type': content_type},
        _make_translate_job(state, content_type, items),
        exclusive=True)
    return {'job_id': job.id}


class TranslatePageIn(BaseModel):
    page: int = 0
    size: int = 50
    filter_mode: str = 'all'
    search: str = ''
    character: str = ''


@router.post('/texts/{content_type}/translate-page')
async def translate_page(content_type: str, req: TranslatePageIn,
                         state: AppState = Depends(require_project)):
    _check_ct(content_type)
    _service(state)
    if content_type == 'dialogue':
        await _check_dialogue_prerequisites(state)
        rows, _ = await state.db_call(
            state.db.get_dialogues_page,
            req.page, req.size, req.filter_mode, req.character, req.search)
    else:
        rows, _ = await state.db_call(
            state.db.get_ui_texts_page,
            req.page, req.size, req.filter_mode, req.search)
    items = [r for r in rows if not r.get('translated_text')]
    if not items:
        return {'job_id': None, 'message': '本页没有待翻译的内容'}

    job = state.jobs.create(
        f'texts.translate-page.{content_type}',
        f'翻译本页（{"对话" if content_type == "dialogue" else "字符串"}，{len(items)} 条）',
        {'content_type': content_type, 'page': req.page},
        _make_translate_job(state, content_type, items))
    return {'job_id': job.id}


# ---- 失败条目（批次内未译出的暂存） ----

async def _prune_failed_batches(state: AppState, content_type: str) -> list:
    """列出暂存批次并顺手自愈：已全部译出的删除、部分译出的回写剩余条目"""
    recs = await state.db_call(state.db.list_failed_batches, content_type)
    kept = []
    for rec in recs:
        remaining = await state.db_call(
            state.db.filter_untranslated_items, content_type, rec['items'])
        if not remaining:
            await state.db_call(state.db.delete_failed_batch, rec['id'])
        else:
            if len(remaining) < len(rec['items']):
                await state.db_call(
                    state.db.update_failed_batch_items, rec['id'], remaining)
            rec['items'] = remaining
            kept.append(rec)
    return kept


@router.get('/texts/{content_type}/failed-batches')
async def list_failed_items(content_type: str,
                            state: AppState = Depends(require_project)):
    """暂存的未译出条目（扁平列表；已译/已删的条目自动剔除）"""
    _check_ct(content_type)
    recs = await _prune_failed_batches(state, content_type)
    items = []
    for rec in recs:
        for it in rec['items']:
            items.append({
                'batch_id': rec['id'],
                'id': it['id'],
                'character': it.get('character', ''),
                'original_text': it.get('original_text', ''),
                'reason': it.get('reason', ''),
                'created_at': rec['created_at'],
            })
    return {'items': items, 'count': len(items), 'batch_count': len(recs)}


class RetryFailedIn(BaseModel):
    chunk_size: int = 10


@router.post('/texts/{content_type}/failed-batches/retry')
async def retry_failed_items(content_type: str, req: RetryFailedIn,
                             state: AppState = Depends(require_project)):
    """把全部暂存条目拆成小批重新翻译（任务）

    成功的条目直接写库；仍失败的回写暂存，全部译出的批次记录删除。
    """
    _check_ct(content_type)
    _service(state)
    chunk_size = max(1, min(req.chunk_size, 50))
    recs = await _prune_failed_batches(state, content_type)
    if not recs:
        return {'job_id': None, 'message': '没有待核验的失败条目'}

    async def body(job):
        service = _service(state)
        remaining_map = {rec['id']: rec['items'] for rec in recs}
        all_items = [it for rec in recs for it in rec['items']]
        chunks = [all_items[i:i + chunk_size]
                  for i in range(0, len(all_items), chunk_size)]
        job.emit_log(f'共 {len(all_items)} 条待核验，'
                     f'分 {len(chunks)} 批（每批 {chunk_size} 条）')
        failed_ids: set = set()
        done = 0
        for i, sub in enumerate(chunks):
            job.check_cancelled()
            results = await service.translate_batch(
                sub, content_type, stash_on_failure=False)
            ok_ids = set(results)
            failed_ids.update(it['id'] for it in sub if it['id'] not in ok_ids)
            done += len(sub)
            job.emit_progress(done / len(all_items),
                              f'已核验 {done}/{len(all_items)}'
                              f'（第 {i+1}/{len(chunks)} 批）')
        for batch_id, remaining in remaining_map.items():
            left = [it for it in remaining if it['id'] in failed_ids]
            if left:
                await state.db_call(
                    state.db.update_failed_batch_items, batch_id, left)
            else:
                await state.db_call(state.db.delete_failed_batch, batch_id)
        if failed_ids:
            job.emit_log(f'{len(failed_ids)} 条仍未译出，已保留在暂存中')
        else:
            job.emit_log('全部译出，暂存已清空')
        return {'retried': len(all_items), 'failed': len(failed_ids)}

    total = sum(len(rec['items']) for rec in recs)
    job = state.jobs.create(
        f'texts.failed-retry.{content_type}',
        f'失败条目重试（{"对话" if content_type == "dialogue" else "字符串"}，'
        f'{total} 条）',
        {'content_type': content_type},
        body, exclusive=True)
    return {'job_id': job.id}


@router.delete('/texts/{content_type}/failed-batches')
async def clear_failed_batches(content_type: str,
                               state: AppState = Depends(require_project)):
    """清空暂存（条目保持未翻译，可被下次全部翻译重新拾起）"""
    _check_ct(content_type)
    await state.db_call(state.db.clear_failed_batches, content_type)
    return {'ok': True}


# ---- UI 字符串：重建出处 ----

@router.post('/texts/ui/hints/rebuild')
async def rebuild_hints(state: AppState = Depends(require_project)):
    async def body(job):
        from renpy_parser import RenpyParser
        project_dir = state.project_manager.project_dir(state.current_project)
        game_root = project_dir / 'game'

        job.emit_log('正在回扫源码定位字符串出处...')
        hints = await state.run_sync(
            RenpyParser().locate_ui_string_contexts, str(game_root))
        matched = await state.db_call(state.db.update_ui_hints, hints)
        counts = await state.db_call(state.db.get_ui_text_count)
        job.emit_progress(1.0, f'完成: {matched}/{counts["total"]} 条命中')
        return {'matched': matched, 'total': counts['total']}

    job = state.jobs.create('ui.hints-rebuild', '重建字符串上下文', {}, body)
    return {'job_id': job.id}


# ---- 风格指南 ----

@router.get('/style-guide')
async def get_style_guide(state: AppState = Depends(require_project)):
    text = await state.db_call(state.db.get_meta, 'style_guide', '')
    return {'style_guide': text}


class StyleGuideIn(BaseModel):
    style_guide: str


@router.put('/style-guide')
async def put_style_guide(req: StyleGuideIn,
                          state: AppState = Depends(require_project)):
    await state.db_call(state.db.set_meta, 'style_guide', req.style_guide)
    return {'ok': True}


@router.post('/style-guide/generate')
async def generate_style_guide(state: AppState = Depends(require_project)):
    """AI 生成风格指南（同步；采样对话文本）"""
    if not state.translator:
        raise ApiError(409, 'NO_TRANSLATOR', '请先配置翻译器（模型配置）')

    # 采样走 db_call（executor + db_lock）。
    # db.sample_dialogue_texts(limit: int = 30000) -> str：
    #   每个 label 前 3 句 + 随机 50 句，拼成 "角色: 文本" 行，
    #   总长超 limit 字符截断；无对话时返回 ""
    sample = await state.db_call(state.db.sample_dialogue_texts, 30000)
    if not sample:
        raise ApiError(409, 'NO_DIALOGUES', '项目暂无对话文本，无法生成风格指南')
    guide = await state.run_sync(state.translator.generate_style_guide, sample)
    if not guide:
        raise ApiError(502, 'GENERATE_FAILED', '风格指南生成失败（空结果）')
    return {'style_guide': guide}
