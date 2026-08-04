"""任务 API：查询/回答/取消/SSE 事件流（after_seq 从 db 回放）"""
import asyncio
import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..deps import get_state
from ..errors import ApiError
from ..state import AppState

router = APIRouter(prefix='/jobs', tags=['jobs'])

TERMINAL = ('succeeded', 'failed', 'cancelled', 'interrupted')


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
    """SSE 事件流：先订阅（防漏），再从 db 回放 after_seq 之后的事件，
    然后转直播（seq 去重）；终态 status 事件后关流。"""
    record = state.app_db.get_job(job_id)
    if not record:
        raise ApiError(404, 'JOB_NOT_FOUND', f'任务不存在: {job_id}')

    queue = state.bus.subscribe(job_id)

    async def gen():
        last = after_seq
        try:
            # 回放（db 是唯一事实源；回放期间的新事件进了 queue，靠 seq 去重）
            for ev in state.app_db.get_events(job_id, after_seq):
                last = ev['seq']
                yield {'event': ev['kind'], 'data': json.dumps(
                    {'seq': ev['seq'], **ev['data']}, ensure_ascii=False)}
            # 任务已终结且无新事件 → 发一个终态 status 兜底后关流
            if record['status'] in TERMINAL:
                yield {'event': 'status', 'data': json.dumps(
                    {'seq': last + 1, 'status': record['status']}, ensure_ascii=False)}
                return
            # 直播
            while True:
                event = await queue.get()
                if event['seq'] <= last:
                    continue
                last = event['seq']
                yield {'event': event['kind'], 'data': json.dumps(
                    {'seq': event['seq'], **event['data']}, ensure_ascii=False)}
                if event['kind'] == 'status':
                    return
        finally:
            state.bus.unsubscribe(job_id, queue)

    return EventSourceResponse(gen(), ping=15)
