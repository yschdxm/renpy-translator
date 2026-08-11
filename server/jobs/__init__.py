"""任务系统：db 持久化 + SSE 事件流 + ask/answer 交互 + 取消

- 任务与事件落 data/app.db（重启后 running/waiting_input → interrupted，
  终态，仅作历史记录，无断点续跑实现）
- 事件表是唯一事实源：emit_* 线程安全（服务在 executor 线程中直接落库，
  AppDatabase 内锁），SSE 端按 after_seq 轮询回放
- ask() 挂起任务直到 POST /jobs/{id}/answer；取消时抛 JobCancelled
- EventBus 仅用于 _app 频道（关停广播等进程内通知），不再承载 job 事件
"""
from .events import EventBus
from .registry import TERMINAL_STATUSES, Job, JobCancelled, JobRegistry

__all__ = ['EventBus', 'Job', 'JobCancelled', 'JobRegistry', 'TERMINAL_STATUSES']
