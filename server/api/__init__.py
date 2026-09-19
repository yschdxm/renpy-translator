"""API 路由聚合"""
from fastapi import APIRouter

from . import configs, embedded, export, glossary, graph, jobs, logs, names, projects, session, system, texts, updates
from ..version import app_version as _app_version

router = APIRouter()


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
router.include_router(graph.router)
router.include_router(system.router)
router.include_router(updates.router)
