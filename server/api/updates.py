"""更新检查 API：GitHub Release latest 与当前版本比对 + 自动检查开关"""
import json
import re
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import get_state
from ..errors import ApiError
from ..state import AppState
from ..version import app_version

router = APIRouter(tags=['updates'])

RELEASES_LATEST = ('https://api.github.com/repos/yschdxm/renpy-translator'
                   '/releases/latest')
RELEASES_PAGE = ('https://github.com/yschdxm/renpy-translator/releases/latest')

# 设置键（data/app.db settings 表）：'1' 开 / '0' 关，缺省开
_SETTING_KEY = 'auto_update_check'


def _parse_version(v: str):
    """'v0.3.3'/'0.3.3' → (0, 3, 3)；解析不了（dev/unknown 等）返回 None"""
    m = re.fullmatch(r'[vV]?(\d+(?:\.\d+)*)', (v or '').strip())
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split('.'))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动重定向：releases/latest 恒 302 到 tag 页，要读的就是 Location"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_latest() -> dict:
    """拉最新版本信息（阻塞，调用方放 executor）；两层都失败抛异常

    1) GitHub API：能拿到发布说明与时间，但未认证限额 60 次/小时·IP
       （国内共享出口极易 403），只作首选；
    2) 降级 releases/latest 页的 302 Location：主站可达性好、无限额，
       只有 tag 与页面链接（无发布说明）。
    """
    req = urllib.request.Request(
        RELEASES_LATEST, headers={
            # GitHub API 强制要求 User-Agent，否则 403
            'User-Agent': 'renpy-translator-update-check',
            'Accept': 'application/vnd.github+json',
        })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        return {'tag': data.get('tag_name') or '',
                'url': data.get('html_url') or '',
                # 更新说明可能很长，弹窗展示截断
                'notes': (data.get('body') or '')[:2000],
                'published_at': data.get('published_at') or ''}
    except Exception:
        pass  # 限额/网络问题：落到 302 降级

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(urllib.request.Request(
            RELEASES_PAGE, headers={'User-Agent': 'Mozilla/5.0'}), timeout=10)
        raise RuntimeError('releases/latest 未按预期重定向')
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get('Location') or ''
            tag = loc.rstrip('/').rsplit('/', 1)[-1]
            if tag:
                return {'tag': tag, 'url': loc, 'notes': '',
                        'published_at': ''}
        raise


@router.get('/updates/check')
async def check_update(state: AppState = Depends(get_state)):
    """检查更新（GitHub Release）。

    网络失败返回 502：自动检查场景前端静默吞掉，手动检查时提示原因。
    当前版本解析不了（本地 dev 构建）不做升级判断，仅回显信息。
    """
    current = app_version()
    try:
        latest = await state.run_sync(_fetch_latest)
    except Exception as e:
        raise ApiError(502, 'UPDATE_CHECK_FAILED',
                       f'检查更新失败（无法访问 GitHub）: {e}')

    cur_v, lat_v = _parse_version(current), _parse_version(latest['tag'])
    has_update = (cur_v is not None and lat_v is not None and lat_v > cur_v)
    return {'current': current, 'latest': latest['tag'],
            'has_update': has_update, 'url': latest['url'],
            'notes': latest['notes'],
            'published_at': latest['published_at']}


@router.get('/updates/auto-check')
async def get_auto_check(state: AppState = Depends(get_state)):
    v = await state.run_sync(state.app_db.get_setting, _SETTING_KEY, '1')
    return {'enabled': v != '0'}


class AutoCheckIn(BaseModel):
    enabled: bool


@router.put('/updates/auto-check')
async def set_auto_check(req: AutoCheckIn, state: AppState = Depends(get_state)):
    await state.run_sync(
        state.app_db.set_setting, _SETTING_KEY, '1' if req.enabled else '0')
    return {'ok': True}
