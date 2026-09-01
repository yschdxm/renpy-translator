"""无显示名角色（泛指形参/玩家命名主角）的翻译状态判定

背景（bug）：dd4d85f 让无显示名角色 display_name 留空、AI 跳过人名翻译
仅分析——但统计/前置检查仍按 cn_name='' 把它们算"未翻译"：
人名表显示"待翻译"，且 get_char_dict_count.untranslated>0 触发对话翻译
前置 409 卡死。修复：无显示名角色不计入翻译统计与待翻译列表；
"未分析"判定改为按行 profile_json（空名角色共享空显示名，name-key
字典会互相误覆盖）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import ProjectDatabase  # noqa: E402


def _db(tmp_path):
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    db.connect()
    return db


def _insert(db, variable, display_name, cn_name='', profile_json=''):
    db._conn.execute(
        "INSERT INTO characters (variable, display_name, cn_name, profile_json,"
        " lines_count, is_placeholder) VALUES (?, ?, ?, ?, 10, 0)",
        (variable, display_name, cn_name, profile_json))
    db._conn.commit()


def _set_profile(db, variable):
    db._conn.execute(
        "UPDATE characters SET profile_json=? WHERE variable=?",
        (json.dumps({'性格特征': 'x'}, ensure_ascii=False), variable))
    db._conn.commit()


# ---- 统计与待翻译列表 ----

def test_empty_name_not_counted(tmp_path):
    """无显示名角色不计入人名翻译统计（没有可翻译的名字）"""
    db = _db(tmp_path)
    _insert(db, 'mc', '')                        # 无显示名
    _insert(db, 'e', 'Eileen')                   # 未翻译
    counts = db.get_char_dict_count()
    assert counts['total'] == 1                  # 只算 Eileen
    assert counts['untranslated'] == 1
    db._conn.execute(
        "UPDATE characters SET cn_name='艾琳' WHERE variable='e'")
    db._conn.commit()
    counts = db.get_char_dict_count()
    assert counts['untranslated'] == 0           # mc 不算未翻译 → 前置可过
    db.close()


def test_empty_name_excluded_from_untranslated_list(tmp_path):
    db = _db(tmp_path)
    _insert(db, 'mc', '')
    _insert(db, 'girl', '')
    _insert(db, 'e', 'Eileen')
    todo = db.get_untranslated_characters()
    assert [c['variable'] for c in todo] == ['e']
    db.close()


# ---- 对话翻译前置检查 ----

class _FakeState:
    def __init__(self, db):
        self.db = db

    async def db_call(self, fn, *args):
        return fn(*args)


async def test_prerequisite_passes_with_empty_name(tmp_path):
    """无显示名角色不卡人名翻译前置；未分析按行 profile_json 判定"""
    from server.api.texts import _check_dialogue_prerequisites
    from server.errors import ApiError

    db = _db(tmp_path)
    _insert(db, 'mc', '')
    _insert(db, 'e', 'Eileen', cn_name='艾琳')
    # 人名翻译前置已过（mc 不算未翻译），但都未分析 → 报未分析
    with pytest.raises(ApiError, match='未分析'):
        await _check_dialogue_prerequisites(_FakeState(db))
    # 两个角色都分析完（mc 的档案按行 profile_json，不靠显示名键）→ 通过
    _set_profile(db, 'mc')
    _set_profile(db, 'e')
    await _check_dialogue_prerequisites(_FakeState(db))  # 不抛
    db.close()


async def test_prerequisite_blocks_real_untranslated(tmp_path):
    """有显示名未翻译的角色仍然卡前置（防回归）"""
    from server.api.texts import _check_dialogue_prerequisites
    from server.errors import ApiError

    db = _db(tmp_path)
    _insert(db, 'mc', '')
    _insert(db, 'e', 'Eileen')                   # 未翻译
    with pytest.raises(ApiError, match='未翻译'):
        await _check_dialogue_prerequisites(_FakeState(db))
    db.close()


async def test_prerequisite_empty_name_unanalyzed_shows_variable(tmp_path):
    """无显示名角色未分析时提示信息回退变量名（不是空字符串）"""
    from server.api.texts import _check_dialogue_prerequisites
    from server.errors import ApiError

    db = _db(tmp_path)
    _insert(db, 'mc', '')
    with pytest.raises(ApiError, match='mc'):
        await _check_dialogue_prerequisites(_FakeState(db))
    db.close()
