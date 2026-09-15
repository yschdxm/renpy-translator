# -*- coding: utf-8 -*-
"""值通道翻译（!t）测试：add_tflag、_apply_ttag_literals、_sweep_ttag

核心契约：Ren'Py 替换式"先译模板、后插值"——[x] 的值原样插入，
[x!t] 的值先过 strings 表。模板（字面量/zz old/ui_texts）三处必须
同步补 !t，任一处缺失即查不中。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from database import ProjectDatabase
from embedded_strings import Candidate
from markup_check import add_tflag
from services.game_export import GameExporter


# ---- add_tflag ----

def test_tflag_basic():
    assert add_tflag('[x]') == '[x!t]'
    assert add_tflag('a [x] b [y.z]') == 'a [x!t] b [y.z!t]'


def test_tflag_nested():
    assert add_tflag('[d[_return][0]]') == '[d[_return][0]!t]'
    assert add_tflag('[stat[0]]: [stat[2]]') == '[stat[0]!t]: [stat[2]!t]'


def test_tflag_existing_flags():
    assert add_tflag('[item.title!i]') == '[item.title!it]'
    assert add_tflag('[x!q]') == '[x!qt]'


def test_tflag_idempotent():
    assert add_tflag('[x!t]') == '[x!t]'
    assert add_tflag('[x!tq]') == '[x!tq]'
    assert add_tflag('[item.title!it]') == '[item.title!it]'


def test_tflag_no_interp_and_escapes():
    assert add_tflag('plain text') == 'plain text'
    assert add_tflag('[[literal') == '[[literal'


def test_tflag_skips_format_spec():
    """带格式规格的插值整体跳过：!t 字符串化后 format(str, '.1f') 必炸
    （生产事故：player_status_hud 的 [corruption:.1f!t] 启动即崩）"""
    assert add_tflag('[corruption:.1f]') == '[corruption:.1f]'
    assert add_tflag('[price:,.2f] 元') == '[price:,.2f] 元'
    assert add_tflag('[x!i:.2f]') == '[x!i:.2f]'  # 旗标+规格 → 跳
    assert add_tflag('[d[a:b]]') == '[d[a:b]!t]'  # 切片冒号在括号内，不是规格


def test_tflag_skips_braced_content():
    """[{0}] 是"括号包格式占位"的字面文本（.format 后才轮到它），
    不是 Ren'Py 插值——加 !t 会把显示值当变量 eval（[染色!t] → NameError）"""
    assert add_tflag('[{0}]') == '[{0}]'
    assert add_tflag('{{color=#FFFF00}}[{0}]{{/color}}') == '{{color=#FFFF00}}[{0}]{{/color}}'
    assert add_tflag('[{name}]') == '[{name}]'


def test_tflag_whitespace_preserved():
    assert add_tflag('[ x ]') == '[ x!t ]'


# ---- _apply_ttag_literals ----

def _mk_export(tmp_path, src_line: str, raw: str, template: str):
    out = tmp_path / 'out'
    sub = out / 'game' / 'game' / 'scripts'
    sub.mkdir(parents=True)
    content = 'screen s:\n    ' + src_line + '\n'
    (sub / 'x.rpy').write_text(content, encoding='utf-8')
    full_line = '    ' + src_line
    col = full_line.index(raw)
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    c = Candidate(file=str(sub / 'x.rpy'), rel_file='scripts/x.rpy',
                  line=2, col_start=col, col_end=col + len(raw),
                  raw=raw, text=template, kind='screen', hint='界面',
                  confidence='high')
    rid = db.merge_embedded_candidates([c])[0]['id']
    db.update_embedded_ai(rid, 1, '界面', False, 'x.rpy:2', 'table', 'coarse')
    db.set_embedded_status([rid], 'marked')
    return out, sub, db, rid


def _exporter(db):
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    ex.db = db
    return ex


def test_apply_ttag_hit(tmp_path):
    out, sub, db, rid = _mk_export(
        tmp_path, 'text "[love_label]: [love_info]"',
        '"[love_label]: [love_info]"', '[love_label!t]: [love_info!t]')
    logs = []
    _exporter(db)._apply_ttag_literals(out, logs.append)
    text = (sub / 'x.rpy').read_text(encoding='utf-8')
    assert '"[love_label!t]: [love_info!t]"' in text
    assert any('!t' in l for l in logs)
    db.close()


