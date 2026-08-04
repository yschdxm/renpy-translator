"""Job / JobRegistry：任务生命周期、事件发射、ask/answer、取消"""
import asyncio
import threading
import time
import traceback
import uuid
from typing import Any, Awaitable, Callable

from ..appdb import AppDatabase
from .events import EventBus

TERMINAL_STATUSES = ('succeeded', 'failed', 'cancelled', 'interrupted')


class JobCancelled(Exception):
    """任务被取消（ask 等待中或检查点触发）"""


class Job:
    """运行中的任务句柄（仅内存；状态/事件持久化在 app.db）"""

    def __init__(self, registry: 'JobRegistry', job_id: str, kind: str, label: str):
        self.registry = registry
        self.id = job_id
        self.kind = kind
        self.label = label
        self.cancel_event = threading.Event()
        self.done = False  # 任务体已结束（成功/失败/取消）
        self._last_progress = (0.0, '', 0.0)  # 进度节流状态
        self._loop = asyncio.get_running_loop()
        self._answer_future: asyncio.Future | None = None
        self._question_id: str = ''

    # ---- 事件发射（线程安全；db 先写，bus 后扇出） ----

    def _emit(self, kind: str, data: dict):
        db = self.registry.app_db
        seq = db.add_event(self.id, kind, data)
        event = {'seq': seq, 'kind': kind, 'data': data}
        self._loop.call_soon_threadsafe(
            self.registry.bus.publish, self.id, event)

    def emit_log(self, text: str):
        self._emit('log', {'text': text})

    def emit_progress(self, value: float, text: str = ''):
        # 节流：距上次 <0.3s 时，只有换新文本或进度跨过 0.5% 才发射
        # （逐文件复制/解压每文件一次回调，不节流会刷爆事件表）
        now = time.monotonic()
        last_v, last_t, last_time = self._last_progress
        if (now - last_time < 0.3
                and (text == last_t or abs(value - last_v) < 0.005)):
            return
        self._last_progress = (value, text, now)
        self.registry.app_db.update_job(self.id, progress=round(value, 4))
        self._emit('progress', {'value': round(value, 4), 'text': text})

    def emit_stage(self, stage: str):
        self.registry.app_db.update_job(self.id, stage=stage)
        self._emit('stage', {'stage': stage})

    # ---- 取消 ----

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise JobCancelled()

    def cancel(self):
        self.cancel_event.set()
        fut = self._answer_future
        if fut is not None and not fut.done():
            self._loop.call_soon_threadsafe(
                lambda: fut.cancelled() or fut.set_exception(JobCancelled()))

    # ---- ask/answer ----

    async def ask(self, qtype: str, payload: dict) -> Any:
        """挂起任务等待用户输入。取消 → JobCancelled"""
        self.check_cancelled()
        qid = uuid.uuid4().hex[:8]
        self._question_id = qid
        question = {'question_id': qid, 'type': qtype, 'payload': payload}
        db = self.registry.app_db
        db.update_job(self.id, status='waiting_input', question=question)
        self._emit('question', question)

        fut = self._loop.create_future()
        self._answer_future = fut
        try:
            answer = await fut
        finally:
            self._answer_future = None
            self._question_id = ''
            db.update_job(self.id, status='running', question=None)
            self._emit('answered', {'question_id': qid})
        self.check_cancelled()
        return answer

    def try_answer(self, question_id: str, answer: Any) -> bool:
        """False = question_id 过期或未在等待（调用方返回 409）"""
        fut = self._answer_future
        if fut is None or question_id != self._question_id or fut.done():
            return False
        fut.set_result(answer)
        return True


class JobRegistry:
    """任务注册表：内存中的活跃任务 + db 持久化"""

    def __init__(self, app_db: AppDatabase, bus: EventBus):
        self.app_db = app_db
        self.bus = bus
        self._jobs: dict[str, Job] = {}

    def create(self, kind: str, label: str, payload: dict,
               body: Callable[[Job], Awaitable[Any]]) -> Job:
        """创建并启动任务。body(job) 为协程，返回值写入 result。"""
        job_id = self.app_db.create_job(kind, label, payload)
        job = Job(self, job_id, kind, label)
        self._jobs[job_id] = job

        async def _run():
            db = self.app_db
            try:
                result = await body(job)
                if job.cancel_event.is_set():
                    db.update_job(job_id, status='cancelled')
                    job._emit('status', {'status': 'cancelled'})
                else:
                    db.update_job(job_id, status='succeeded', result=result or {},
                                  progress=1.0)
                    job._emit('status', {'status': 'succeeded',
                                         'result': result or {}})
            except JobCancelled:
                db.update_job(job_id, status='cancelled')
                job._emit('status', {'status': 'cancelled'})
            except Exception as e:
                err = f'{e}\n\n{traceback.format_exc()}'
                db.update_job(job_id, status='failed', error=err)
                job._emit('status', {'status': 'failed', 'error': str(e),
                                     'detail': traceback.format_exc()})
            finally:
                job.done = True
                # 句柄保留在内存中直到终态事件送达（前端可能立刻 answer/cancel 已结束的任务）
                self._loop_gc(job_id)

        asyncio.create_task(_run())
        return job

    def _loop_gc(self, job_id: str):
        """终态任务句柄延迟回收（10 分钟，够前端重连取终态）"""
        async def _gc():
            await asyncio.sleep(600)
            self._jobs.pop(job_id, None)
        asyncio.get_running_loop().create_task(_gc())

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def answer(self, job_id: str, question_id: str, answer: Any) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        return job.try_answer(question_id, answer)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.done:
            return False
        job.cancel()
        return True
