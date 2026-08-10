"""人名 API：列表/改名/单条翻译+分析/批量任务/画像"""
import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import require_project
from ..errors import ApiError
from ..state import AppState

router = APIRouter(prefix='/current/names', tags=['names'])


def _name_service(state: AppState, max_context_k: int = 8):
    """人名翻译服务单例缓存（hooks 由调用方接）

    服务内部持有 ThreadPoolExecutor，每次新建而不 shutdown 会泄漏线程，
    因此按 (db, translator, translation_service, max_context_k) 缓存复用；
    项目切换/模型配置变化导致任一依赖变更时，关闭旧实例线程池后重建。
    """
    if not state.translation_service or not state.translator:
        raise ApiError(409, 'NO_TRANSLATOR', '请先配置翻译器（模型配置）')
    from services.name_translation import NameTranslationService
    key = (id(state.db), id(state.translator),
           id(state.translation_service), max_context_k)
    cached = getattr(state, '_name_service_cache', None)
    if cached and cached[0] == key:
        return cached[1]
    if cached:
        cached[1].close()  # 释放旧实例的线程池
    service = NameTranslationService(
        db=state.db, translator=state.translator,
        translation_service=state.translation_service,
        logger=state.logger, max_context_k=max_context_k)
    state._name_service_cache = (key, service)
    return service


async def _max_context_k(state: AppState) -> int:
    provider = state.translation_service.config_provider \
        if state.translation_service else None
    if not provider:
        return 8
    cfg = await state.db_call(provider)
    return cfg[0] if cfg else 8


@router.get('')
async def list_names(state: AppState = Depends(require_project)):
    """全部角色（人名数量级小，前端本地分页）"""
    characters = await state.db_call(state.db.get_characters)
    rows = []
    for c in characters:
        if c['is_placeholder']:
            continue
        cn = c['cn_name'] or ''
        rows.append({
            'variable': c['variable'] or '',
            'original': c['display_name'],
            'translated': cn,
            'lines': c['lines_count'],
            'name_done': bool(cn.strip()),
            'analyzed': bool(c['profile_json']),
        })
    counts = await state.db_call(state.db.get_char_dict_count)
    profiles = await state.db_call(state.db.get_all_profiles)
    return {'rows': rows, 'total': counts['total'],
            'translated': counts['translated'], 'analyzed': len(profiles)}


class UpdateNameIn(BaseModel):
    cn_name: str
    variable: str = ''


@router.patch('/{display_name}')
async def update_name(display_name: str, req: UpdateNameIn,
                      state: AppState = Depends(require_project)):
    await state.db_call(
        state.db.update_character_cn_name, display_name, req.cn_name,
        variable=req.variable or None)
    state.logger.info(f'人名已保存: {display_name} -> {req.cn_name}', panel='names')
    return {'ok': True}


class TranslateOneIn(BaseModel):
    variable: str = ''


@router.post('/{display_name}/translate')
async def translate_one(display_name: str, req: TranslateOneIn,
                        state: AppState = Depends(require_project)):
    """单条翻译+分析（同步；分段台词时可能较久）"""
    service = _name_service(state, await _max_context_k(state))
    # 单例复用需清掉上次批量任务的残留状态：
    # 取消标记（否则分段循环直接 break）与指向已结束 job 的 hooks
    service._cancel = False
    service.on_progress = None
    service.on_row_busy = None
    service.on_row_done = None
    await service.translate_and_analyze(
        display_name, variable=req.variable or None)
    c = await state.db_call(state.db.get_character_by_name, display_name)
    profile = await state.db_call(state.db.get_profile, display_name)
    return {'cn_name': (c['cn_name'] if c else '') or '', 'profile': profile}


@router.post('/translate-all')
async def translate_all(state: AppState = Depends(require_project)):
    service = _name_service(state, await _max_context_k(state))

    async def body(job):
        service.on_progress = lambda i, total, text: job.emit_progress(
            i / max(total, 1), text)
        service.on_row_busy = lambda n: job.emit_log(f'开始: {n}')
        # 取消传播：轮询 job.cancel_event → service.stop()（服务内部
        # 分批循环会检查 _cancel；批量任务体内不能随意 raise，会被吞）
        task = asyncio.create_task(service.translate_all())
        while not task.done():
            if job.cancel_event.is_set():
                service.stop()
            await asyncio.sleep(0.3)
        result = await task
        job.check_cancelled()
        job.emit_progress(1.0, '完成')
        return result

    job = state.jobs.create('names.translate-all', '全部翻译+分析（人名）', {}, body)
    return {'job_id': job.id}


@router.get('/{display_name}/profile')
async def get_profile(display_name: str, variable: str = '',
                      state: AppState = Depends(require_project)):
    profile = await state.db_call(state.db.get_profile, display_name)
    if not profile:
        raise ApiError(404, 'NO_PROFILE', '该角色尚未分析')
    return {'profile': profile}
