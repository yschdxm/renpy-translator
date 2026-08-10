"""FastAPI 应用工厂：CORS、API 路由、SPA 静态挂载（fallback 到 index.html）"""
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .errors import register_error_handlers

ROOT = Path(__file__).resolve().parent.parent
# 冻结后 src/ 在 _MEIPASS 下（作为 data 打包）；开发态在仓库根
_BUNDLE = Path(getattr(sys, '_MEIPASS', ROOT))
SRC = _BUNDLE / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rt_home import home as _home  # noqa: E402

# PyInstaller 冻结时前端产物在 _MEIPASS 下
_WEB_DIST = _BUNDLE / 'web' / 'dist'

_req_logger = logging.getLogger('renpy_translator.requests')


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .state import AppState
    app.state.app_state = AppState(app.state.root)
    await app.state.app_state.startup()
    yield
    # 关停广播在 /api/shutdown 端点里做（lifespan 退出时连接已被 uvicorn 断开）
    await app.state.app_state.shutdown()


def create_app(root: Path = None) -> FastAPI:
    app = FastAPI(title="Ren'Py 翻译工具", lifespan=lifespan)
    # 用户数据根（data/app.db、logs/ 等的基准）；冻结后为 exe 目录
    app.state.root = Path(root) if root else _home()

    @app.middleware('http')
    async def request_status_log(request: Request, call_next):
        """按状态码分级记录请求：4xx 警告、5xx 错误（替代 uvicorn 一刀切 INFO）"""
        try:
            response = await call_next(request)
        except Exception as e:
            _req_logger.error('%s %s -> 异常: %s',
                              request.method, request.url.path, e)
            raise
        status = response.status_code
        if status >= 500:
            _req_logger.error('%s %s -> %s',
                              request.method, request.url.path, status)
        elif status >= 400:
            _req_logger.warning('%s %s -> %s',
                                request.method, request.url.path, status)
        return response

    register_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=['*'],
        allow_methods=['*'],
        allow_headers=['*'],
    )

    from .api import router as api_router
    app.include_router(api_router, prefix='/api')

    if _WEB_DIST.is_dir():
        app.mount('/assets', StaticFiles(directory=_WEB_DIST / 'assets'),
                  name='assets')

        @app.get('/{full_path:path}', include_in_schema=False)
        async def spa_fallback(full_path: str):
            # resolve 后必须仍在 dist 内，防止 %2e%2e/ 之类目录穿越读出任意文件
            target = (_WEB_DIST / full_path).resolve()
            if (full_path and target.is_relative_to(_WEB_DIST.resolve())
                    and target.is_file()):
                return FileResponse(target)
            # index.html 必须每次重校验：重新构建后 chunk hash 全变，
            # 缓存的旧入口会让未访问页面的动态 import 404（菜单点击无反应）。
            # /assets 下的文件按内容 hash 命名，可安全使用默认缓存。
            return FileResponse(_WEB_DIST / 'index.html',
                                headers={'Cache-Control': 'no-cache'})

    return app
