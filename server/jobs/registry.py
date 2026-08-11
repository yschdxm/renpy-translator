"""Job / JobRegistry：任务生命周期、事件发射、ask/answer、取消"""
import asyncio
import logging
import threading
import time
import traceback
import uuid
from typing import Any, Awaitable, Callable

from ..appdb import AppDatabase
from ..errors import ApiError
from .events import EventBus

_log = logging.getLogger(__name__)

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

    # ---- 事件发射（线程安全；落库即唯一事实源，SSE 端轮询回放） ----

    def _emit(self, kind: str, data: dict):
        self.registry.app_db.add_event(self.id, kind, data)

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
        already = self.cancel_event.is_set()
        self.cancel_event.set()
        if not already:
            # 取消是协作式的：在飞的 API 调用/文件操作要跑完才到检查点，
            # 明确告知用户任务正在取消而非卡住（事件入库，重连回放可见）
            self.emit_stage('正在取消（等待当前条目完成）…')
            self.emit_log('收到取消请求：将在当前条目完成后停止')
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
               body: Callable[[Job], Awaitable[Any]],
               exclusive: bool = False) -> Job:
        """创建并启动任务。body(job) 为协程，返回值写入 result。

        exclusive=True 时同 kind 已有活跃任务（running/waiting_input）则抛 409。
        """
        if exclusive:
            for rec in self.app_db.list_jobs(active_only=True):
                if rec['kind'] == kind:
                    raise ApiError(
                        409, 'JOB_ACTIVE',
                        f'已有同类型任务正在进行: {rec["label"] or rec["id"]}')
        job_id = self.app_db.create_job(kind, label, payload)
        job = Job(self, job_id, kind, label)
        self._jobs[job_id] = job

        async def _run():
            try:
                result = await body(job)
                if job.cancel_event.is_set():
                    self._finalize(job, {'status': 'cancelled'},
                                   {'status': 'cancelled'})
                else:
                    self._finalize(
                        job,
                        {'status': 'succeeded', 'result': result or {},
                         'progress': 1.0},
                        {'status': 'succeeded', 'result': result or {}})
            except JobCancelled:
                self._finalize(job, {'status': 'cancelled'},
                               {'status': 'cancelled'})
            except Exception as e:
                if job.cancel_event.is_set():
                    # 取消归一化：cancel 后任务体在飞操作撞到的异常（连接中断、
                    # 文件占用等）一律按取消处理，任务体无需各自判断；
                    # 异常留痕——取消窗口内的真实错误（磁盘/代码缺陷）不应消失
                    _log.info('任务 %s 取消窗口内异常已归一化为 cancelled: %s',
                              job.id, e, exc_info=True)
                    self._finalize(job, {'status': 'cancelled'},
                                   {'status': 'cancelled'})
                else:
                    err = f'{e}\n\n{traceback.format_exc()}'
                    self._finalize(
                        job, {'status': 'failed', 'error': err},
                        {'status': 'failed', 'error': str(e),
                         'detail': traceback.format_exc()})
            finally:
                job.done = True
                # 句柄保留在内存中直到终态事件送达（前端可能立刻 answer/cancel 已结束的任务）
                self._loop_gc(job_id)

        asyncio.create_task(_run())
        return job

    def _finalize(self, job: Job, fields: dict, event: dict):
        """终态收尾：先更新任务记录、后落终态事件（SSE 轮询端据此关流）。

        关停窗口内库可能已关闭：失败仅记日志，shutdown 兜底会把
        残留活跃任务标为 interrupted。
        """
        try:
            self.app_db.update_job(job.id, **fields)
            job._emit('status', event)
        except Exception as e:
            _log.warning('任务 %s 终态写库失败: %s', job.id, e, exc_info=True)
            # 非关停场景（如 result 含不可 JSON 序列化对象）不能把任务
            # 留成 running 僵尸（SSE 永不关流、exclusive 永久 409）：
            # 降级补落 failed（纯字符串 payload，不会再因序列化失败）；
            # 关停窗口内库已关闭时内层 try 兜住，由 shutdown 兜底标 interrupted
            if fields.get('status') != 'failed':
                try:
                    self.app_db.update_job(
                        job.id, status='failed',
                        error=f'任务收尾失败: {e}')
                    job._emit('status', {'status': 'failed',
                                         'error': f'任务收尾失败: {e}'})
                except Exception:
                    _log.warning('任务 %s 降级收尾也失败', job.id, exc_info=True)

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
