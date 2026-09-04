# -*- coding: utf-8 -*-
"""旧版前缀字符串坏包装（f_("...") 等）的导出前还原

旧扫描器把 f/r/b/u 前缀字面量的引号位置当候选起点，apply_wrapping
插入 _() 后产生 f_("...")——语法上是调用函数 f_，编译校验拦不住，
运行时 NameError。导出前的安全网要把它们机械还原为原始前缀字面量。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from services.game_export import GameExporter  # noqa: E402


def _repair(game_root: Path) -> list:
    logs = []
    exporter = GameExporter.__new__(GameExporter)
    exporter._repair_legacy_prefix_wraps(game_root, logs.append)
    return logs


def test_repairs_f_string_wrap(tmp_path):
    f = tmp_path / 'person_info_ui.rpy'
    f.write_text(
        'python:\n'
        '    obedience_info = f_("{person.obedience} points")\n'
        '    plain = _("Keep me")\n',
        encoding='utf-8')
    _repair(tmp_path)
    text = f.read_text(encoding='utf-8')
    assert 'f"{person.obedience} points"' in text
    assert 'f_(' not in text
    # 正常的 _() 包装不动
    assert '_("Keep me")' in text


def test_repairs_all_prefixes_and_quotes(tmp_path):
    f = tmp_path / 'x.rpy'
    f.write_text(
        "$ a = f_(\"double\") + r_('single') + b_(\"bytes\") + u_('uni')\n",
        encoding='utf-8')
    _repair(tmp_path)
    text = f.read_text(encoding='utf-8')
    assert 'f"double"' in text
    assert "r'single'" in text
    assert 'b"bytes"' in text
    assert "u'uni'" in text


def test_escaped_quotes_inside_literal(tmp_path):
    f = tmp_path / 'y.rpy'
    f.write_text(
        "$ a = f_(\"she said \\\"hi\\\" to {name}\")\n",
        encoding='utf-8')
    _repair(tmp_path)
    text = f.read_text(encoding='utf-8')
    assert 'f"she said \\"hi\\" to {name}"' in text
    assert 'f_(' not in text


def test_skips_tl_dir(tmp_path):
    tl = tmp_path / 'game' / 'tl' / 'chinese'
    tl.mkdir(parents=True)
    f = tl / 'z.rpy'
    f.write_text('    old "f_("x")"\n', encoding='utf-8')
    _repair(tmp_path)
    assert f.read_text(encoding='utf-8') == '    old "f_("x")"\n'


def test_multiline_leftover_warns_not_repairs(tmp_path):
    f = tmp_path / 'm.rpy'
    f.write_text(
        "$ a = f_(\"\"\"line1\nline2\"\"\")\n",
        encoding='utf-8')
    logs = _repair(tmp_path)
    # 三引号跨行不在自动还原范围：保持原样并告警
    assert 'f_(' in f.read_text(encoding='utf-8')
    assert any('警告' in line for line in logs)


def test_log_reports_count(tmp_path):
    f = tmp_path / 'c.rpy'
    f.write_text("$ a = f_(\"x\")\n$ b = f_(\"y\")\n", encoding='utf-8')
    logs = _repair(tmp_path)
    assert any('2 处' in line for line in logs)
