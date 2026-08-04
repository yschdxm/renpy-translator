"""SSE 事件总线：每任务多订阅者，队列扇出（仅在事件循环线程内调用）"""
import asyncio


class EventBus:
    def __init__(self):
        self._subs: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue):
        subs = self._subs.get(job_id)
        if subs and q in subs:
            subs.remove(q)
            if not subs:
                del self._subs[job_id]

    def publish(self, job_id: str, event: dict):
        """事件循环线程内调用；队列满时丢给最旧的订阅者也必须保证不断流——
        前端有 after_seq 游标，断流后重连可从 db 回放补齐"""
        for q in self._subs.get(job_id, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
