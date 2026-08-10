"""日志 API：全局环形缓冲（TranslationLogger 的内存 handler）"""
from fastapi import APIRouter, Depends

from ..deps import get_state
from ..state import AppState

router = APIRouter(tags=['logs'])


@router.get('/logs')
async def get_logs(limit: int = 200, panel: str = '',
                   state: AppState = Depends(get_state)):
    # 翻译工作线程在 append，事件循环线程 list() 迭代偶发
    # 'deque mutated during iteration'；抓到 RuntimeError 重试取快照即可
    for _ in range(3):
        try:
            entries = list(state.log_buffer)
            break
        except RuntimeError:
            continue
    else:
        entries = []
    if panel:
        entries = [e for e in entries if e['panel'] == panel]
    return entries[-limit:]