def test_apply_ttag_skips_rows_without_interps(tmp_path):
    out, sub, db, rid = _mk_export(tmp_path, 'text "Plain label"',
                                   '"Plain label"', 'Plain label')
    logs = []
    _exporter(db)._apply_ttag_literals(out, logs.append)
    assert '"Plain label"' in (sub / 'x.rpy').read_text(encoding='utf-8')
    assert not any('!t 改写定位失败' in l for l in logs)
    db.close()


def test_apply_ttag_drift_relocates(tmp_path):
    out, sub, db, rid = _mk_export(
        tmp_path, 'text "[a]: [b]"', '"[a]: [b]"', '[a!t]: [b!t]')
    db.update_embedded_position(rid, 9, 0)  # 漂移
    logs = []
    _exporter(db)._apply_ttag_literals(out, logs.append)
    text = (sub / 'x.rpy').read_text(encoding='utf-8')
    assert '"[a!t]: [b!t]"' in text
    db.close()


def test_apply_ttag_missing_warns(tmp_path):
    out, sub, db, rid = _mk_export(
        tmp_path, 'text "[a]: [b]"', '"[a]: [b]"', '[a!t]: [b!t]')
    (sub / 'x.rpy').write_text('screen s:\n    pass\n', encoding='utf-8')
    logs = []
    _exporter(db)._apply_ttag_literals(out, logs.append)
    assert any('定位失败' in l for l in logs)
    db.close()


# ---- _sweep_ttag ----

def _mk_sweep_export(tmp_path):
    out = tmp_path / 'out'
    sub = out / 'game' / 'game'
    sub.mkdir(parents=True)
    return out, sub


def test_sweep_prop_lines_and_action_lists(tmp_path):
    out, sub = _mk_sweep_export(tmp_path)
    (sub / 'menu.rpy').write_text(
        'screen main_choice_display(menu_items):\n'
        '    textbutton "[item.title!i]":\n'
        '        action [Function(item.hide_person), Return(item.return_value)]\n'
        '    text "[title_element]" xalign 0.5\n'
        '    text "[already!t]"\n',
        encoding='utf-8')
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    logs = []
    ex._sweep_ttag(out, logs.append)
    text = (sub / 'menu.rpy').read_text(encoding='utf-8')
    # 屏幕属性行的插值补 !t（含已有旗标的追加）
    assert '"[item.title!it]"' in text
    assert '"[title_element!t]"' in text
    # 已有 !t 不重复
    assert '"[already!t]"' in text and '!tt' not in text
    # 动作列表（非字符串字面量）绝不动
    assert '[Function(item.hide_person), Return(item.return_value)]' in text
    db = None


def test_sweep_wrapped_literal_and_tl_old(tmp_path):
    out, sub = _mk_sweep_export(tmp_path)
    (sub / 'a.rpy').write_text(
        'screen s:\n    text _("Review [show_list] now")\n',
        encoding='utf-8')
    tl = out / 'game' / 'tl' / 'chinese'   # 导出副本布局：export/game/tl
    tl.mkdir(parents=True)
    (tl / 'a.rpy').write_text(
        'translate chinese strings:\n\n'
        '    old "Review [show_list] now"\n'
        '    new "查看全部"\n',
        encoding='utf-8')
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    ex._sweep_ttag(out, print)
    assert '"Review [show_list!t] now"' in (sub / 'a.rpy').read_text(
        encoding='utf-8')
    tl_text = (tl / 'a.rpy').read_text(encoding='utf-8')
    assert 'old "Review [show_list!t] now"' in tl_text
    assert 'new "查看全部"' in tl_text  # new 行不动（填充时重建）


def test_sweep_skips_non_prop_lines(tmp_path):
    out, sub = _mk_sweep_export(tmp_path)
    original = ('label start:\n'
                '    python:\n'
                '        x = compute([a], [b])\n')
    (sub / 'b.rpy').write_text(original, encoding='utf-8')
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    ex._sweep_ttag(out, print)
    # 非属性行非 _() 的列表字面量不受影响
    assert (sub / 'b.rpy').read_text(encoding='utf-8') == original


def test_sweep_skips_method_named_tooltip(tmp_path):
    """tooltip.append(...) 是方法调用不是屏幕属性行——\btooltip\b
    曾误中并把 f-string 插值里的下标 ss[0] 改成 ss[0!t]（非法语法）"""
    out, sub = _mk_sweep_export(tmp_path)
    original = (
        'python:\n'
        '    tooltip.append(f"{get_coloured_arrow(ss[0])} '
        '{person_info_ui_format_hearts(ss[0])} - {ss[1]}\\n")\n')
    (sub / 'c.rpy').write_text(original, encoding='utf-8')
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    ex._sweep_ttag(out, print)
    assert (sub / 'c.rpy').read_text(encoding='utf-8') == original


