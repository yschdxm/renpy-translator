# -*- coding: utf-8 -*-
"""_ren.py 模块显示字符串定向提取测试

三条规则：构造器首参、显示 kwarg、字符串函数返回。
非显示 kwarg/键名/路径不入候选。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from renpy_module_strings import find_module_string_candidates


def _mk_game(tmp_path, files: dict) -> Path:
    root = tmp_path / 'game'
    sub = root / 'game'
    sub.mkdir(parents=True)
    for rel, content in files.items():
        p = sub / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
    return root


def test_ctor_first_positional(tmp_path):
    root = _mk_game(tmp_path, {'goals_ren.py':
        'work_goal = Goal("Work-A-Day", "You learn something every day.",\n'
        '                 "general_work")\n'
        'trait = SerumTrait(name = "Synthetic Hair Bleach", tier = 0)\n'})
    texts = {c.text for c in find_module_string_candidates(str(root))}
    assert 'Work-A-Day' in texts
    assert 'You learn something every day.' in texts  # 第二参数=描述
    assert 'Synthetic Hair Bleach' in texts
    assert 'general_work' not in texts  # 第三参数（键名）不提


def test_display_kwargs(tmp_path):
    root = _mk_game(tmp_path, {'x_ren.py':
        'a = Action("Sleep for the night", req, "sleep_desc",\n'
        '           menu_tooltip = "Too early to sleep")\n'
        'b = Thing(category = "weapons", slot = 3)\n'})
    texts = {c.text for c in find_module_string_candidates(str(root))}
    assert 'Sleep for the night' in texts       # Action 首参
    assert 'sleep_desc' not in texts  # Action 第三位置参数不提（只取首参+次参）
    assert 'Too early to sleep' in texts          # menu_tooltip kwarg
    assert 'weapons' not in texts                 # 非显示 kwarg


def test_string_return_rule(tmp_path):
    root = _mk_game(tmp_path, {'conv_ren.py':
        'def get_obedience_string(amount):\n'
        '    if amount > 100:\n'
        '        return "Respectful"\n'
        '    return "Rebellious"\n'
        'def compute_damage(x):\n'
        '    return "not a string func result"\n'})
    texts = {c.text for c in find_module_string_candidates(str(root))}
    assert 'Respectful' in texts
    assert 'Rebellious' in texts
    assert 'not a string func result' not in texts  # 函数名不含 string/name/title


def test_noise_filtered(tmp_path):
    root = _mk_game(tmp_path, {'y_ren.py':
        'a = Room("gui/icons/room.png", x = 1)\n'
        'b = Clothing("white skin", "Breast region", 1)\n'})
    texts = {c.text for c in find_module_string_candidates(str(root))}
    assert 'gui/icons/room.png' not in texts   # 路径样过滤
    assert 'white skin' in texts


def test_raw_reconstruction_and_positions(tmp_path):
    root = _mk_game(tmp_path, {'z_ren.py':
        'x = Goal("Test Goal", "desc", "key")\n'})
    cands = find_module_string_candidates(str(root))
    c = next(c for c in cands if c.text == 'Test Goal')
    assert c.raw == '"Test Goal"'
    content = (root / 'game' / 'z_ren.py').read_text(encoding='utf-8')
    line = content.split('\n')[c.line - 1]
    assert line[c.col_start:c.col_end] == c.raw  # 坐标精确可定位


def test_dict_keys_and_values(tmp_path):
    """话题映射 dict 的键（话题名）与值（显示短语）都提取"""
    root = _mk_game(tmp_path, {'d_ren.py':
        'topic_phrases = {\n'
        '    "flirting": "flirty talk",\n'
        '    "skirts": "girls in skirts",\n'
        '}\n'})
    texts = {c.text for c in find_module_string_candidates(str(root))}
    assert 'flirting' in texts
    assert 'flirty talk' in texts
    assert 'girls in skirts' in texts


def test_return_string_list(tmp_path):
    """return [ ... ] 的字符串列表（init_list_of_opinions 的观点话题表）"""
    root = _mk_game(tmp_path, {'e_ren.py':
        'def init_list_of_opinions():\n'
        '    return [\n'
        '        "boots",\n'
        '        "flirting",\n'
        '        "classical music",\n'
        '    ]\n'})
    texts = {c.text for c in find_module_string_candidates(str(root))}
    assert 'boots' in texts
    assert 'flirting' in texts
    assert 'classical music' in texts
