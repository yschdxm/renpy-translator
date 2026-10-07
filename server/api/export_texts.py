"""翻译文本导出 API：对话/UI/人名/术语 -> txt/json/xlsx/docx

与游戏导出（export.py）同前缀不同路径。数据量级小（实测量级 1-3 秒），
同步生成不走 jobs 系统；产物落 exports/{项目名}/，下载/打开目录复用
projects.py 的 packages 端点。
"""
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..deps import require_project
from ..errors import ApiError
from ..state import AppState

router = APIRouter(prefix='/current/export', tags=['export'])

ExportType = Literal['dialogue', 'ui', 'names', 'glossary']


class TextExportFilter(BaseModel):
    """「仅导出当前筛选」：仅作用于 content_type 指定的表"""
    content_type: Literal['dialogue', 'ui'] = 'dialogue'
    filter_mode: str = 'all'
    search: str = ''
    character: str = ''


class TextExportIn(BaseModel):
    types: list[ExportType] = Field(min_length=1)
    # tword = 翻译 Word：仅原文、每条一段（供译者翻译后导回）
    format: Literal['txt', 'json', 'xlsx', 'docx', 'tword'] = 'xlsx'
    # 按类型给列 key 列表（白名单见 services.text_export.COLUMN_DEFS）；
    # 空 dict = 各类型全部列；tword 强制只取原文
    columns: dict[str, list[str]] = {}
    filename: str = ''          # 可选自定义主名（后端 sanitize + 去重）
    filter: TextExportFilter | None = None


@router.post('/texts')
async def export_texts(req: TextExportIn,
                       state: AppState = Depends(require_project)):
    from services import text_export
    from .projects import _exports_dir

    # 规范类型序（与勾选顺序无关）：tword 导出/导入按序对齐依赖此顺序
    types = [t for t in text_export.EXPORT_TYPES if t in set(req.types)]
    flt = req.filter.model_dump() if req.filter else None
    try:
        if req.format == 'tword':
            columns = {t: ['original'] for t in types}
        else:
            columns = req.columns
        data = await state.db_call(
            text_export.collect_data, state.db, types, columns, flt)
    except ValueError as e:
        raise ApiError(400, 'BAD_COLUMNS', str(e)) from e

    exports_dir = _exports_dir(state, state.current_project)
    try:
        path, counts = await state.run_sync(
            text_export.write_export_file, data, exports_dir,
            state.current_project, req.format, req.filename)
    except ValueError as e:
        raise ApiError(400, 'BAD_REQUEST', str(e)) from e

    return {
        'file': path.name,
        'download_url': f'/api/projects/{state.current_project}'
                        f'/packages/{path.name}',
        'counts': counts,
    }
