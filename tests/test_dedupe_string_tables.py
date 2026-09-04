# -*- coding: utf-8 -*-
"""导出副本 strings 表去重清扫（dedupe_string_tables）测试

重复 old 是 Ren'Py 加载硬错误。三个来源：SDK 校验重新生成模板时提取
的 _()（wrap/原生/补丁）、同一文本多处原生 _()、zz 写入早于模板生成。
保留策略：非 zz 优先、有译文优先；译文回填不丢失。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from services.game_export import dedupe_string_tables


def _mk_export(tmp_path):
    tl = tmp_path / 'out' / 'game' / 'tl' / 'chinese'
    tl.mkdir(parents=True)
    return tmp_path / 'out', tl


def _strings_block(old, new, src='scripts/a.rpy', line=3):
    return (f'translate chinese strings:\n\n'
            f'    # {src}:{line}\n'
            f'    old "{old}"\n'
            f'    new "{new}"\n')


def test_dup_between_zz_and_template_fills_keeper(tmp_path):
    """zz 有译文、SDK 模板为空：保留模板条目并回填译文，zz 条目删除"""
    out, tl = _mk_export(tmp_path)
    (tl / 'zz_embedded.rpy').write_text(
        _strings_block('Load action mod: {}', '加载动作模组：{}'), encoding='utf-8')
    tmpl = tl / 'bugfix_additions'
    tmpl.mkdir()
    (tmpl / 'action_mod_core.rpy').write_text(
        _strings_block('Load action mod: {}', ''), encoding='utf-8')

    logs = []
    n = dedupe_string_tables(out, logs.append)
    assert n == 1

    zz = (tl / 'zz_embedded.rpy').read_text(encoding='utf-8')
    assert 'Load action mod' not in zz
    keeper = (tmpl / 'action_mod_core.rpy').read_text(encoding='utf-8')
    assert 'old "Load action mod: {}"' in keeper
    assert 'new "加载动作模组：{}"' in keeper
    assert any('重复 old' in l for l in logs)


def test_dup_between_two_templates(tmp_path):
    """同一文本在两个模板文件：保留一条（有译文优先），另一条删除"""
    out, tl = _mk_export(tmp_path)
    a = tl / 'a'
    b = tl / 'b'
    a.mkdir()
    b.mkdir()
    (a / 'x.rpy').write_text(_strings_block('Married', ''), encoding='utf-8')
    (b / 'y.rpy').write_text(_strings_block('Married', '已婚'),
                             encoding='utf-8')

    n = dedupe_string_tables(out, print)
    assert n == 1
    kept_a = (a / 'x.rpy').read_text(encoding='utf-8')
    kept_b = (b / 'y.rpy').read_text(encoding='utf-8')
    filled, empty = (kept_a, kept_b) if '已婚' in kept_a else (kept_b, kept_a)
    assert 'old "Married"' in filled and 'new "已婚"' in filled
    assert 'Married' not in empty


def test_no_dup_noop(tmp_path):
    out, tl = _mk_export(tmp_path)
    (tl / 'zz_embedded.rpy').write_text(
        _strings_block('Only in zz', '只在zz'), encoding='utf-8')
    tmpl = tl / 'scripts'
    tmpl.mkdir()
    content = _strings_block('Other text', '别的')
    (tmpl / 'a.rpy').write_text(content, encoding='utf-8')

    n = dedupe_string_tables(out, print)
    assert n == 0
    assert (tmpl / 'a.rpy').read_text(encoding='utf-8') == content


def test_dup_zz_filled_template_filled_keeps_template(tmp_path):
    """两边都有译文：保留非 zz 条目，zz 删除（不重复回填）"""
    out, tl = _mk_export(tmp_path)
    (tl / 'zz_embedded.rpy').write_text(
        _strings_block('Single', '单身甲'), encoding='utf-8')
    tmpl = tl / 'game_screens'
    tmpl.mkdir()
    (tmpl / 's.rpy').write_text(
        _strings_block('Single', '单身乙'), encoding='utf-8')

    n = dedupe_string_tables(out, print)
    assert n == 1
    assert 'Single' not in (tl / 'zz_embedded.rpy').read_text(encoding='utf-8')
    keeper = (tmpl / 's.rpy').read_text(encoding='utf-8')
    assert 'new "单身乙"' in keeper


def test_dialogue_blocks_untouched(tmp_path):
    """对话 translate 块（非 strings 块）里的同文本不受影响——
    去重只作用于 translate strings 块"""
    out, tl = _mk_export(tmp_path)
    (tl / 'zz_embedded.rpy').write_text(
        _strings_block('Wait.', '等等'), encoding='utf-8')
    tmpl = tl / 'scripts'
    tmpl.mkdir()
    (tmpl / 'a.rpy').write_text(
        'translate chinese start_1234:\n\n'
        '    # scripts/a.rpy:10\n'
        '    # mom "Wait."\n'
        '    mom "Wait."\n', encoding='utf-8')

    n = dedupe_string_tables(out, print)
    assert n == 0
    assert 'old "Wait."' in (tl / 'zz_embedded.rpy').read_text(
        encoding='utf-8')
