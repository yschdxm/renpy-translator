"""游戏导出 API：统计信息 + 导出任务"""
from fastapi import APIRouter, Depends

from ..deps import require_project
from ..errors import ApiError
from ..state import AppState

router = APIRouter(prefix='/current/export', tags=['export'])


@router.get('/info')
async def export_info(state: AppState = Depends(require_project)):
    from .projects import _exports_dir
    d = await state.db_call(state.db.get_dialogue_count)
    u = await state.db_call(state.db.get_ui_text_count)
    n = await state.db_call(state.db.get_char_dict_count)
    total = d['total'] + u['total'] + n['total']
    translated = d['translated'] + u['translated'] + n['translated']
    return {
        'dialogue': d, 'ui': u, 'names': n,
        'total': total, 'translated': translated,
        'percent': round(translated / total * 100, 1) if total else 0,
        # 导出产物只有 exports/{项目名}/{项目名}-translated.zip
        'exports_dir': str(_exports_dir(state, state.current_project)),
    }


@router.post('/game')
async def export_game(state: AppState = Depends(require_project)):
    d = await state.db_call(state.db.get_dialogue_count)
    if d['translated'] == 0:
        raise ApiError(409, 'NOTHING_TO_EXPORT', '没有已翻译的内容可导出')

    project_dir = state.project_manager._get_project_dir(state.current_project)
    sdk_path = state.resolve_sdk_path(project_dir)
    if not sdk_path:
        from sdk_manager import detect_engine_version
        gv = detect_engine_version(project_dir)
        hint = (f"（游戏引擎为 Ren'Py {'.'.join(map(str, gv))}，"
                f'需要 {gv[0]}.x 的 SDK）' if gv else '')
        # 不降级：tl 模板生成本就依赖 SDK，无 SDK 的导出没有意义
        raise ApiError(409, 'NO_SDK',
                       "导出需要 Ren'Py SDK（模板生成与编译校验）" + hint +
                       '，请在模型配置页下载对应版本的 SDK')

    async def body(job):
        import asyncio
        import tempfile
        from pathlib import Path
        from services.game_export import (
            ExportCancelled, GameExporter, zip_directory)
        from .projects import _exports_dir
        from ..jobs import JobCancelled
        loop = asyncio.get_event_loop()
        exporter = GameExporter(state.project_manager, state.db, state.logger)

        # 导出组装在临时目录完成（与导入/导出项目包一致），
        # 不再在项目下留 output/ 目录；最终产物只有 zip 包
        zip_path = None
        try:
            with tempfile.TemporaryDirectory(prefix='export-') as tmp:
                work_dir = Path(tmp) / 'out'

                def _export():
                    try:
                        return exporter.export(
                            state.current_project,
                            log=lambda msg: job.emit_log(msg),
                            progress=lambda v, t: job.emit_progress(v, t),
                            export_dir=work_dir,
                            cancel_event=job.cancel_event,
                        )
                    except ExportCancelled:
                        raise JobCancelled()

                result = await loop.run_in_executor(None, _export)
                job.check_cancelled()
                if not result['success']:
                    raise RuntimeError(result['message'])

                # 编译校验 + 自愈（译文修复需模型；内嵌拆除不需要）
                from services.export_healer import ExportHealer
                healer = ExportHealer(state.db, state.translator, str(project_dir),
                                      sdk_path, state.logger, exporter)
                for attempt in range(1, 3):
                    job.check_cancelled()
                    status = await healer.validate_and_heal(
                        work_dir, log=job.emit_log,
                        cancel_check=job.check_cancelled)
                    if status == 'ok':
                        break
                    if status == 'reexport' and attempt < 2:
                        job.emit_log('内嵌标记已拆除，重新导出...')
                        result = await loop.run_in_executor(None, _export)
                        job.check_cancelled()
                        if not result['success']:
                            raise RuntimeError(result['message'])
                        continue
                    raise RuntimeError(
                        '导出后编译校验未通过，自动修复未能解决（详见日志）')

                # 打包为 {项目名}-translated.zip（exports/{项目名}/ 下，与项目包同目录）
                exports_dir = _exports_dir(state, state.current_project)
                zip_path = exports_dir / f'{state.current_project}-translated.zip'
                job.emit_log(f'打包导出结果: {zip_path.name}')

                def _zip():
                    try:
                        zip_directory(
                            work_dir, zip_path,
                            progress=lambda v, t: job.emit_progress(
                                0.95 + v * 0.05, t),
                            cancel_event=job.cancel_event)
                    except ExportCancelled:
                        raise JobCancelled()

                await loop.run_in_executor(None, _zip)
                job.emit_log(f'导出包: {zip_path}')
                result['package'] = str(zip_path)
                result['package_name'] = zip_path.name
                result['message'] = f'导出包: {zip_path}'
                job.emit_progress(1.0, '导出完成（编译校验通过）')
                return result
        except JobCancelled:
            # 删除半成品 zip，避免被当成完整导出包下载
            if zip_path is not None:
                zip_path.unlink(missing_ok=True)
            raise

    job = state.jobs.create('export.game', f'导出游戏（{state.current_project}）',
                            {}, body)
    return {'job_id': job.id}
