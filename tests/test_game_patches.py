# -*- coding: utf-8 -*-
"""游戏补丁注册表测试：锚点命中应用 / 文件缺失 / 锚点不匹配跳过

补丁语义铁律：英文（未翻译）环境下必须是恒等改写——
锚点存在时替换，替换后英文游戏行为与原文件完全一致。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from services import game_patches as gp


def _mk_tree(tmp_path):
    """导出副本布局：out/game/game/<rel_file>"""
    root = tmp_path / 'out' / 'game' / 'game'
    root.mkdir(parents=True)
    return tmp_path / 'out'


def test_patch_applied_on_anchor_hit(tmp_path):
    out = _mk_tree(tmp_path)
    patch = gp.PATCHES[0]
    f = out / 'game' / 'game' / patch.rel_file
    f.parent.mkdir(parents=True)
    f.write_text(
        'def show_climax_menu(self):\n'
        '    for climax_option in self.climax_options:\n'
        '        for inner in [1]:\n'
        + patch.anchor
        + '            climax_type = climax_option[1]\n',
        encoding='utf-8')
    logs = []
    gp.apply_game_patches(out, logs.append)
    text = f.read_text(encoding='utf-8')
    assert '_(climax_option[0])' in text
    assert any('已应用游戏补丁' in l for l in logs)


def test_patch_skips_when_file_missing(tmp_path):
    out = _mk_tree(tmp_path)
    logs = []
    gp.apply_game_patches(out, logs.append)
    assert not any('已应用' in l for l in logs)
    assert any('文件不存在' in l for l in logs)


def test_patch_skips_when_anchor_mismatch(tmp_path):
    out = _mk_tree(tmp_path)
    patch = gp.PATCHES[0]
    f = out / 'game' / 'game' / patch.rel_file
    f.parent.mkdir(parents=True)
    original = patch.anchor.replace('climax_option[0]', 'renamed_var[0]')
    f.write_text(original, encoding='utf-8')
    logs = []
    gp.apply_game_patches(out, logs.append)
    assert f.read_text(encoding='utf-8') == original
    assert any('锚点不匹配' in l for l in logs)


def test_patch_registry_entries_have_distinct_targets(tmp_path):
    """注册表自洽：无重复 (rel_file, anchor)——重复会双写"""
    keys = [(p.rel_file, p.anchor) for p in gp.PATCHES]
    assert len(keys) == len(set(keys))
    # 每个补丁的替换必须不同于锚点（否则是空操作）
    for p in gp.PATCHES:
        assert p.anchor != p.replacement
