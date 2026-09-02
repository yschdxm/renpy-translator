"""译文标记一致性校验测试

背景（生产事故）：AI 把 [tribe_name] 译成 [部落名]、丢 {w}/{i} 标签，
提示词的"保留原样"是软约束，全链路无代码级检查，坏译文直达游戏。
markup_check 把约束变硬校验，接入三处：批次翻译（进重试）、单句翻译
（带纠正重译）、导出闸门（保留英文 + 拦截清单）。
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from markup_check import check_newline, check_pair, extract_interps, extract_tags
from translator import AITranslator, TranslationConfig
from services.game_export import GameExporter


@pytest.fixture
def translator():
    # api_key 非空才会建 OpenAI client（纯构造，不发请求）
    return AITranslator(TranslationConfig(api_key='offline-test-key'))


# ---- 校验器单元 ----

def test_pass_when_translation_preserves_markup():
    assert check_pair('Hello [name], {i}please{/i}!',
                      '你好 [name]，{i}请{/i}！') == []


def test_rewritten_interp_rejected():
    """改写插值表达式（生产事故原型：[tribe_name] → [部落名]）"""
    probs = check_pair('Honor to [tribe_name].', '向[部落名]致敬。')
    assert any('[部落名]' in p and '没有的插值' in p for p in probs)


def test_lost_interp_rejected():
    """丢失玩家名插值 → 动态内容不再显示"""
    probs = check_pair('Prince [player_name]!', '王子殿下！')
    assert any('丢失了原文的插值' in p and '[player_name]' in p for p in probs)


def test_new_tag_rejected_and_lost_tag_rejected():
    probs = check_pair('plain text', '{size=-5}加了标签{/size}')
    assert any('没有的文本标签' in p for p in probs)
    probs = check_pair('Wait {w}here', '等{w}一下')  # 保留 {w} 通过
    assert probs == []
    probs = check_pair('I...{w}It is fine.', '我……没事。')
    assert any('丢失了原文的文本标签' in p for p in probs)


def test_unbalanced_bracket_rejected():
    probs = check_pair('tell [h] there', '告诉[h 那里')
    assert any('未闭合' in p for p in probs)
    probs = check_pair('a {i} b', '甲 { 乙')
    assert any('未闭合' in p for p in probs)


def test_unbalanced_original_exempt():
    """原文本身不平衡是源头的错误（英文版同样会炸），译文保持原样不拦"""
    assert check_pair('tell [h there', '告诉[h 那里') == []
    assert check_pair('a { b', '甲 { 乙') == []


def test_lone_close_bracket_is_literal():
    """[[b] 显示为 [b]：孤立的 ] 是合法字面字符，不误报"""
    assert check_pair('a [[b] tail', '甲 [[乙] 尾') == []


def test_escaped_brackets_not_markup():
    """[[ 与 {{ 是字面转义，不参与插值/标签比较"""
    assert check_pair('a [[b] and {{c}', '甲 [[乙] 与 {{丙}') == []


def test_comment_tag_ignored():
    """{#...} 注释标签渲染时丢弃，两端有无不影响判定"""
    assert check_pair('Save{#b}', '保存') == []


def test_whitespace_normalized():
    assert extract_interps('[ player_name ]') == {'player_name'}


def test_conversion_suffix_preserved():
    """!q 等转换后缀属于表达式的一部分，改动视为改写"""
    assert check_pair('[x!q]', '[x!q]') == []
    assert any('没有的插值' in p for p in check_pair('[x]', '[x!q]'))


def test_nested_interp_and_format_placeholder_legal():
    """嵌套方括号插值 [len(x[i])] 与 Python format 占位符 {} 是合法语法
    （生产库真实误报回归：BrothelSlop 'Queue: [len(custom_playlists[i])]'、
    renpy/common 'While unpacking {}, unknown type {}.'）"""
    assert check_pair('Queue: [len(custom_playlists[i])]',
                      '队列：[len(custom_playlists[i])]') == []
    assert check_pair('Page {}', '第 {} 页') == []
    assert check_pair('While unpacking {}, unknown type {}.',
                      '解包 {} 时出现未知类型 {}。') == []
    assert check_pair('slot {0:>3}', '槽位 {0:>3}') == []


def test_nested_outer_rewrite_detected():
    """嵌套插值的外层改写也要拦：栈式提取取最外层完整表达式，
    只取内层的做法会把 len( → size( 这类改写漏检"""
    assert extract_interps('Queue: [len(x[i])]') == {'len(x[i])'}
    probs = check_pair('Queue: [len(x[i])]', '队列：[size(x[i])]')
    assert any('没有的插值' in p and '[size(x[i])]' in p for p in probs)
    assert any('丢失了原文的插值' in p and '[len(x[i])]' in p for p in probs)


# ---- 译文真实换行检查 ----

def test_raw_newline_rejected():
    """原文是字面 \\n（两字符），译文却展开成真实换行——违规
    （官方文档：Ren'Py 字符串不支持跨行，换行必须写 \\n 转义）"""
    probs = check_newline('Join them\\nNext line.',
                          '加入他们\n下一行。')
    assert len(probs) == 1 and '换行' in probs[0] and '\\n' in probs[0]


def test_literal_backslash_n_passes():
    """译文按原文形式写字面 \\n → 通过"""
    assert check_newline('Join them\\nNext.', '加入他们\\n下一行。') == []


def test_raw_newline_in_original_exempt():
    """原文本身含真实换行（多行模板/_p 块），译文同形不算违规"""
    assert check_newline('第一行\n第二行', 'one\ntwo') == []


def test_no_newline_passes():
    assert check_newline('plain', '普通') == []


# ---- 批次翻译接入：违规进存疑并带纠正指令重试 ----

def _fake_message(translations):
    args = json.dumps({'translations': translations, 'terms': []},
                      ensure_ascii=False)
    fn = SimpleNamespace(arguments=args)
    return SimpleNamespace(tool_calls=[SimpleNamespace(function=fn)])


def test_parse_marks_bad_markup_suspicious():
    items = [{'original_text': 'Honor to [tribe_name].'}]
    msg = _fake_message([{'id': 1, 'translation': '向[部落名]致敬。'}])
    placed, _, suspicious, _ = AITranslator._parse_tool_response(msg, items)
    assert placed == {}
    assert '标记校验未通过' in suspicious[0]


def test_batch_retry_receives_correction_hint(translator, monkeypatch):
    """第一轮译文破坏插值 → 存疑并重试，第二轮 prompt 带纠正指令；
    修正后正常合并"""
    prompts = []

    def fake_call_api(messages, temperature, max_tokens, tools=None,
                      tool_choice=None, return_message=False, task_type=''):
        prompts.append(messages[1]['content'])
        if len(prompts) == 1:
            return _fake_message([{'id': 1, 'translation': '向[部落名]致敬。'}])
        return _fake_message([{'id': 1, 'translation': '向[tribe_name]致敬。'}])

    monkeypatch.setattr(translator, '_call_api', fake_call_api)
    merged, _, fail_reasons, _rej = translator.translate_batch(
        [{'original_text': 'Honor to [tribe_name].'}],
        content_type='dialogue')

    assert len(prompts) == 2
    assert '标记校验未通过' in prompts[1] and '[部落名]' in prompts[1]
    assert merged == {0: '向[tribe_name]致敬。'}
    assert fail_reasons == {}


def test_batch_gives_up_keeps_reason(translator, monkeypatch):
    """重试耗尽仍破坏插值：条目不进结果，原因可读"""

    def always_bad(messages, temperature, max_tokens, tools=None,
                   tool_choice=None, return_message=False, task_type=''):
        return _fake_message([{'id': 1, 'translation': '向[部落名]致敬。'}])

    monkeypatch.setattr(translator, '_call_api', always_bad)
    merged, _, fail_reasons, _rej = translator.translate_batch(
        [{'original_text': 'Honor to [tribe_name].'}],
        content_type='dialogue')
    assert merged == {}
    assert '标记校验未通过' in fail_reasons[0]


# ---- 单句翻译接入：带纠正指令重译一次 ----

def test_single_translate_retries_with_correction(translator, monkeypatch):
    prompts = []

    def fake_call_api(messages, temperature, max_tokens, task_type=''):
        prompts.append(messages[1]['content'])
        if len(prompts) == 1:
            return '向[部落名]致敬。'
        return '向[tribe_name]致敬。'

    monkeypatch.setattr(translator, '_call_api', fake_call_api)
    translated, _ = translator.translate_text('Honor to [tribe_name].')
    assert translated == '向[tribe_name]致敬。'
    assert '未通过校验' in prompts[1]


def test_single_translate_keeps_better_of_two(translator, monkeypatch):
    """重译仍失败时保留原版（导出闸门兜底）"""

    def always_bad(messages, temperature, max_tokens, task_type=''):
        return '向[部落名]致敬。'

    monkeypatch.setattr(translator, '_call_api', always_bad)
    translated, _ = translator.translate_text('Honor to [tribe_name].')
    assert translated == '向[部落名]致敬。'


# ---- 导出闸门：违规译文保留英文并出清单 ----

TL_CONTENT = '''translate chinese strings:

    old "Honor to [tribe_name]."
    new ""

translate chinese strings:

    old "Plain sentence."
    new ""
'''

SAY_CONTENT = '''translate chinese abc_123:

    # e "Honor to [tribe_name]."
    e "Honor to [tribe_name]."

    # e "Plain sentence."
    e "Plain sentence."
'''


@pytest.fixture
def exporter(tmp_path):
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    tl = tmp_path / 'tl'
    tl.mkdir()
    (tl / 's.rpy').write_text(TL_CONTENT, encoding='utf-8')
    (tl / 'd.rpy').write_text(SAY_CONTENT, encoding='utf-8')
    return ex, tl


def test_fill_strings_blocks_bad_translation(exporter):
    ex, tl = exporter
    mapping = {'Honor to [tribe_name].': '向[部落名]致敬。',
               'Plain sentence.': '普通句子。'}
    filled = ex._fill_strings(tl, mapping)
    out = (tl / 's.rpy').read_text(encoding='utf-8')
    assert filled == 1                      # 好译文正常填
    assert 'new ""' in out                  # 坏译文保留英文（空模板）
    assert '向[部落名]' not in out
    assert len(ex._blocked) == 1
    assert '[部落名]' in ex._blocked[0][1]


def test_fill_dialogue_blocks_bad_translation(exporter):
    ex, tl = exporter
    mapping = {'Honor to [tribe_name].': '向[部落名]致敬。',
               'Plain sentence.': '普通句子。'}
    filled = ex._fill_dialogue(tl, mapping)
    out = (tl / 'd.rpy').read_text(encoding='utf-8')
    assert filled == 1
    # 坏译文不填：重放行保留英文原文
    assert 'e "Honor to [tribe_name]."' in out
    assert '向[部落名]' not in out
    assert len(ex._blocked) == 1


def test_fill_passes_good_translations(exporter):
    ex, tl = exporter
    mapping = {'Honor to [tribe_name].': '向[tribe_name]致敬。',
               'Plain sentence.': '普通句子。'}
    filled = ex._fill_strings(tl, mapping)
    assert filled == 2
    assert ex._blocked == []
    out = (tl / 's.rpy').read_text(encoding='utf-8')
    assert '向[tribe_name]致敬。' in out


# ---- 导出预检：库内坏译文前置清单 ----

def test_scan_markup_issues(tmp_path):
    """scan_markup_issues 扫出库内破坏插值/标签的已译条目（与导出闸门同一套
    校验），未翻译与好译文不计"""
    from database import ProjectDatabase
    from services.game_export import scan_markup_issues

    db = ProjectDatabase(str(tmp_path / 'p.db'))
    db.connect()
    try:
        db.insert_dialogues([
            {'file_path': 'a.rpy', 'line_number': 1, 'label': 's',
             'character': 'e', 'original_text': 'Honor to [tribe_name].'},
            {'file_path': 'a.rpy', 'line_number': 2, 'label': 's',
             'character': 'e', 'original_text': 'Plain sentence.'},
            {'file_path': 'a.rpy', 'line_number': 3, 'label': 's',
             'character': 'e', 'original_text': 'Not translated.'},
        ])
        db.insert_ui_texts([
            {'file_path': 'b.rpy', 'line_number': 1, 'label': '',
             'original_text': 'Page [n]'},
        ])
        rows = {r['original_text']: r for r in db.get_all_dialogues()}
        db.update_dialogue(rows['Honor to [tribe_name].']['id'],
                           '向[部落名]致敬。')          # 坏：编造插值
        db.update_dialogue(rows['Plain sentence.']['id'], '普通句子。')  # 好
        ui = db.get_all_ui_texts()[0]
        db.update_ui_text(ui['id'], '第 [n] 页')        # 好
        # 未译出的 'Not translated.' 不计

        issues = scan_markup_issues(db)
        assert issues['count'] == 1
        assert issues['samples'][0]['kind'] == 'dialogue'
        assert '[部落名]' in issues['samples'][0]['reason']

        # 修好后清零
        db.update_dialogue(rows['Honor to [tribe_name].']['id'],
                           '向[tribe_name]致敬。')
        assert scan_markup_issues(db)['count'] == 0
    finally:
        db.close()
