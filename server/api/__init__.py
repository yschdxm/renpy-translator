"""API 路由聚合"""
from functools import lru_cache

from fastapi import APIRouter

from . import configs, embedded, export, glossary, jobs, logs, names, projects, session, system, texts

router = APIRouter()


@lru_cache(maxsize=1)
def _app_version() -> str:
    """版本号：importlib.metadata 优先（uv/pip 安装态），解析 pyproject.toml 兜底
    （源码直接运行 / PyInstaller 冻结时 dist 元数据不可用）。

    lru_cache：/health 是高频探测端点，版本号进程内不变，避免每次重复
    importlib 查询/磁盘 IO。"""
    try:
        from importlib.metadata import version
        return version('renpy-translator')
    except Exception:
        pass
    try:
        import sys
        import tomllib
        from pathlib import Path
        # 冻结后仓库根不可达，以 _MEIPASS 为基准兜底找 pyproject（通常找不到→unknown）
        base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
        with (base / 'pyproject.toml').open('rb') as f:
            return tomllib.load(f)['project']['version']
    except Exception:
        return 'unknown'


@router.get('/health')
async def health():
    return {'ok': True, 'version': _app_version()}


@router.get('/health/deep')
async def health_deep():
    """深度健康检查：验证惰性加载的重依赖在打包环境可用

    CI 冒烟用——普通 /health 只证明进程起来了，openai/tiktoken 这类
    首次使用才初始化的依赖（tiktoken_ext 插件元数据缺失曾导致打包后崩）
    需要真实触发一次才能暴露问题。
    """
    import openai  # noqa: F401
    import tiktoken
    enc = tiktoken.get_encoding('cl100k_base')
    return {'ok': True, 'version': _app_version(),
            'checks': {'openai': getattr(openai, '__version__', 'unknown'),
                       'tiktoken_cl100k_base': enc.n_vocab > 0}}


router.include_router(session.router)
router.include_router(projects.router)
router.include_router(configs.router)
router.include_router(logs.router)
router.include_router(jobs.router)
router.include_router(texts.router)
router.include_router(names.router)
router.include_router(glossary.router)
router.include_router(embedded.router)
router.include_router(export.router)
router.include_router(system.router)
