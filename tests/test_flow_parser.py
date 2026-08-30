"""flow_parser 剧情控制流解析测试（临时 .rpy fixture）"""

import pytest

from flow_parser import FlowParser


SCRIPT = '''
define e = Character("Eileen")

label start:
    scene bg room
    show eileen happy at left
    e "Hello!"
    "Narration."
    jump day1

label day1:
    scene bg school
    e "Class."
    menu:
        "Go home":
            jump home
        "Stay":
            if score > 5:
                jump library
            else:
                call study
    e "After menu."
    jump end

label home:
    e "Home."
    return

label study:
    e "Studying."
    return

label library:
    $ renpy.jump("end")

label end:
    e "Bye."

label orphan:
    e "Nobody jumps here."

label dynamic:
    jump expression dest_var
'''


@pytest.fixture
def game_dir(tmp_path):
    (tmp_path / 'script.rpy').write_text(SCRIPT, encoding='utf-8')
    return tmp_path


@pytest.fixture
def parsed(game_dir):
    return FlowParser(str(game_dir)).parse()


def _nodes(parsed):
    return {n.label: n for n in parsed['nodes']}


def _edges(parsed, source):
    return [e for e in parsed['edges'] if e.source == source]


def test_all_labels_found(parsed):
    assert set(_nodes(parsed)) == {
        'start', 'day1', 'home', 'study', 'library', 'end', 'orphan',
        'dynamic'}


def test_entry_and_dialogue(parsed):
    start = _nodes(parsed)['start']
    assert start.is_entry
    assert start.dialogue_count == 2
    assert start.speakers == ['e']
    assert start.first_text == 'Hello!'
    assert start.first_dlg_line > 0
# ---- say 变体台词解析 ----

SAY_VARIANTS = '''
label start:
    e happy "Hello."
    e @ vhappy "Wow!"
    e -annoyed "Hmm."
    mc.name "It's me."
    the_person.title "Fancy."
    style "some_style"
    text "Not a say"
    e """Inline monologue."""
    e """
Multi
line
    """
    x = """
python string
    """
'''


@pytest.fixture
def say_parsed(tmp_path):
    (tmp_path / 's.rpy').write_text(SAY_VARIANTS, encoding='utf-8')
    return FlowParser(str(tmp_path)).parse()


def test_say_with_image_attributes(say_parsed):
    """e happy "..." / e @ vhappy / e -annoyed 都计入说话人台词"""
    n = say_parsed['nodes'][0]
    assert 'e' in n.speakers
    # 3 条属性 say + 带点说话人 2 条 + monologue 2 条
    assert n.dialogue_count == 7
    assert n.first_text == 'Hello.'


def test_dotted_speaker(say_parsed):
    """mc.name 等带点说话人计入 speakers"""
    n = say_parsed['nodes'][0]
    assert 'mc.name' in n.speakers
    assert 'the_person.title' in n.speakers


def test_keywords_not_speakers(say_parsed):
    """screen 属性关键字（style/text）不会被当成说话人"""
    n = say_parsed['nodes'][0]
    assert 'style' not in n.speakers
    assert 'text' not in n.speakers


def test_inline_monologue(say_parsed):
    n = say_parsed['nodes'][0]
    assert n.first_dlg_line > 0
    assert 'Inline monologue.' not in n.first_text or n.first_text == 'Hello.'


def test_multiline_monologue_span(say_parsed):
    """跨行三引号块整体计 1 条，定位到闭合行；python 三引号串不计"""
    n = say_parsed['nodes'][0]
    assert n.dialogue_count == 7
    # 闭合行（含结尾 """ 的行）应被记为末条台词行
    lines = SAY_VARIANTS.split('\n')
    close_line = next(i for i, l in enumerate(lines, 1)
                      if l.strip() == '"""')
    assert n.last_dlg_line == close_line

