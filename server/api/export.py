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

    sdk_path = state.app_db.get_setting('sdk_path', '')
    if not sdk_path:
        for c in state.config_manager.load_all_configs():
            if c.sdk_path:
                sdk_path = c.sdk_path
                break
    if not sdk_path:
        # 不降级：tl 模板生成本就依赖 SDK，无 SDK 的导出没有意义
        raise ApiError(409, 'NO_SDK',
                       "导出需要 Ren'Py SDK（模板生成与编译校验），请先在模型配置中设置 SDK 路径")
    project_dir = state.project_manager._get_project_dir(state.current_project)

    async def body(job):
        import asyncio
        from services.game_export import GameExporter
        loop = asyncio.get_event_loop()
        exporter = GameExporter(state.project_manager, state.db, state.logger)

        def _export():
            return exporter.export(
                state.current_project,
                log=lambda msg: job.emit_log(msg),
                progress=lambda v, t: job.emit_progress(v, t),
            )

        result = await loop.run_in_executor(None, _export)
        if not result['success']:
            raise RuntimeError(result['message'])

        # 编译校验 + 自愈（译文修复需模型；内嵌拆除不需要）
        from services.export_healer import ExportHealer
        healer = ExportHealer(state.db, state.translator, str(project_dir),
                              sdk_path, state.logger, exporter)
        for attempt in range(1, 3):
            status = await healer.validate_and_heal(
                project_dir / 'output', log=job.emit_log)
            if status == 'ok':
                job.emit_progress(1.0, '导出完成（编译校验通过）')
                return result
            if status == 'reexport' and attempt < 2:
                job.emit_log('内嵌标记已拆除，重新导出...')
                result = await loop.run_in_executor(None, _export)
                if not result['success']:
                    raise RuntimeError(result['message'])
                continue
            raise RuntimeError(
                '导出后编译校验未通过，自动修复未能解决（详见日志）')
        raise RuntimeError('导出后编译校验未通过')

    job = state.jobs.create('export.game', f'导出游戏（{state.current_project}）',
                            {}, body)
    return {'job_id': job.id}
