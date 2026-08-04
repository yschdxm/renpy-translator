"""游戏导出 API：统计信息 + 导出任务"""
from fastapi import APIRouter, Depends

from ..deps import require_project
from ..errors import ApiError
from ..state import AppState

router = APIRouter(prefix='/current/export', tags=['export'])


@router.get('/info')
async def export_info(state: AppState = Depends(require_project)):
    d = await state.db_call(state.db.get_dialogue_count)
    u = await state.db_call(state.db.get_ui_text_count)
    n = await state.db_call(state.db.get_char_dict_count)
    total = d['total'] + u['total'] + n['total']
    translated = d['translated'] + u['translated'] + n['translated']
    project_dir = state.project_manager._get_project_dir(state.current_project)
    return {
        'dialogue': d, 'ui': u, 'names': n,
        'total': total, 'translated': translated,
        'percent': round(translated / total * 100, 1) if total else 0,
        'output_dir': str(project_dir / 'output'),
    }


@router.post('/game')
async def export_game(state: AppState = Depends(require_project)):
    d = await state.db_call(state.db.get_dialogue_count)
    if d['translated'] == 0:
        raise ApiError(409, 'NOTHING_TO_EXPORT', '没有已翻译的内容可导出')

    async def body(job):
        from services.game_export import GameExporter
        exporter = GameExporter(state.project_manager, state.db, state.logger)
        loop = __import__('asyncio').get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: exporter.export(
                state.current_project,
                log=lambda msg: job.emit_log(msg),
                progress=lambda v, t: job.emit_progress(v, t),
            ))
        if not result['success']:
            raise RuntimeError(result['message'])
        job.emit_progress(1.0, '导出完成')
        return result

    job = state.jobs.create('export.game', f'导出游戏（{state.current_project}）',
                            {}, body)
    return {'job_id': job.id}
