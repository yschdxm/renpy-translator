"""日志 API：全局环形缓冲（TranslationLogger 的内存 handler）"""
from fastapi import APIRouter, Depends

from ..deps import get_state
from ..state import AppState

router = APIRouter(tags=['logs'])


@router.get('/logs')
async def get_logs(limit: int = 200, panel: str = '',
                   state: AppState = Depends(get_state)):
    entries = list(state.log_buffer)
    if panel:
        entries = [e for e in entries if e['panel'] == panel]
    return entries[-limit:]
