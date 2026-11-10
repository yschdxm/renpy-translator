"""内嵌文本提取 API：扫描任务（含复核 ask/重判循环）、单句精判、源码查看"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import require_project
from ..errors import ApiError
from ..jobs import JobCancelled
from ..state import AppState

router = APIRouter(prefix='/current/embedded', tags=['embedded'])


class EmbeddedScanRequest(BaseModel):
    # 跳过 AI 预筛：只做零 token 规则分流，未决项交人工复核
    # （无模型也可用；复核界面可再按需精判）
    skip_ai_prescreen: bool = False


def _pipeline(state: AppState):
    from services.embedded_pipeline import EmbeddedPipeline
    project_dir = state.project_manager.project_dir(state.current_project)
    # 引擎目录在项目根的 game/ 子目录下，SDK 大版本选择必须基于它
    sdk_path = state.resolve_sdk_path(project_dir / 'game')
    return EmbeddedPipeline(state.db, state.translator, str(project_dir),
                            sdk_path, state.logger)


def _row_payload(r: dict) -> dict:
    from usage_rules import RULE_REASON_PREFIX
    c = r['candidate']
    reason = r['ai_reason'] or ''
    danger = 1 if r.get('ai_danger') else 0
    # 旧库兼容：危险警示曾以 ⚠ 前缀拼在理由里，拆回独立标记
    if reason.startswith('⚠'):
        danger = 1
        reason = reason.split('；', 1)[1] if '；' in reason else ''
    return {
        'id': r['id'], 'text': c.text, 'kind': c.kind, 'hint': c.hint,
        'file': c.rel_file, 'line': c.line, 'confidence': c.confidence,
        'raw': c.raw,
        'ai_keep': r['ai_keep'], 'ai_reason': reason, 'ai_danger': danger,
        'ai_evidence': r.get('ai_evidence', ''),
        'apply_path': r.get('apply_path', ''),
        # 判定来源：rule=静态规则确定，ai=模型判定，pending=未判定
        # （跳过预筛/未决行不算 AI 判定，前端筛选不混入）
        'source': ('rule' if reason.startswith(RULE_REASON_PREFIX)
                   else 'pending' if r['ai_keep'] == -1 else 'ai'),
        'status': r.get('status', 'pending'),
    }


async def start_embedded_scan(state, skip_ai_prescreen: bool = False):
    """内嵌文本提取全管线任务（state 可为 AppState 或项目运行时）：
    扫描→AI预筛(可跳过)→ask 复核(重判循环)→标记→SDK→入库"""
    if not skip_ai_prescreen and not state.translator:
        raise ApiError(409, 'NO_TRANSLATOR', 'AI 预筛需要翻译器，请先配置模型'
                       '（或勾选「跳过 AI 预筛」零 token 纯扫描）')
    pipe = _pipeline(state)

    async def body(job):
        job.emit_stage('正在扫描源码中的内嵌文本...')
        rows = await pipe.scan_and_merge()
        if not rows:
            return {'message': '未发现可提取的内嵌文本（或全部已处理）', 'wrapped': 0}
        job.emit_log(f'扫描到 {len(rows)} 条候选')

        # 筛查/精判/拆除阶段取消时抛 ScreeningCancelled（cancel_event 已置位），
        # 由 registry 统一归一化为 cancelled
        while True:
            job.check_cancelled()
            # AI 预筛（只判未决；失败即任务失败，不降级）；
            # skip 模式只做零 token 规则分流，unknown 保持未决交人工
            prescreen_label = ('正在规则分流（零 token）' if skip_ai_prescreen
                               else '正在 AI 预筛')
            await pipe.screen_undecided(
                rows,
                on_progress=lambda phase, done, total: job.emit_progress(
                    done / max(total, 1) * 0.9,
                    f'{prescreen_label}（{phase}）... ({done}/{total})'),
                cancel_event=job.cancel_event,
                skip_ai_prescreen=skip_ai_prescreen)

            answer = await job.ask('embedded_review', {
                'rows': [_row_payload(r) for r in rows],
                'ai_prescreen_run': not skip_ai_prescreen,
            })
            action = answer.get('action')

            # 重判/精判都基于前端筛出的行（row_ids），未传则作用于全部
            def _target_rows():
                ids = set(answer.get('row_ids') or [])
                return [r for r in rows if r['id'] in ids] if ids else rows

            if action in ('recheck', 'refine_all') and not state.translator:
                raise ApiError(409, 'NO_TRANSLATOR',
                               'AI 复核/精判需要翻译器，请先配置模型')
            if action == 'recheck':
                target = _target_rows()
                job.emit_log(f'灰区复核 {len(target)} 条（锚定上轮判定，'
                             '仅纠错不重猜）')
                await pipe.recheck_grayzone(
                    target,
                    on_progress=lambda phase, done, total: job.emit_progress(
                        done / max(total, 1) * 0.9,
                        f'正在灰区复核（{phase}）... ({done}/{total})'),
                    cancel_event=job.cancel_event)
                continue
            if action == 'refine_all':
                target = _target_rows()
                job.emit_log(f'精判 {len(target)} 条（agentic 精审）')
                await pipe.refine_rows(
                    target,
                    on_progress=lambda phase, done, total: job.emit_progress(
                        done / max(total, 1) * 0.9,
                        f'正在 AI 精判（{phase}）... ({done}/{total})'),
                    cancel_event=job.cancel_event)
                continue
            if action == 'cancel':
                raise JobCancelled()

            chosen_ids = set(answer.get('chosen_ids') or [])
            chosen = [r for r in rows if r['id'] in chosen_ids]
            result = await pipe.apply_selection(
                rows, chosen,
                stage=lambda text: job.emit_stage(text),
                cancel_event=job.cancel_event)
            job.emit_progress(1.0, '完成')
            return result

    job = state.jobs.create('embedded.scan', '提取内嵌文本', {}, body,
                            exclusive=True)
    return {'job_id': job.id}


@router.post('/scan')
async def scan(req: EmbeddedScanRequest | None = None,
               state: AppState = Depends(require_project)):
    return await start_embedded_scan(
        state, skip_ai_prescreen=bool(req and req.skip_ai_prescreen))


@router.post('/refine/{row_id}')
async def refine(row_id: int, state: AppState = Depends(require_project)):
    """单句 AI 精判（复核对话框等待期间也可调用；写库并返回判定）"""
    if not state.translator:
        raise ApiError(409, 'NO_TRANSLATOR', '请先配置翻译器（模型配置）')
    pipe = _pipeline(state)
    try:
        keep, reason, danger, evidence, apply_path = await pipe.refine_by_id(
            row_id)
    except KeyError as e:
        raise ApiError(404, 'NOT_FOUND', str(e))
    return {'ai_keep': 1 if keep else 0, 'ai_reason': reason,
            'ai_danger': 1 if danger else 0, 'ai_evidence': evidence,
            'apply_path': apply_path}


@router.get('/snippet')
async def snippet(file: str, line: int, ctx: int = 6,
                  state: AppState = Depends(require_project)):
    """候选处源码（前后 ctx 行）；file 为相对 game/game/ 的路径（防目录穿越）"""
    project_dir = state.project_manager.project_dir(state.current_project)
    # 与 find_candidates 的 rel_file 基准一致：game/ 子目录存在时以它为根
    base = project_dir / 'game'
    if (base / 'game').is_dir():
        base = base / 'game'
    base = base.resolve()
    target = (base / file).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise ApiError(404, 'NOT_FOUND', f'文件不存在: {file}')

    def _read():
        lines = target.read_text(encoding='utf-8', errors='ignore').split('\n')
        start = max(1, line - ctx)
        end = min(len(lines), line + ctx)
        return [
            {'no': i, 'text': lines[i - 1], 'is_target': i == line}
            for i in range(start, end + 1)
        ]

    return {'file': file, 'line': line, 'lines': await state.run_sync(_read)}
