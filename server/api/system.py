"""系统 API：服务关停（run.py --mode stop 用）+ 应用级事件流"""
import asyncio
import json
import signal
import threading
import time

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from ..deps import get_state
from ..state import AppState

router = APIRouter(tags=['system'])

# 应用级事件在事件总线上的固定频道
APP_CHANNEL = '_app'


def _arm_exit_watchdog(seconds: float = 25):
    """优雅退出的最后防线：SIGINT 已发出但 uvicorn/lifespan 偶发悬挂
    （半开连接、残留线程、关库阻塞）时，超时硬退保证进程不残留。
    正常关停（含迁移重启的 finally 链）几秒内完成，进程已退、
    该 daemon 线程随之消失，不会误伤。"""
    def _watchdog():
        time.sleep(seconds)
        import os
        os._exit(0)
    threading.Thread(target=_watchdog, daemon=True).start()


def schedule_restart(state: AppState):
    """延迟自动重启服务（迁移后清理占用文件用）。

    广播 restarting（前端显示「正在重启」而非「服务已停止」并在恢复后
    自动刷新）→ 等当前响应送达 → 写重启标记 → 触发优雅退出。
    run.py 退出前发现标记即以分离进程拉起新服务。
    """
    async def _restart():
        state.bus.publish(APP_CHANNEL,
                          {'seq': 0, 'kind': 'app',
                           'data': {'type': 'restarting'}})
        await asyncio.sleep(1.0)  # 让迁移响应与事件先送达
        flag = state.root / 'data' / 'restart.flag'
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text('1', encoding='utf-8')

        def _raise():
            time.sleep(0.3)
            signal.raise_signal(signal.SIGINT)

        threading.Thread(target=_raise, daemon=True).start()
        _arm_exit_watchdog()

    asyncio.get_event_loop().create_task(_restart())


@router.post('/shutdown')
async def shutdown(state: AppState = Depends(get_state)):
    """优雅关停服务：先广播 shutdown（此时 SSE 连接还开着），再触发信号。

    注意广播必须在这里做而不能放 lifespan shutdown——uvicorn 会先断开
    客户端连接再执行 lifespan 退出，那时广播谁都收不到。
    """
    state.bus.publish(APP_CHANNEL,
                      {'seq': 0, 'kind': 'app', 'data': {'type': 'shutdown'}})
    await asyncio.sleep(0.5)  # 让 SSE 把事件推出去

    def _raise():
        time.sleep(0.3)  # 让本响应先返回
        # raise_signal 走 Python 信号处理器（Windows 上同样有效），
        # uvicorn 捕获后执行优雅关闭；os.kill(SIGTERM) 在 Windows 是硬杀
        signal.raise_signal(signal.SIGINT)

    threading.Thread(target=_raise, daemon=True).start()
    _arm_exit_watchdog()
    return {'ok': True, 'message': '服务正在停止'}


@router.get('/events')
async def app_events(state: AppState = Depends(get_state)):
    """应用级事件流（SSE）：服务关停广播等，前端收到 shutdown 自行关窗/提示"""
    queue = state.bus.subscribe(APP_CHANNEL)

    async def gen():
        try:
            while True:
                event = await queue.get()
                yield {'event': 'app',
                       'data': json.dumps(event['data'], ensure_ascii=False)}
                if event['data'].get('type') == 'shutdown':
                    return
        finally:
            state.bus.unsubscribe(APP_CHANNEL, queue)

    return EventSourceResponse(gen(), ping=15)
