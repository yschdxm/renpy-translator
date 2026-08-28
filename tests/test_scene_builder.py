"""scene_builder 场景聚合测试：label 图 → 场景图"""

from flow_parser import StoryEdge, StoryNode
from scene_builder import UNRESOLVED_ID, build_scenes


def _node(label, speakers=(), dlg=1, first='', ret=False, thumb=''):
    return StoryNode(label=label, file='script.rpy', line_start=1,
                     speakers=list(speakers), dialogue_count=dlg,
                     first_text=first, has_return=ret, thumb_file=thumb,
                     is_entry=(label == 'start'))


def _edge(source, target, kind='jump', branch='', text='', expr=''):
    return StoryEdge(source=source, target=target, kind=kind,
                     branch=branch, text=text, expr=expr)


def _scenes(result):
    return {s.scene_id: s for s in result['scenes']}


def test_linear_chain_folds_into_one_scene():
    # start → a → b → c（全部 in/out=1）→ 单个场景
    nodes = [_node('start'), _node('a'), _node('b'), _node('c')]
    edges = [_edge('start', 'a'), _edge('a', 'b'), _edge('b', 'c')]
    r = build_scenes(nodes, edges)
    scenes = _scenes(r)
    assert len(scenes) == 1
    sc = scenes['start']
    assert sc.labels == ['start', 'a', 'b', 'c']
    assert sc.dialogue_count == 4
    assert sc.is_entry
    assert sc.is_ending  # 出度 0 的链尾即结局
    assert not r['edges']


def test_menu_branch_edges_carry_option_text():
    # start → m（menu 两分支）→ x / y：分支点 m 并入前一场景，
    # 选项文本直接挂在场景出边上（不设独立 choice 节点）
    nodes = [_node('start'), _node('m'), _node('x'), _node('y')]
    edges = [
        _edge('start', 'm'),
        _edge('m', 'x', branch='menu', text='选项一'),
        _edge('m', 'y', branch='menu', text='选项二'),
    ]
    r = build_scenes(nodes, edges)
    scenes = _scenes(r)
    assert 'choice_m' not in scenes
    # m 是分支点，并入 start 链尾
    assert scenes['start'].labels == ['start', 'm']
    by_target = {(e.source, e.target): e for e in r['edges']}
    assert by_target[('start', 'x')].texts == ['选项一']
    assert by_target[('start', 'y')].texts == ['选项二']
    assert by_target[('start', 'x')].branch == 'menu'


def test_merge_point_continues_as_single_chain():
    # start → m →(选) x/y → end（x、y 都指向 end）
    nodes = [_node('start'), _node('m'), _node('x'), _node('y'), _node('end')]
    edges = [
        _edge('start', 'm'),
        _edge('m', 'x', branch='menu', text='甲'),
        _edge('m', 'y', branch='menu', text='乙'),
        _edge('x', 'end'), _edge('y', 'end'),
    ]
    r = build_scenes(nodes, edges)
    scenes = _scenes(r)
    # end 入度 2 是 junction，自成场景
    assert scenes['end'].labels == ['end']
    assert scenes['end'].is_ending
    targets = {(e.source, e.target) for e in r['edges']}
    assert ('x', 'end') in targets and ('y', 'end') in targets


def test_unreachable_labels_isolated():
    nodes = [_node('start'), _node('a'), _node('gallery_replay')]
    edges = [_edge('start', 'a')]
    r = build_scenes(nodes, edges)
    assert r['unreachable'] == ['gallery_replay']
    assert 'gallery_replay' not in _scenes(r)


def test_dynamic_jump_to_unresolved_node():
    nodes = [_node('start'), _node('a')]
    edges = [_edge('start', 'a'), _edge('a', None, expr='dest_var')]
    r = build_scenes(nodes, edges)
    unres = [e for e in r['edges'] if e.target == UNRESOLVED_ID]
    assert len(unres) == 1
    assert unres[0].unresolved
    assert 'dest_var' in unres[0].texts
    # 有出边（指向未决），不算结局
    assert not _scenes(r)['start'].is_ending


def test_return_scene_not_ending():
    # start → call → sub（return 结束的子流程不算结局）
    nodes = [_node('start'), _node('sub', ret=True)]
    edges = [_edge('start', 'sub', kind='call')]
    r = build_scenes(nodes, edges)
    assert _scenes(r)['sub'].is_return
    assert not _scenes(r)['sub'].is_ending


def test_scene_aggregation_fields():
    # 链上聚合：speakers 去重有序、首个缩略图、首条台词
    nodes = [
        _node('start', speakers=['a'], dlg=2, first='第一句', thumb='t1.webp'),
        _node('b', speakers=['b', 'a'], dlg=3, first='第二句', thumb='t2.webp'),
    ]
    edges = [_edge('start', 'b')]
    r = build_scenes(nodes, edges)
    sc = _scenes(r)['start']
    assert sc.speakers == ['a', 'b']
    assert sc.dialogue_count == 5
    assert sc.first_text == '第一句'
    assert sc.thumb_file == 't1.webp'


def test_self_loop_and_duplicate_edges_ignored():
    nodes = [_node('start'), _node('a')]
    edges = [_edge('start', 'a'), _edge('start', 'a'), _edge('a', 'a')]
    r = build_scenes(nodes, edges)
    assert len(r['edges']) == 1


def test_call_edge_keeps_chain_connected():
    # call 边参与链折叠（子流程目标可达）
    nodes = [_node('start'), _node('sub', ret=True)]
    edges = [_edge('start', 'sub', kind='call')]
    r = build_scenes(nodes, edges)
    assert not r['unreachable']


def test_pure_cycle_breaks_into_chain():
    # a → b → a（无 start 引路的环，全部 in/out=1）
    nodes = [_node('start'), _node('a'), _node('b')]
    edges = [_edge('start', 'a'), _edge('a', 'b'), _edge('b', 'a')]
    r = build_scenes(nodes, edges)
    scenes = _scenes(r)
    # start 出度1、入度0 是 junction；a 入度2 是 junction（start→a, b→a）
    # a 自成链首，b 跟在其后
    assert scenes['a'].labels == ['a', 'b']
