"""任务系统：db 持久化 + SSE 实时推送 + ask/answer 交互 + 取消

- 任务与事件落 data/app.db（重启后 running/waiting_input → interrupted，可续跑）
- emit_* 线程安全（服务在 executor 线程中调用，经 call_soon_threadsafe 扇出）
- ask() 挂起任务直到 POST /jobs/{id}/answer；取消时抛 JobCancelled
"""
from .events import EventBus
from .registry import Job, JobCancelled, JobRegistry

__all__ = ['EventBus', 'Job', 'JobCancelled', 'JobRegistry']
