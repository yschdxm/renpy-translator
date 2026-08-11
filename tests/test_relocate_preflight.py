"""server/state.py relocate_home 迁移预检分支测试

只覆盖动手前抛出的三个 ApiError 分支（纯路径判断，不做真实迁移）：
SAME_DIR（新旧相同）、NESTED_DIR（互为子目录）、DEST_CONFLICT（目标同名数据非空）。

AppState 可以在 tmp_path 上完整实例化（AppDatabase/ConfigManager 等都落在
RT_HOME 指向的临时目录下），故直接测真实 relocate_home。
"""
import tempfile

import pytest


@pytest.fixture
def state(tmp_path, monkeypatch):
    """构造一个数据根在临时目录的 AppState（RT_HOME 指过去，避免碰真实目录）"""
    home = tmp_path / 'old_home'
    monkeypatch.setenv('RT_HOME', str(home))

    from server.state import AppState
    old_tempdir = tempfile.tempdir  # AppState 会全局改写 tempfile.tempdir
    s = AppState(home)
    yield s
    tempfile.tempdir = old_tempdir
    try:
        s.app_db.close()
    except Exception:
        pass


async def test_relocate_same_dir(state):
    from server.errors import ApiError
    with pytest.raises(ApiError) as exc_info:
        await state.relocate_home(state.root)
    assert exc_info.value.status == 400
    assert exc_info.value.code == 'SAME_DIR'


async def test_relocate_nested_new_inside_old(state):
    from server.errors import ApiError
    with pytest.raises(ApiError) as exc_info:
        await state.relocate_home(state.root / 'sub')
    assert exc_info.value.status == 400
    assert exc_info.value.code == 'NESTED_DIR'


async def test_relocate_nested_old_inside_new(state):
    from server.errors import ApiError
    with pytest.raises(ApiError) as exc_info:
        await state.relocate_home(state.root.parent)
    assert exc_info.value.status == 400
    assert exc_info.value.code == 'NESTED_DIR'


async def test_relocate_dest_conflict(state, tmp_path):
    from server.errors import ApiError
    # 旧数据根 projects/ 由 ProjectManager 在建 AppState 时创建（RT_HOME 同源）；
    # 在目标目录放同名非空 projects/ 触发冲突
    assert (state.root / 'projects').is_dir()
    dest = tmp_path / 'new_home'
    (dest / 'projects').mkdir(parents=True)
    (dest / 'projects' / 'leftover.txt').write_text('x', encoding='utf-8')

    with pytest.raises(ApiError) as exc_info:
        await state.relocate_home(dest)
    assert exc_info.value.status == 409
    assert exc_info.value.code == 'DEST_CONFLICT'


async def test_relocate_dest_empty_dirs_no_conflict(state, tmp_path, monkeypatch):
    """目标存在同名但为空的目录不算冲突：预检应放行（在迁移阶段打桩拦截，
    用哨兵异常证明流程越过了预检，而不是抛 DEST_CONFLICT）"""
    dest = tmp_path / 'new_home'
    (dest / 'projects').mkdir(parents=True)  # 空目录：不冲突

    def _boom(src, dst):
        raise RuntimeError('preflight-passed-sentinel')

    monkeypatch.setattr('server.state._move_tree', _boom)

    with pytest.raises(RuntimeError, match='preflight-passed-sentinel'):
        await state.relocate_home(dest)
