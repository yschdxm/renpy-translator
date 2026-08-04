"""系统 API：服务关停（run.py --mode stop 用）"""
import signal
import threading
import time

from fastapi import APIRouter

router = APIRouter(tags=['system'])


@router.post('/shutdown')
async def shutdown():
    """优雅关停服务（走 lifespan shutdown：关库、标记未完成任务）"""

    def _raise():
        time.sleep(0.5)  # 让响应先返回
        # raise_signal 走 Python 信号处理器（Windows 上同样有效），
        # uvicorn 捕获后执行优雅关闭；os.kill(SIGTERM) 在 Windows 是硬杀
        signal.raise_signal(signal.SIGINT)

    threading.Thread(target=_raise, daemon=True).start()
    return {'ok': True, 'message': '服务正在停止'}
