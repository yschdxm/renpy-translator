"""API 路由聚合"""
from fastapi import APIRouter

from . import configs, embedded, export, jobs, logs, names, projects, session, system, texts

router = APIRouter()


@router.get('/health')
async def health():
    return {'ok': True}


router.include_router(session.router)
router.include_router(projects.router)
router.include_router(configs.router)
router.include_router(logs.router)
router.include_router(jobs.router)
router.include_router(texts.router)
router.include_router(names.router)
router.include_router(embedded.router)
router.include_router(export.router)
router.include_router(system.router)