def test_plain_jump(parsed):
    edges = _edges(parsed, 'start')
    assert len(edges) == 1
    assert edges[0].target == 'day1'
    assert edges[0].kind == 'jump'
    assert edges[0].branch == ''


def test_menu_edges_with_option_text(parsed):
    edges = [e for e in _edges(parsed, 'day1') if e.branch == 'menu']
    by_text = {}
    for e in edges:
        by_text.setdefault(e.text, set()).add(e.target)
    assert by_text['Go home'] == {'home'}
    assert by_text['Stay'] == {'library', 'study'}  # jump + call 都挂在选项上


def test_condition_inside_option_becomes_menu_branch(parsed):
    # menu 选项优先于 if 条件：选项文本作为分支标注
    edges = [e for e in _edges(parsed, 'day1') if e.branch == 'menu']
    assert all(e.text in ('Go home', 'Stay') for e in edges)


def test_call_edge_and_return(parsed):
    calls = [e for e in _edges(parsed, 'day1') if e.kind == 'call']
    assert len(calls) == 1
    assert calls[0].target == 'study'
    assert _nodes(parsed)['study'].has_return


def test_renpy_py_jump(parsed):
    edges = _edges(parsed, 'library')
    assert len(edges) == 1
    assert edges[0].target == 'end'
    assert edges[0].kind == 'jump'


def test_dynamic_jump_unresolved(parsed):
    edges = _edges(parsed, 'dynamic')
    assert len(edges) == 1
    assert edges[0].target is None
    assert edges[0].expr == 'dest_var'


def test_terminal_detection(parsed):
    nodes = _nodes(parsed)
    # fall-through 语义：end 结尾无 jump/return → 隐式流入同文件下一个
    # label（orphan），orphan 同样流入 dynamic；只有 dynamic 无后继
    assert not nodes['end'].is_terminal
    assert not nodes['orphan'].is_terminal
    assert nodes['dynamic'].is_terminal
    assert not nodes['start'].is_terminal


def test_fallthrough_edges_added(parsed):
    # 隐式边：普通语句结尾的 label 流入同文件下一个 label
    implicit = {(e.source, e.target) for e in parsed['edges'] if e.implicit}
    assert ('end', 'orphan') in implicit
    assert ('orphan', 'dynamic') in implicit
    # jump/return 结尾不补隐式边
    assert ('start', 'day1') not in implicit
    assert ('home', 'study') not in implicit


def test_menu_all_jump_no_fallthrough(parsed):
    # day1 的 menu 每个选项块都以 jump/call 收尾，但以 jump end 结尾，
    # 所以 day1 无隐式边；其 menu 选项边正常存在
    implicit = [e for e in parsed['edges']
                if e.implicit and e.source == 'day1']
    assert not implicit


def test_scene_ops_recorded(parsed):
    start = _nodes(parsed)['start']
    ops = [(op.op, op.images, op.at) for op in start.scene_ops]
    assert ops[0] == ('scene', ['bg', 'room'], [])
    assert ops[1] == ('show', ['eileen', 'happy'], ['left'])


def test_node_line_ranges(parsed):
    nodes = _nodes(parsed)
    assert nodes['start'].line_start < nodes['day1'].line_start
    assert nodes['start'].line_end < nodes['day1'].line_start


def test_label_with_params(tmp_path):
    (tmp_path / 'a.rpy').write_text(
        'label play_video(path, duration=0):\n'
        '    "playing"\n'
        '    return\n',
        encoding='utf-8')
    r = FlowParser(str(tmp_path)).parse()
    assert [n.label for n in r['nodes']] == ['play_video']


def test_code_keywords_not_speakers(tmp_path):
    (tmp_path / 'a.rpy').write_text(
        'label x:\n'
        '    text "button label"\n'
        '    e "real dialogue"\n'
        '    jump x\n',
        encoding='utf-8')
    r = FlowParser(str(tmp_path)).parse()
    node = r['nodes'][0]
    assert node.speakers == ['e']
