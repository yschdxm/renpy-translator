"""翻译文本导入 API：JSON / 翻译Word 回导译文

单步导入：上传文件 → 解析生成计划 → 直接写库，返回各类型统计。
- JSON（本工具导出格式）：按原文匹配，顺序无关
- 翻译Word（仅原文每条一段）：按段落顺序对齐，段落数不一致直接报错
"""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile

from ..deps import require_project
from ..errors import ApiError
from ..state import AppState

router = APIRouter(prefix='/current/import', tags=['import'])

_MAX_UPLOAD = 64 * 1024 * 1024


@router.post('/texts')
async def import_texts(file: UploadFile = File(...),
                       types: str = Form(''),
                       filter: str = Form(''),
                       state: AppState = Depends(require_project)):
    from services import text_import

    ext = Path(file.filename or '').suffix.lower()
    if ext not in ('.json', '.docx'):
        raise ApiError(400, 'BAD_FILE', '仅支持导入 .json 或翻译 .docx 文件')
    content = await file.read()
    if len(content) > _MAX_UPLOAD:
        raise ApiError(400, 'TOO_LARGE', '文件超过 64MB')

    sel_types = json.loads(types) if types else []
    flt = json.loads(filter) if filter else None
    if ext == '.docx' and not sel_types:
        raise ApiError(400, 'BAD_TYPES',
                       '翻译 Word 按段落顺序对齐导入，请选择与导出时一致的内容类型')

    try:
        if ext == '.json':
            pairs = text_import.parse_json_import(content.decode('utf-8-sig'))
            plan, stats = await state.db_call(
                text_import.build_json_plan, state.db, pairs)
        else:
            lines = await state.run_sync(text_import.read_tword_lines, content)
            plan, stats = await state.db_call(
                text_import.build_tword_plan, state.db, lines, sel_types, flt)
    except ValueError as e:
        raise ApiError(400, 'BAD_IMPORT', str(e)) from e

    counts = await state.db_call(text_import.apply_plan, state.db, plan)
    return {'counts': counts, 'total': len(plan), 'stats': stats}
