"""任务 API：查询/回答/取消/SSE 事件流（db 轮询回放，after_seq 游标）"""
import asyncio
import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..deps import get_state
from ..errors import ApiError
from ..jobs import TERMINAL_STATUSES
from ..state import AppState

router = APIRouter(prefix='/jobs', tags=['jobs'])


class AnswerRequest(BaseModel):
    question_id: str
    answer: dict


@router.get('')
async def list_jobs(active: bool = False, limit: int = 50,
                    state: AppState = Depends(get_state)):
    return state.app_db.list_jobs(active_only=active, limit=limit)


@router.get('/{job_id}')
async def get_job(job_id: str, state: AppState = Depends(get_state)):
    job = state.app_db.get_job(job_id)
    if not job:
        raise ApiError(404, 'JOB_NOT_FOUND', f'任务不存在: {job_id}')
    return job


@router.post('/{job_id}/answer')
async def answer_job(job_id: str, req: AnswerRequest,
                     state: AppState = Depends(get_state)):
    if not state.jobs.answer(job_id, req.question_id, req.answer):
        raise ApiError(409, 'STALE_QUESTION',
                       '问题已过期或任务不在等待输入（请刷新任务状态）')
    return {'ok': True}


@router.post('/{job_id}/cancel')
async def cancel_job(job_id: str, state: AppState = Depends(get_state)):
    if not state.jobs.cancel(job_id):
        raise ApiError(409, 'NOT_CANCELLABLE', '任务不在运行中（或已结束）')
    return {'ok': True}


@router.get('/{job_id}/events')
async def job_events(job_id: str, after_seq: int = 0,
                     state: AppState = Depends(get_state)):
    """SSE 事件流：db 是唯一事实源，按 after_seq 轮询增量拉取。

    终态 status 事件送达后关流；任务记录已终态但终态事件缺失
    （收尾写库失败等）时发兜底 status 关流。"""
    record = state.app_db.get_job(job_id)
    if not record:
        raise ApiError(404, 'JOB_NOT_FOUND', f'任务不存在: {job_id}')

    def _fmt(ev: dict) -> dict:
        return {'event': ev['kind'], 'data': json.dumps(
            {'seq': ev['seq'], **ev['data']}, ensure_ascii=False)}

    def _is_terminal_status(ev: dict) -> bool:
        return (ev['kind'] == 'status'
                and ev['data'].get('status') in TERMINAL_STATUSES)

    async def gen():
        last = after_seq
        while True:
            events = await state.run_sync(
                state.app_db.get_events, job_id, last)
            terminal_sent = False
            for ev in events:
                last = ev['seq']
                yield _fmt(ev)
                if _is_terminal_status(ev):
                    # 终态 status 是任务的最后一个事件（收尾时最后落库）
                    terminal_sent = True
            if terminal_sent:
                return
            if events:
                continue  # 事件还在写入，立刻再拉
            rec = await state.run_sync(state.app_db.get_job, job_id)
            if not (rec and rec['status'] in TERMINAL_STATUSES):
                await asyncio.sleep(0.3)
                continue
            # 记录已终态但终态事件未出现：收尾是「先 update_job 后落事件」，
            # 撞微窗口时多确认两轮（仍无新事件说明事件写库失败）再兜底关流
            quiet = True
            for _ in range(2):
                await asyncio.sleep(0.15)
                more = await state.run_sync(
                    state.app_db.get_events, job_id, last)
                if more:
                    quiet = False
                    break
            if not quiet:
                continue
            yield {'event': 'status', 'data': json.dumps(
                {'seq': last + 1, 'status': rec['status']},
                ensure_ascii=False)}
            return

    return EventSourceResponse(gen(), ping=15)
