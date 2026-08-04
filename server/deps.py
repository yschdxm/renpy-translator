"""FastAPI 依赖：状态获取与项目守卫"""
from fastapi import Request

from .errors import ApiError
from .state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def require_project(request: Request) -> AppState:
    """需要已打开项目的路由守卫；未打开 → 409（响亮失败）"""
    state = get_state(request)
    if state.db is None:
        raise ApiError(409, 'NO_PROJECT', '请先打开项目')
    return state
