"""结构化错误：响亮失败——完整 traceback 直达前端（本地单用户工具，traceback 即特性）"""
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """业务错误：带 HTTP 状态码与机器可读 code"""

    def __init__(self, status: int, code: str, message: str, detail: str = ''):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


def _payload(code: str, message: str, detail: str = '') -> dict:
    return {'error': {'code': code, 'message': message, 'detail': detail}}


def register_error_handlers(app: FastAPI):
    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status,
                            content=_payload(exc.code, exc.message, exc.detail))

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content=_payload('INTERNAL', str(exc) or type(exc).__name__,
                             traceback.format_exc()),
        )
