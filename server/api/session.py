"""会话 API：当前项目、统计、开关项目"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import get_state, require_project
from ..state import AppState

router = APIRouter(tags=['session'])


class OpenRequest(BaseModel):
    name: str


async def _stats(state: AppState) -> dict:
    if state.db is None:
        return {}
    d = await state.db_call(state.db.get_dialogue_count)
    u = await state.db_call(state.db.get_ui_text_count)
    n = await state.db_call(state.db.get_char_dict_count)
    return {'dialogue': d, 'ui': u, 'names': n}


@router.get('/session')
async def get_session(state: AppState = Depends(get_state)):
    return {
        'current_project': state.current_project,
        'has_translator': state.translator is not None,
        'stats': await _stats(state),
        'active_jobs': state.app_db.list_jobs(active_only=True),
        'interrupted_jobs': state.interrupted_count,
    }


@router.post('/session/open')
async def open_project(req: OpenRequest, state: AppState = Depends(get_state)):
    await state.open_project(req.name)
    return {
        'current_project': state.current_project,
        'has_translator': state.translator is not None,
        'stats': await _stats(state),
    }


@router.post('/session/close')
async def close_project(state: AppState = Depends(require_project)):
    await state.close_project()
    return {'current_project': ''}
