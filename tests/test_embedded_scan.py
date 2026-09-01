"""内嵌文本扫描器（embedded_strings.find_candidates）测试

覆盖本期新增与修复：
- f/r/b/u 前缀字面量整体跳过（f-string 包 _() 会变 f_(...) 非法语法）
- df"x" 这类以 f 结尾的标识符不误杀
- screen 单引号字符串
- screen 参数默认值
- screen 作用域内 Notify（label 作用域不产出）
- 单行闭合三引号字面量产出候选（多行块仍跳过）
- python 显示调用点 hint/置信度增强
- 既有排除规则回归（隐式拼接链、Character 定义、已包 _()）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from embedded_strings import find_candidates


def _scan(tmp_path, files: dict) -> list:
    game = tmp_path / 'game'
    game.mkdir(exist_ok=True)
    for rel, content in files.items():
        p = game / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
    return find_candidates(str(tmp_path))


def _texts(cands):
    return {c.text for c in cands}


# ---- 前缀字面量 ----

def test_fstring_skipped(tmp_path):
    cands = _scan(tmp_path, {'a.rpy':
                             'label start:\n'
                             '    $ msg = f"Hello {name}"\n'
                             '    $ other = "Plain text here"\n'})
    assert 'Hello {name}' not in _texts(cands)
    assert 'Plain text here' in _texts(cands)


def test_r_b_u_prefix_skipped(tmp_path):
    cands = _scan(tmp_path, {'a.rpy':
                             'label start:\n'
                             '    $ a = r"raw string here"\n'
                             '    $ b = u"unicode string here"\n'})
    assert 'raw string here' not in _texts(cands)
    assert 'unicode string here' not in _texts(cands)


def test_identifier_ending_in_f_not_killed(tmp_path):
    """df"x" 的 f 是标识符尾部，不是字符串前缀"""
    cands = _scan(tmp_path, {'a.rpy':
                             'label start:\n'
                             '    $ df"Not a prefix at all"\n'})
    assert 'Not a prefix at all' in _texts(cands)


# ---- screen 单引号字符串 ----

def test_screen_single_quoted(tmp_path):
    cands = _scan(tmp_path, {'a.rpy':
                             "screen s:\n"
                             "    textbutton 'Click me now' action NullAction()\n"})
    assert 'Click me now' in _texts(cands)


# ---- screen 参数默认值 ----

def test_screen_param_default(tmp_path):
    cands = _scan(tmp_path, {'a.rpy':
                             'screen stats(title="Player Stats", n=5):\n'
                             '    text "x"\n'})
    hit = [c for c in cands if c.text == 'Player Stats']
    assert len(hit) == 1
    assert hit[0].kind == 'screen'
    assert '参数默认' in hit[0].hint


# ---- screen 作用域内 Notify ----

def test_notify_in_screen_scope(tmp_path):
    cands = _scan(tmp_path, {'a.rpy':
                             'screen s:\n'
                             '    textbutton "Go" action Notify("Saved ok")\n'})
    hit = [c for c in cands if c.text == 'Saved ok']
    assert len(hit) == 1
    assert hit[0].kind == 'screen'


def test_notify_wrapped_skipped(tmp_path):
    cands = _scan(tmp_path, {'a.rpy':
                             'screen s:\n'
                             '    textbutton "Go" action Notify(_("Saved ok"))\n'})
    assert 'Saved ok' not in _texts(cands)


# ---- 单行三引号 ----

def test_single_line_triple_produced(tmp_path):
    cands = _scan(tmp_path, {'a.rpy':
                             'label start:\n'
                             '    $ msg = """A whole sentence here."""\n'})
    hit = [c for c in cands if 'whole sentence' in c.text]
    assert len(hit) == 1
    assert hit[0].raw.startswith('"""')


def test_multi_line_triple_still_skipped(tmp_path):
    cands = _scan(tmp_path, {'a.rpy':
                             'label start:\n'
                             '    $ msg = """first line\n'
                             '    second line with words\n'
                             '    """\n'
                             '    $ other = "Real candidate here"\n'})
    assert 'second line with words' not in _texts(cands)
    assert 'first line' not in _texts(cands)
    assert 'Real candidate here' in _texts(cands)


# ---- python 显示调用点增强 ----

def test_display_call_hint_and_confidence(tmp_path):
    cands = _scan(tmp_path, {'a.rpy':
                             'label start:\n'
                             '    $ renpy.notify("Welcome back")\n'})
    hit = [c for c in cands if c.text == 'Welcome back']
    assert len(hit) == 1
    assert hit[0].hint.endswith('通知')
    assert hit[0].confidence == 'high'


# ---- 既有排除规则回归 ----

def test_existing_exclusions_regression(tmp_path):
    cands = _scan(tmp_path, {'a.rpy':
                             'define e = Character("Eileen")\n'
                             'label start:\n'
                             '    $ x = _("Already wrapped")\n'
                             '    $ y = "first "\n'
                             '          "second"\n'
                             '    $ z = "key_name"\n'})
    texts = _texts(cands)
    assert 'Eileen' not in texts
    assert 'Already wrapped' not in texts
    assert 'first ' not in texts      # 隐式拼接链不单独提取
    assert 'second' not in texts
    assert 'key_name' not in texts    # snake_case 键名过滤
