"""术语表 API：分页列表/新增/编辑/删除（编辑用 INSERT OR REPLACE 覆盖语义）"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import require_project
from ..errors import ApiError
from ..state import AppState

router = APIRouter(prefix='/current/glossary', tags=['glossary'])


@router.get('')
async def list_glossary(page: int = 0, size: int = 50, search: str = '',
                        source: str = '', sort_by: str = 'en_term',
                        sort_order: str = 'asc',
                        state: AppState = Depends(require_project)):
    rows, total = await state.db_call(
        state.db.get_glossary_page, page, size, search, source,
        sort_by, sort_order)
    return {'rows': rows, 'total': total}


class AddTermIn(BaseModel):
    en_term: str
    cn_term: str
    term_type: str = 'other'


@router.post('')
async def add_term(req: AddTermIn,
                   state: AppState = Depends(require_project)):
    en = req.en_term.strip()
    if not en:
        raise ApiError(400, 'EMPTY_TERM', '英文术语不能为空')
    await state.db_call(
        state.db.add_glossary_term, en, req.cn_term, req.term_type, 'manual')
    return {'ok': True}


class UpdateTermIn(BaseModel):
    cn_term: str
    term_type: str = 'other'


@router.patch('/{en_term}')
async def update_term(en_term: str, req: UpdateTermIn,
                      state: AppState = Depends(require_project)):
    # 先取现有行：确认存在 + 保留原 source（ai 提取的词编辑后仍标记 ai）
    existing = await state.db_call(state.db.get_glossary_term, en_term)
    if not existing:
        raise ApiError(404, 'NOT_FOUND', f'术语不存在: {en_term}')
    await state.db_call(
        state.db.add_glossary_term, en_term, req.cn_term, req.term_type,
        existing['source'])
    return {'ok': True}


@router.delete('/{en_term}')
async def delete_term(en_term: str,
                      state: AppState = Depends(require_project)):
    existing = await state.db_call(state.db.get_glossary_term, en_term)
    if not existing:
        raise ApiError(404, 'NOT_FOUND', f'术语不存在: {en_term}')
    await state.db_call(state.db.delete_glossary_term, en_term)
    return {'ok': True}
