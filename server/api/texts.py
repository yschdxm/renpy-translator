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
    """批翻译任务体：逐批翻译，批间响应取消"""
    async def body(job):
        service = _service(state)
        batches = await service.prepare_batches(items, content_type)
        total_items = len(items)
        done = 0
        job.emit_log(f'共 {total_items} 条，分 {len(batches)} 批')
        for i, batch in enumerate(batches):
            job.check_cancelled()
            await service.translate_batch(batch, content_type)
            done += len(batch)
            job.emit_progress(done / total_items,
                              f'已翻译 {done}/{total_items}（第 {i+1}/{len(batches)} 批）')
        return {'translated': done, 'total': total_items}
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
