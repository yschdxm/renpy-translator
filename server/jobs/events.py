"""进程内事件总线：按频道多订阅者，队列扇出（仅在事件循环线程内调用）

job 事件不走这里（db 轮询是唯一事实源，见 api/jobs.py）；
本总线只剩 _app 频道（关停广播等一次性进程内通知）。
"""
import asyncio


class EventBus:
    def __init__(self):
        self._subs: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, channel: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.setdefault(channel, []).append(q)
        return q

    def unsubscribe(self, channel: str, q: asyncio.Queue):
        subs = self._subs.get(channel)
        if subs and q in subs:
            subs.remove(q)
            if not subs:
                del self._subs[channel]

    def publish(self, channel: str, event: dict):
        """事件循环线程内调用；队列满则丢弃（_app 频道是best-effort通知，
        订阅者收到任意一条即触发流程，丢重复事件无害）"""
        for q in self._subs.get(channel, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
