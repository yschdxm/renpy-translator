"""静态用途分流（usage_rules.UsageAnalyzer）测试

不打文件系统：UsageAnalyzer 接受 files 字典（{rel: 行列表}），
直接构造源码片段验证站点分类、聚合判定与多跳变量追踪。

契约：
- 纯显示出现点 → KEEP；纯非显示 → DROP；任一未分类 → UNKNOWN
- 显示+非显示混合 → UNKNOWN + danger
- 拼接/格式化（fragment/format）归显示侧但置 static_fragment
  （渲染时已变形，apply 必须走 _() 包裹 → decide_apply_path='wrap'）
- 变量追踪 BFS（depth≤2）：重绑定/容器/循环/函数参数可追，
  任何一跳无法分类即 unknown
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from usage_rules import (KEEP, DROP, UNKNOWN, UsageAnalyzer,
                         decide_apply_path)


def _analyzer(files: dict) -> UsageAnalyzer:
    return UsageAnalyzer('', files={
        k: (v.split('\n') if isinstance(v, str) else v)
        for k, v in files.items()})


def _classify(files: dict, raw: str):
    c = SimpleNamespace(raw=raw)
    _analyzer(files).classify_all([c])
    return c


# ---- 站点分类：显示白名单 ----

def test_screen_keyword_display():
    c = _classify({'a.rpy': ['screen s:', '    textbutton "Save" action NullAction()']},
                  '"Save"')
    assert c.static_verdict == KEEP
    assert c.static_fragment is False
    assert decide_apply_path(c) == 'table'


def test_notify_action_display():
    c = _classify({'a.rpy': ['screen s:',
                             '    textbutton "Go" action Notify("Saved")']},
                  '"Saved"')
    assert c.static_verdict == KEEP


def test_renpy_notify_call_display():
    c = _classify({'a.rpy': ['label start:', '    $ renpy.notify("Welcome back")']},
                  '"Welcome back"')
    assert c.static_verdict == KEEP


def test_fstring_braces_display():
    c = _classify({'a.rpy': ['screen s:', "    text f\"{' '.join(words)}\""]},
                  "' '")
    assert c.static_verdict == KEEP


# ---- 站点分类：非显示黑名单 ----

def test_comparison_nondisplay():
    c = _classify({'a.rpy': ['label start:', '    if mode == "Save":', '        pass']},
                  '"Save"')
    assert c.static_verdict == DROP
    assert c.static_danger is True


def test_dict_key_nondisplay():
    c = _classify({'a.rpy': ['init python:', '    cfg = {"Save": 1}']},
                  '"Save"')
    assert c.static_verdict == DROP


def test_index_nondisplay():
    c = _classify({'a.rpy': ['label start:', '    $ x = data["Save"]']},
                  '"Save"')
    assert c.static_verdict == DROP


def test_screen_jump_action_nondisplay():
    c = _classify({'a.rpy': ['screen s:',
                             '    textbutton "Go" action Jump("Save")']},
                  '"Save"')
    assert c.static_verdict == DROP


def test_member_test_nondisplay():
    c = _classify({'a.rpy': ['label start:',
                             '    if "Save" in visited:', '        pass']},
                  '"Save"')
    assert c.static_verdict == DROP


# ---- 拼接/格式化：归显示侧但标 fragment ----

def test_format_site_keep_with_fragment():
    c = _classify({'a.rpy': ['label start:',
                             '    $ msg = "Score: %s" % points']},
                  '"Score: %s"')
    assert c.static_verdict == KEEP
    assert c.static_fragment is True
    assert decide_apply_path(c) == 'wrap'


def test_concat_fragment_keep():
    c = _classify({'a.rpy': ['label start:',
                             '    $ greeting = "Hello, " + player_name']},
                  '"Hello, "')
    assert c.static_verdict == KEEP
    assert c.static_fragment is True


# ---- 聚合规则 ----

def test_mixed_display_nondisplay_unknown_danger():
    c = _classify({'a.rpy': ['screen s:', '    text "Back"',
                             'label start:', '    if key == "Back":', '        pass']},
                  '"Back"')
    assert c.static_verdict == UNKNOWN
    assert c.static_danger is True
    assert '混合' in c.static_reason


def test_unclassified_site_unknown():
    c = _classify({'a.rpy': ['label start:',
                             '    $ foo("Mystery", 42)']},
                  '"Mystery"')
    assert c.static_verdict == UNKNOWN


def test_no_occurrence_unknown():
    c = _classify({'a.rpy': ['label start:', '    pass']}, '"Ghost"')
    assert c.static_verdict == UNKNOWN
    assert '未找到' in c.static_reason


def test_static_sites_recorded():
    c = _classify({'a.rpy': ['screen s:', '    text "Hi"',
                             'label l:', '    if x == "Hi":', '        pass']},
                  '"Hi"')
    kinds = {s['kind'] for s in c.static_sites}
    assert kinds == {'display', 'nondisplay'}
    assert all(s['site'].startswith('a.rpy:') for s in c.static_sites)


def test_static_sites_truncated(monkeypatch):
    from usage_rules import UsageAnalyzer as UA
    monkeypatch.setattr(UA, '_SITES_CAP', 1)
    c = _classify({'a.rpy': ['screen s:', '    text "Hi"', '    text "Hi"']},
                  '"Hi"')
    assert len(c.static_sites) == 1
    assert c.static_sites_truncated is True


# ---- 变量追踪：单跳（原有能力的回归） ----

def test_trace_single_assign_display():
    c = _classify({
        'a.rpy': ['default msg = "Hello"',
                  'screen s:', '    text msg'],
    }, '"Hello"')
    assert c.static_verdict == KEEP


def test_trace_multi_assign_unknown():
    c = _classify({
        'a.rpy': ['default msg = "Hello"',
                  'label start:', '    $ msg = "Bye"',
                  'screen s:', '    text msg'],
    }, '"Hello"')
    assert c.static_verdict == UNKNOWN


def test_trace_dollar_assign_display():
    """$ 单行赋值也要能追踪（label 内的常见形态）"""
    c = _classify({
        'a.rpy': ['label start:', '    $ msg = "Hello"',
                  'screen s:', '    text msg'],
    }, '"Hello"')
    assert c.static_verdict == KEEP


# ---- 变量追踪：多跳 BFS ----

def test_trace_reassign_chain_keep():
    c = _classify({
        'a.rpy': ['default a = "Hello"',
                  'label start:', '    $ b = a',
                  'screen s:', '    text b'],
    }, '"Hello"')
    assert c.static_verdict == KEEP


def test_trace_reassign_concat_fragment():
    c = _classify({
        'a.rpy': ['default a = "Hello"',
                  'label start:', '    $ b = a + "!"',
                  'screen s:', '    text b'],
    }, '"Hello"')
    assert c.static_verdict == KEEP
    assert c.static_fragment is True


def test_trace_container_loop_keep():
    c = _classify({
        'a.rpy': ['default t = "Hello"',
                  'label start:',
                  '    $ lst = [t]',
                  '    for x in lst:',
                  '        show screen s'],
        'b.rpy': ['screen s:', '    vbox:', '        text x'],
    }, '"Hello"')
    assert c.static_verdict == KEEP


def test_trace_function_param_keep():
    c = _classify({
        'a.rpy': ['init python:',
                  '    def show_msg(m):',
                  '        renpy.notify(m)'],
        'b.rpy': ['default t = "Hello"',
                  'label start:', '    $ show_msg(t)'],
    }, '"Hello"')
    assert c.static_verdict == KEEP


def test_trace_function_param_positional():
    """name 在第二参数位：按位置对齐形参"""
    c = _classify({
        'a.rpy': ['init python:',
                  '    def wrap(tag, m):',
                  '        text m'],
        'b.rpy': ['default t = "Hello"',
                  'label start:', '    $ wrap("h1", t)'],
    }, '"Hello"')
    assert c.static_verdict == KEEP


def test_trace_function_multi_def_unknown():
    c = _classify({
        'a.rpy': ['init python:', '    def show_msg(m):', '        renpy.notify(m)'],
        'b.rpy': ['init python:', '    def show_msg(m):', '        pass'],
        'c.rpy': ['default t = "Hello"',
                  'label start:', '    $ show_msg(t)'],
    }, '"Hello"')
    assert c.static_verdict == UNKNOWN


def test_trace_function_not_found_unknown():
    c = _classify({
        'a.rpy': ['default t = "Hello"',
                  'label start:', '    $ mystery(t)'],
    }, '"Hello"')
    assert c.static_verdict == UNKNOWN


def test_trace_depth_limit_unknown():
    """超过深度上限（>2 跳）的链一律 unknown，不猜"""
    c = _classify({
        'a.rpy': ['default a = "Hello"',
                  'label start:',
                  '    $ b = a',
                  '    $ c2 = b',
                  '    $ d = c2',
                  'screen s:', '    text d'],
    }, '"Hello"')
    assert c.static_verdict == UNKNOWN


def test_trace_to_nondisplay_drop():
    c = _classify({
        'a.rpy': ['default key = "Hello"',
                  'label start:', '    $ b = key',
                  '    if b == "Hello":', '        pass'],
    }, '"Hello"')
    # 赋值点 + 字面量比较点都是非显示/追踪非显示 → DROP
    assert c.static_verdict == DROP
    assert c.static_danger is True


# ---- trace_symbol_report ----

def test_trace_symbol_report():
    an = _analyzer({
        'a.rpy': ['default msg = "Hello"',
                  'label start:', '    $ renpy.notify(msg)',
                  '    if msg == "x":', '        pass'],
    })
    rep = an.trace_symbol_report('msg')
    assert rep['name'] == 'msg'
    assert len(rep['definitions']) == 1
    kinds = {r['kind'] for r in rep['references']}
    assert 'display' in kinds and 'nondisplay' in kinds
    assert rep['truncated'] is False


def test_trace_symbol_report_truncated():
    lines = ['default m = "x"'] + [f'    $ foo(m)' for _ in range(60)]
    an = _analyzer({'a.rpy': lines})
    rep = an.trace_symbol_report('m', cap=50)
    assert rep['truncated'] is True
    assert len(rep['references']) == 50
