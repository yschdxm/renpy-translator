# -*- coding: utf-8 -*-
"""f-string 模板化（fstring_strings）测试

核心契约：
- 形态判定：def 内/!r/spec/裸方括号 → B（_("{0}...").format(...)），
  其余 → A（_("[expr]...")）
- 模板构造：字面花括号双写 + {i}/{i!r}/{i:spec}
- 导出改写：位置校验/漂移重定位/DB 模板一致性校验；替换后源码语法成立
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from database import ProjectDatabase
from embedded_strings import Candidate
from fstring_strings import (build_replacement, find_fstring_candidates,
                             parse_fstring, transform_fstring_literals)
from services.game_export import GameExporter


# ---- parse_fstring：形态判定 ----

def test_form_b_store_scope():
    """单一急切形态：store 作用域同样 B——赋值后流转的场景
    （值被嵌进别的文本）惰性模板会把括号原样漏出"""
    fmt = parse_fstring('f"Text {person.title} here"', False)
    assert fmt.form == 'B'
    assert fmt.template == 'Text {0} here'
    assert fmt.args == ['person.title']


def test_form_b_in_def():
    fmt = parse_fstring('f"{name}: +2 love"', True)
    assert fmt.form == 'B'
    assert fmt.template == '{0}: +2 love'
    assert fmt.args == ['name']


def test_form_b_conversion_and_spec():
    assert parse_fstring('f"{x!r} repr"', False).form == 'B'
    fmt = parse_fstring('f"{amount:+} obedience"', False)
    assert fmt.form == 'B'
    assert fmt.template == '{0:+} obedience'
    assert fmt.args == ['amount']


def test_form_b_brackets_in_literal():
    fmt = parse_fstring('f"[{x}]"', False)
    assert fmt.form == 'B'
    assert fmt.template == '[{0}]'


def test_skip_no_interpolation():
    assert parse_fstring('f"plain text"', False) is None


def test_skip_nested_format_spec():
    assert parse_fstring('f"{x:{w}} nested"', False) is None


def test_skip_syntax_error():
    assert parse_fstring('f"{unclosed"', False) is None


# ---- 模板构造细节 ----

def test_form_b_undoubles_then_redoubles_braces():
    """f-string 的 {{ 与 .format 转义一致：ast 单反后重新双写——
    渲染时 {image=...} 仍被解释为图片标签"""
    fmt = parse_fstring(
        'f"{person.obedience} {{image=triskelion_token_small}} left"', False)
    assert fmt.form == 'B'
    assert fmt.template == ('{0} {{image=triskelion_token_small}} left')


def test_form_b_keeps_doubled_braces():
    fmt = parse_fstring('f"{{tag}} value {x}"', True)
    assert fmt.form == 'B'
    assert fmt.template == '{{tag}} value {0}'


def test_form_b_multiple_args_order():
    fmt = parse_fstring('f"Received {n} dose of {name}"', True)
    assert fmt.template == 'Received {0} dose of {1}'
    assert fmt.args == ['n', 'name']


# ---- build_replacement ----

def test_replacement_store_scope_same_as_def():
    """形态 B 与作用域无关：实参包 rt_translate() 安全现译（非字符串原样返回）"""
    fmt = parse_fstring('f"Text {person.title} here"', False)
    assert (build_replacement(fmt, '"')
            == '__("Text {0} here").format(rt_translate(person.title))')


def test_replacement_form_b():
    fmt = parse_fstring('f"Received {n} dose of {name}"', True)
    assert (build_replacement(fmt, '"')
            == '__("Received {0} dose of {1}").format(rt_translate(n), rt_translate(name))')


def test_replacement_escapes_quote_and_backslash():
    fmt = parse_fstring("f'it\\'s {name} \\\\ path'", True)
    out = build_replacement(fmt, "'")
    # 引号按原引号字符转义、反斜杠双写
    assert "it\\'s" in out
    assert '\\\\' in out
    # 输出本身是合法 python 表达式
    ast.parse(out, mode='eval')


# ---- 扫描器 ----

def _mk_game(tmp_path, files: dict) -> Path:
    root = tmp_path / 'game'
    sub = root / 'game'
    sub.mkdir(parents=True)
    for rel, content in files.items():
        p = sub / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
    return root


def test_scan_python_block_and_dollar(tmp_path):
    root = _mk_game(tmp_path, {'a.rpy':
        'label start:\n'
        '    python:\n'
        '        msg = f"Hello {user.name} today"\n'
        '    $ mc.log_event(f"{the_person.display_name} leveled up")\n'})
    cands = find_fstring_candidates(str(root))
    texts = {c.text for c in cands}
    # label 顶层 / $ 行同样形态 B（急切求值，位置无关）
    assert 'Hello {0} today' in texts
    assert '{0} leveled up' in texts


def test_scan_def_body_is_form_b(tmp_path):
    root = _mk_game(tmp_path, {'a.rpy':
        'init python:\n'
        '    def helper(person):\n'
        '        return f"{person.name} works hard"\n'
        '    x = f"{global_name} works hard"\n'})
    cands = find_fstring_candidates(str(root))
    # def 内外同形态（急切求值，作用域无关）；两条候选都在
    assert len(cands) == 2
    assert all(c.text == '{0} works hard' for c in cands)


def test_scan_screen_prop_line(tmp_path):
    root = _mk_game(tmp_path, {'s.rpy':
        'screen hud():\n'
        '    text f"Score: {score.points} now"\n'})
    cands = find_fstring_candidates(str(root))
    assert len(cands) == 1
    assert cands[0].text == 'Score: {0} now'


def test_scan_skips_rf_prefix_and_comments(tmp_path):
    root = _mk_game(tmp_path, {'a.rpy':
        'python:\n'
        '    x = rf"raw {not_ours}"\n'
        '    # f"comment {nope}"\n'
        '    y = f"Real {value} string"\n'})
    cands = find_fstring_candidates(str(root))
    assert len(cands) == 1
    assert cands[0].text == 'Real {0} string'


def test_scan_skips_pure_interpolation_and_paths(tmp_path):
    root = _mk_game(tmp_path, {'a.rpy':
        'python:\n'
        '    a = f"{x}"\n'
        '    b = f"{d}/icon.png"\n'
        '    c = f"Good {thing} here"\n'})
    cands = find_fstring_candidates(str(root))
    assert len(cands) == 1
    assert cands[0].text == 'Good {0} here'


def test_scan_skips_wrapped_and_concat(tmp_path):
    root = _mk_game(tmp_path, {'a.rpy':
        'python:\n'
        '    a = _(f"wrapped {x} text")\n'
        '    b = "pre" f"concat {y} text"\n'
        '    c = f"Free {z} text"\n'})
    cands = find_fstring_candidates(str(root))
    texts = {c.text for c in cands}
    assert 'Free {0} text' in texts
    assert len(cands) == 1


def test_scan_includes_renpy_interp_literal(tmp_path):
    """字面片段含 Ren'Py 插值的 f-string 必须入候选——!t 值通道需要
    （f"[energy_label]: {...}" 的静态字母全在插值里，剥插值会漏掉）"""
    root = _mk_game(tmp_path, {'a.rpy':
        'screen hud():\n'
        '    textbutton f"[energy_label]: {get_energy_string(p.energy)}":\n'
        '        action Return(1)\n'})
    cands = find_fstring_candidates(str(root))
    assert len(cands) == 1
    assert cands[0].text == '[energy_label!t]: {0}'


def test_scan_includes_multi_arg_minimal_static(tmp_path):
    """静态内容只有空格/标签的多参数字符串也入候选——参数现译通道
    （f"{trait.name} {tags}" 静态仅空格，但 trait.name 不变换永远英文）"""
    root = _mk_game(tmp_path, {'a.rpy':
        'init python:\n'
        '    def title(trait):\n'
        '        return f"{trait.name} {get_trait_tags(trait)}"\n'})
    cands = find_fstring_candidates(str(root))
    assert len(cands) == 1
    assert cands[0].text == '{0} {1}'


# ---- 导出改写 ----

def _mk_export(tmp_path, src_line: str, raw: str, template: str,
               in_def: bool = False):
    out = tmp_path / 'out'
    sub = out / 'game' / 'game' / 'scripts'
    sub.mkdir(parents=True)
    if in_def:
        content = ('label start:\n    python:\n'
                   '        def helper():\n'
                   '            ' + src_line + '\n')
        line_idx = 4
        full_line = '            ' + src_line
    else:
        content = ('label start:\n    python:\n        '
                   + src_line + '\n')
        line_idx = 3
        full_line = '        ' + src_line
    (sub / 'x.rpy').write_text(content, encoding='utf-8')
    col = full_line.index(raw)
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    c = Candidate(file=str(sub / 'x.rpy'), rel_file='scripts/x.rpy',
                  line=line_idx, col_start=col, col_end=col + len(raw),
                  raw=raw, text=template, kind='fstring',
                  hint='f-string模板·形态B', confidence='low')
    rid = db.merge_embedded_candidates([c])[0]['id']
    db.update_embedded_ai(rid, 1, 'f-string', False, 'x.rpy:3', 'fstring',
                          'refine')
    db.set_embedded_status([rid], 'marked')
    return out, sub, db, rid


def test_transform_form_b_end_to_end(tmp_path):
    raw = 'f"Received {n} dose of {name}"'
    out, sub, db, rid = _mk_export(
        tmp_path, 'msg = ' + raw, raw, 'Received {0} dose of {1}',
        in_def=True)
    done, moved, lost = transform_fstring_literals(
        db.get_marked_embedded(apply_path='fstring'),
        str(out / 'game' / 'game'))
    assert done == 1 and moved == [] and lost == []
    text = (sub / 'x.rpy').read_text(encoding='utf-8')
    assert '__("Received {0} dose of {1}").format(rt_translate(n), rt_translate(name))' in text
    # 改写后的赋值行本身是合法 python（rpy 整体不是纯 python，只验该行）
    line = [l for l in text.split('\n') if 'msg =' in l][0]
    ast.parse(line.strip(), mode='exec')
    db.close()


def test_transform_store_scope_end_to_end(tmp_path):
    raw = 'f"Text {person.title} here"'
    out, sub, db, rid = _mk_export(
        tmp_path, 'msg = ' + raw, raw, 'Text {0} here')
    done, moved, lost = transform_fstring_literals(
        db.get_marked_embedded(apply_path='fstring'),
        str(out / 'game' / 'game'))
    assert done == 1
    text = (sub / 'x.rpy').read_text(encoding='utf-8')
    assert '__("Text {0} here").format(rt_translate(person.title))' in text
    db.close()


def test_transform_template_mismatch_skips(tmp_path):
    """DB 模板与 raw 现派生不一致（源码/口径已变）→ 跳过不改写"""
    raw = 'f"Received {n} dose"'
    out, sub, db, rid = _mk_export(tmp_path, 'msg = ' + raw, raw,
                                   'STALE TEMPLATE')
    done, moved, lost = transform_fstring_literals(
        db.get_marked_embedded(apply_path='fstring'),
        str(out / 'game' / 'game'))
    assert done == 0 and lost == [rid]
    assert raw in (sub / 'x.rpy').read_text(encoding='utf-8')  # 原样保留
    db.close()


def test_transform_line_drift_relocates(tmp_path):
    raw = 'f"Text {person.title} here"'
    out, sub, db, rid = _mk_export(tmp_path, 'msg = ' + raw, raw,
                                   'Text {0} here')
    db.update_embedded_position(rid, 9, 0)  # 记录坐标漂移
    done, moved, lost = transform_fstring_literals(
        db.get_marked_embedded(apply_path='fstring'),
        str(out / 'game' / 'game'))
    assert done == 1 and moved != [] and lost == []
    db.close()


def test_transform_missing_literal(tmp_path):
    raw = 'f"Text {person.title} here"'
    out, sub, db, rid = _mk_export(tmp_path, 'msg = ' + raw, raw,
                                   'Text {0} here')
    (sub / 'x.rpy').write_text('label start:\n    pass\n', encoding='utf-8')
    done, moved, lost = transform_fstring_literals(
        db.get_marked_embedded(apply_path='fstring'),
        str(out / 'game' / 'game'))
    assert done == 0 and lost == [rid]
    db.close()


# ---- GameExporter 接线 ----

def test_exporter_transform_step(tmp_path):
    raw = 'f"Received {n} dose of {name}"'
    out, sub, db, rid = _mk_export(tmp_path, 'msg = ' + raw, raw,
                                   'Received {0} dose of {1}', in_def=True)
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    ex.db = db
    logs = []
    ex._transform_fstrings(out, logs.append)
    text = (sub / 'x.rpy').read_text(encoding='utf-8')
    assert '.format(rt_translate(n), rt_translate(name))' in text
    assert any('已改写' in l for l in logs)
    db.close()


def test_exporter_transform_no_rows(tmp_path):
    out = tmp_path / 'out'
    (out / 'game' / 'game').mkdir(parents=True)
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    ex.db = db
    logs = []
    ex._transform_fstrings(out, logs.append)
    assert logs == []
    db.close()


def test_unescape_u_escape():
    """Unicode/十六进制转义必须还原——否则文本带反斜杠被路径过滤误杀"""
    from embedded_strings import _unescape
    assert _unescape('Vandenberg\u00A0Ltd. Lobby') == 'Vandenberg\xa0Ltd. Lobby'


def test_scan_includes_text_tag_only_static(tmp_path):
    """静态内容全是文本标签的 f-string（彩色标签）也入候选——
    参数是显示值（get_label_tag 的 {color}Sluttiness{/color} 漏译事故）"""
    root = _mk_game(tmp_path, {'a.rpy':
        'init python:\n'
        '    def get_label_tag(label_name):\n'
        '        return f"{{color=#d0d010}}{label_name}{{/color}}"\n'})
    cands = find_fstring_candidates(str(root))
    assert len(cands) == 1
    assert cands[0].text == '{{color=#d0d010}}{0}{{/color}}'


def test_scan_screen_action_list_fstring(tmp_path):
    """screen 块内动作列表的 f-string 也扫（SetScreenVariable 的
    f"{loc.formal_name} is closed"）；label 里的 menu 选项不扫
    （翻译 id 哈希敏感区）"""
    root = _mk_game(tmp_path, {'a.rpy':
        'screen map_screen():\n'
        '    textbutton "x":\n'
        '        hovered SetScreenVariable("info", f"{loc.formal_name} is closed")\n'
        'label start:\n'
        '    menu:\n'
        '        f"{x} menu option":\n'
        '            pass\n'})
    cands = find_fstring_candidates(str(root))
    texts = {c.text for c in cands}
    assert '{0} is closed' in texts
    assert not any('menu option' in t for t in texts)
