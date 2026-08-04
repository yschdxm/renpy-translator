"""内嵌文本提取 API：扫描任务（含复核 ask/重判循环）、单句精判、源码查看"""
import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends

from ..deps import require_project
from ..errors import ApiError
from ..jobs import JobCancelled
from ..state import AppState

router = APIRouter(prefix='/current/embedded', tags=['embedded'])


def _pipeline(state: AppState):
    from services.embedded_pipeline import EmbeddedPipeline
    sdk_path = state.app_db.get_setting('sdk_path', '')
    if not sdk_path:
        # 回退：旧版存在模型配置里
        for c in state.config_manager.load_all_configs():
            if c.sdk_path:
                sdk_path = c.sdk_path
                break
    project_dir = state.project_manager._get_project_dir(state.current_project)
    return EmbeddedPipeline(state.db, state.translator, str(project_dir),
                            sdk_path, state.logger)


def _row_payload(r: dict) -> dict:
    c = r['candidate']
    return {
        'id': r['id'], 'text': c.text, 'kind': c.kind, 'hint': c.hint,
        'file': c.rel_file, 'line': c.line, 'confidence': c.confidence,
        'raw': c.raw,
        'ai_keep': r['ai_keep'], 'ai_reason': r['ai_reason'],
        'status': r.get('status', 'pending'),
    }


@router.post('/scan')
async def scan(state: AppState = Depends(require_project)):
    """内嵌文本提取全管线任务：扫描→AI预筛→ask 复核(重判循环)→标记→SDK→入库"""
    if not state.translator:
        raise ApiError(409, 'NO_TRANSLATOR', 'AI 预筛需要翻译器，请先配置模型')
    pipe = _pipeline(state)

    async def body(job):
        job.emit_stage('正在扫描源码中的内嵌文本...')
        rows = await pipe.scan_and_merge()
        if not rows:
            return {'message': '未发现可提取的内嵌文本（或全部已处理）', 'wrapped': 0}
        job.emit_log(f'扫描到 {len(rows)} 条候选')

        while True:
            job.check_cancelled()
            # AI 预筛（只判未决；失败即任务失败，不降级）
            await pipe.screen_undecided(
                rows,
                on_progress=lambda phase, done, total: job.emit_progress(
                    done / max(total, 1) * 0.9,
                    f'AI 预筛（{phase}）: {done}/{total}'))

            answer = await job.ask('embedded_review', {
                'rows': [_row_payload(r) for r in rows],
            })
            action = answer.get('action')

            if action == 'rescreen':
                job.emit_log('全部重判：清空判定，重新 AI 预筛')
                await pipe.rescreen_all(
                    rows,
                    on_progress=lambda phase, done, total: job.emit_progress(
                        done / max(total, 1) * 0.9,
                        f'AI 重判（{phase}）: {done}/{total}'))
                continue
            if action == 'cancel':
                raise JobCancelled()

            chosen_ids = set(answer.get('chosen_ids') or [])
            chosen = [r for r in rows if r['id'] in chosen_ids]
            result = await pipe.apply_selection(
                rows, chosen,
                stage=lambda text: job.emit_stage(text))
            job.emit_progress(1.0, '完成')
            return result

    job = state.jobs.create('embedded.scan', '提取内嵌文本', {}, body)
    return {'job_id': job.id}


@router.post('/refine/{row_id}')
async def refine(row_id: int, state: AppState = Depends(require_project)):
    """单句 AI 精判（复核对话框等待期间也可调用；写库并返回判定）"""
    if not state.translator:
        raise ApiError(409, 'NO_TRANSLATOR', '请先配置翻译器（模型配置）')
    pipe = _pipeline(state)
    try:
        keep, reason = await pipe.refine_by_id(row_id)
    except KeyError as e:
        raise ApiError(404, 'NOT_FOUND', str(e))
    return {'ai_keep': 1 if keep else 0, 'ai_reason': reason}


@router.get('/snippet')
async def snippet(file: str, line: int, ctx: int = 6,
                  state: AppState = Depends(require_project)):
    """候选处源码（前后 ctx 行）；file 为相对 game/game/ 的路径（防目录穿越）"""
    project_dir = state.project_manager._get_project_dir(state.current_project)
    # 与 find_candidates 的 rel_file 基准一致：game/ 子目录存在时以它为根
    base = project_dir / 'game'
    if (base / 'game').is_dir():
        base = base / 'game'
    base = base.resolve()
    target = (base / file).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise ApiError(404, 'NOT_FOUND', f'文件不存在: {file}')

    loop = asyncio.get_event_loop()

    def _read():
        lines = target.read_text(encoding='utf-8', errors='ignore').split('\n')
        start = max(1, line - ctx)
        end = min(len(lines), line + ctx)
        return [
            {'no': i, 'text': lines[i - 1], 'is_target': i == line}
            for i in range(start, end + 1)
        ]

    return {'file': file, 'line': line, 'lines': await loop.run_in_executor(None, _read)}
