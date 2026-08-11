"""导出自愈核心件测试：unwrap / 错误解析 / 条目定位

迁移自 tools/test_export_healer.py（模块级脚本）为正式 pytest 用例。
注意 unwrap_candidates 的契约：返回 (成功拆除的 Candidate 列表, skipped 数)，
而非计数——调用方按返回的 Candidate 逐行更新状态。
"""
import pytest

from embedded_strings import apply_wrapping, find_candidates, unwrap_candidates
from services.export_healer import ExportHealer, _ERR_RE

# ---- 1. wrap → unwrap 往返 ----

RPY = '''screen s():
    text "Settings"
    textbutton "Back" action Return()
    $ renpy.notify("Welcome back")
'''


@pytest.fixture
def wrapped_game(tmp_path):
    """构造含 3 个可显示字符串的游戏目录，返回 (游戏根, 源文件, 候选列表)"""
    game = tmp_path / 'game'
    game.mkdir()
    f = game / 's.rpy'
    f.write_text(RPY, encoding='utf-8')
    cands = find_candidates(str(tmp_path))
    return tmp_path, f, cands


def test_find_candidates(wrapped_game):
    _, _, cands = wrapped_game
    assert len(cands) == 3


def test_wrap_unwrap_roundtrip(wrapped_game):
    _, f, cands = wrapped_game
    wrapped, skipped = apply_wrapping(cands)
    assert (wrapped, skipped) == (3, 0)
    assert '_(' in f.read_text(encoding='utf-8')

    removed, skipped = unwrap_candidates(cands)
    assert len(removed) == 3 and skipped == 0
    assert f.read_text(encoding='utf-8') == RPY


def test_unwrap_twice_all_skipped(wrapped_game):
    """位置不匹配（已拆除）时全部跳过，不重复拆"""
    _, f, cands = wrapped_game
    apply_wrapping(cands)
    unwrap_candidates(cands)
    _, skipped = unwrap_candidates(cands)
    assert skipped == 3


# ---- 2. 报错解析 ----

SAMPLE_OUTPUT = '''
I'm sorry, but an uncaught exception occurred.

While running game code:
  File "game/tl/chinese/academy.rpy", line 35: expected ',' or ']'.
    new "学院评价 [rank
                     ^
  File "game/definitions/phone.rpy", line 19, in <module>
SyntaxError: invalid syntax
'''


def _parse_errors(output):
    return [
        {'file': m.group(1).replace('\\', '/'), 'line': int(m.group(2)),
         'msg': m.group(3).strip()[:200]}
        for m in _ERR_RE.finditer(output)
    ]


def test_err_re_parses_all_errors():
    parsed = _parse_errors(SAMPLE_OUTPUT)
    assert len(parsed) == 2


def test_err_re_locations():
    parsed = _parse_errors(SAMPLE_OUTPUT)
    assert parsed[0]['file'] == 'game/tl/chinese/academy.rpy'
    assert parsed[0]['line'] == 35
    assert parsed[1]['file'] == 'game/definitions/phone.rpy'
    assert parsed[1]['line'] == 19


# ---- 3. tl 条目定位 ----

TL_STRINGS = '''translate chinese strings:

    # game/script.rpy:10
    old "Settings"
    new "设置"

    old "ACADEMY EVALUATION"
    new "学院评价 [rank"
'''

TL_DIALOGUE = '''# game/script.rpy:70
translate chinese start_abc:
    # e "Hello there"
    e "你好 [name"
'''


def test_locate_strings_entry(tmp_path):
    """报错在第 8 行（new 行坏）→ 向上找到对应 old"""
    tl_file = tmp_path / 'academy.rpy'
    tl_file.write_text(TL_STRINGS, encoding='utf-8')
    h = ExportHealer.__new__(ExportHealer)
    entry = h._locate_entry(tl_file, 8)
    assert entry == ('ui', 'ACADEMY EVALUATION')


def test_locate_dialogue_entry(tmp_path):
    tl_file = tmp_path / 'dlg.rpy'
    tl_file.write_text(TL_DIALOGUE, encoding='utf-8')
    h = ExportHealer.__new__(ExportHealer)
    entry = h._locate_entry(tl_file, 4)
    assert entry == ('dialogue', 'Hello there')


# ---- 4. tl 条目对照内嵌标记库 ----

MARKED = [
    {'id': 1, 'rel_file': 's.rpy', 'line': 2, 'text': 'Settings'},
    {'id': 2, 'rel_file': 's.rpy', 'line': 5, 'text': '第一行\n第二行'},
]


def test_match_embedded_hit():
    assert ExportHealer._match_embedded('Settings', MARKED)['id'] == 1


def test_match_embedded_escaped_newline():
    assert ExportHealer._match_embedded('第一行\\n第二行', MARKED)['id'] == 2


def test_match_embedded_miss():
    assert ExportHealer._match_embedded('Unknown Text', MARKED) is None


def test_match_embedded_none():
    assert ExportHealer._match_embedded(None, MARKED) is None