def test_sweep_skips_fstring_on_prop_line(tmp_path):
    """屏幕属性行上的 f-string 由 _transform_fstrings 处理——
    清扫绝不把 f-string 插值里的下标当 Ren'Py 插值"""
    out, sub = _mk_sweep_export(tmp_path)
    original = 'screen s:\n    text f"Value: {row[0]} done"\n'
    (sub / 'd.rpy').write_text(original, encoding='utf-8')
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    ex._sweep_ttag(out, print)
    assert (sub / 'd.rpy').read_text(encoding='utf-8') == original


def test_sweep_tflags_fstring_without_python_interp(tmp_path):
    """不含 { 的 f-string（只有 Ren'Py 插值，如 f"[a]: [b]"）可以
    安全补 !t——没有 Python 下标风险"""
    out, sub = _mk_sweep_export(tmp_path)
    (sub / 'e.rpy').write_text(
        'screen s:\n    textbutton f"[obedience_label]: [obedience_info]":\n'
        '        action Return(1)\n',
        encoding='utf-8')
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    ex._sweep_ttag(out, print)
    text = (sub / 'e.rpy').read_text(encoding='utf-8')
    assert 'f"[obedience_label!t]: [obedience_info!t]"' in text


def test_sweep_does_not_touch_double_underscore(tmp_path):
    """__() 是 f-string 变换已写好的立即翻译输出——清扫再补 !t 会把
    [{0}] 改成 [{0}!t] 二次破坏（生产事故：trait 标签变 NameError）"""
    out, sub = _mk_sweep_export(tmp_path)
    original = ('python:\n'
                '    trait_tags.append(__("{{color=#FFFF00}}[{0}]{{/color}}")'
                '.format(rt_translate(a_tag)))\n')
    (sub / 'f.rpy').write_text(original, encoding='utf-8')
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    ex._sweep_ttag(out, print)
    assert (sub / 'f.rpy').read_text(encoding='utf-8') == original


# ---- (tooltip) 拆分派生 ----

def test_tooltip_title_entries(tmp_path):
    """含 ' (tooltip)' 的菜单 caption 派生标题/tooltip 两条部分条目：
    译文同步拆分、% 转义（Ren'Py 菜单 caption 会被 % 格式化，
    裸 % 抛 ValueError）、已有 old 不重复"""
    out, sub = _mk_sweep_export(tmp_path)
    tl = sub / 'tl' / 'chinese'
    tl.mkdir(parents=True)
    (tl / 'zz_embedded.rpy').write_text(
        'translate chinese strings:\n\n'
        '    old "Existing entry"\n'
        '    new "已有"\n',
        encoding='utf-8')
    ex = object.__new__(GameExporter)
    # 入库形态：\n 为字面两字符
    td = {
        'Wait here {image=time_advance}\\n{menu_yellow}10% Extra '
        '{image=energy_token_small}{menu_yellow} (tooltip)Kill time.':
        '在这里等候 {image=time_advance}\\n{menu_yellow}额外10% '
        '{image=energy_token_small}{menu_yellow} (tooltip)消磨时间。',
        'Existing entry (tooltip)x': '已有 (tooltip) x',  # 标题部分已存在 → 跳过
    }
    n = ex._add_tooltip_title_entries(tl, td)
    # title+tooltip 各一 + 第二条目的 tooltip 部分（标题部分已存在跳过）
    assert n == 3
    zz = (tl / 'zz_embedded.rpy').read_text(encoding='utf-8')
    assert 'old "Wait here {image=time_advance}\\n{menu_yellow}10% Extra' in zz
    # 不做 % 转义：拆分条目只经自定义菜单的 textbutton 显示（Text 直显
    # 不走菜单选项的 % 格式化），%% 会原样显示
    assert '额外10%' in zz and '额外10%%' not in zz
    assert 'old "Kill time."' in zz
    assert 'new "消磨时间。"' in zz
    assert zz.count('old "Existing entry"') == 1  # 标题部分不重复
    assert 'old "x"' in zz  # 第二条目的 tooltip 部分是新条目


def test_tooltip_entries_skip_translation_without_marker(tmp_path):
    """译文丢了 (tooltip) 标记（闸门会拦）→ 不派生"""
    out, sub = _mk_sweep_export(tmp_path)
    tl = sub / 'tl' / 'chinese'
    tl.mkdir(parents=True)
    ex = object.__new__(GameExporter)
    td = {'Full caption (tooltip) tip text': '没有标记的译文'}
    n = ex._add_tooltip_title_entries(tl, td)
    assert n == 0
