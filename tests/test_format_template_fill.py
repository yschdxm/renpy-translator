# -*- coding: utf-8 -*-
""".format 模板 % 转义与 (disabled) 逻辑标记保留测试

- {1:+.0%} 的 % 是格式规格：escape_translation 双写会把 .format 炸掉
  （percent='none' 路径）
- ' (disabled)' 是 MenuItem 的敏感判定标记：译文换了中文括注也必须
  恢复英文标记，否则禁用按钮变可点击（生产事故）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from services.game_export import GameExporter


def _fill(ex, tl, mapping):
    ex._blocked = []
    ex._cancel_event = None
    return ex._fill_strings(tl, mapping)


def _mk(tl_path):
    tl_path.write_text(
        'translate chinese strings:\n\n'
        '    old "{0}: {1:+.0%} Max Energy"\n'
        '    new ""\n'
        '    old "{0}\\n{{menu_red=16}}{1}{{/menu_red}} (disabled)"\n'
        '    new ""\n'
        '    old "Plain UI string 100%"\n'
        '    new ""\n',
        encoding='utf-8')


def test_format_template_percent_not_escaped(tmp_path):
    tl = tmp_path / 'tl'
    tl.mkdir()
    _mk(tl / 's.rpy')
    ex = object.__new__(GameExporter)
    filled = _fill(ex, tl, {
        '{0}: {1:+.0%} Max Energy': '{0}：{1:+.0%} 精力上限',
        '{0}\\n{{menu_red=16}}{1}{{/menu_red}} (disabled)':
            '{0}\\n{{menu_red=16}}{1}{{/menu_red}}（已禁用）',
        'Plain UI string 100%': '纯界面文本 100%',
    })
    out = (tl / 's.rpy').read_text(encoding='utf-8')
    # .format 模板：% 保持单写（格式规格可解析）
    assert 'new "{0}：{1:+.0%} 精力上限"' in out
    # ' (disabled)' 逻辑标记恢复英文（禁用按钮不会变可点击）
    assert '（已禁用）' not in out
    assert ' (disabled)"' in out
    # 普通 UI 字符串照旧 %% 转义
    assert 'new "纯界面文本 100%%"' in out
