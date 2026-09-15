"""场景聚合器：把 label 级剧情图折叠为场景级故事图

粒度转换（纯静态、确定性）：
- junction（出度≠1 或 入度≠1 的 label，外加 start）之间是线性链，
  每条链折叠为一个"场景"（Scene）——两个分支/汇合点之间的一段连续剧情
- junction 处分支 ≥2（menu 或 if 条件）→ 生成一等公民的"选择"（Choice）节点：
  场景 → 选择 → 各分支场景，选择出边带选项文本
- 出度 0 的场景即结局；从 start BFS 不可达的 label（gallery/replay/
  系统 label 等）归入 unreachable，不进主图
- 同一对场景间的多条 label 级边合并（选项文本收集为列表）

输入输出都是纯数据（flow_parser.StoryNode/StoryEdge → Scene/SceneEdge），
不依赖 db/文件系统，测试友好。
"""

from collections import deque
from dataclasses import dataclass, field

from flow_parser import StoryEdge, StoryNode


@dataclass
class Scene:
    """场景节点：两个分支/汇合点之间的一段连续剧情（含收尾的分支 label）"""
    scene_id: str                 # 链首 label（稳定、确定性）
    labels: list = field(default_factory=list)   # 包含的 label
    speakers: list = field(default_factory=list)
    dialogue_count: int = 0
    translated_count: int = 0    # 已译台词数（构建管线回填）
    first_text: str = ''
    first_text_cn: str = ''
    thumb_file: str = ''
    is_entry: bool = False
    is_ending: bool = False
    is_return: bool = False       # 链尾 label 以 return 结束（被 call 的子流程返回点）
    thumb_candidates: list = field(default_factory=list)  # 候选缩略图文件
    file_path: str = ''           # 主文件（链首 label 所在文件）
    line_start: int = 0


@dataclass
class SceneEdge:
    source: str
    target: str
    texts: list = field(default_factory=list)     # 选项/条件文本（原文）
    texts_cn: list = field(default_factory=list)  # 对应译文（可为空串占位）
    branch: str = ''              # '' | menu | condition
    has_call: bool = False
    unresolved: bool = False      # 含动态跳转（target 为合成未决节点时）


# 动态跳转统一指向的合成节点（前端虚线展示）
UNRESOLVED_ID = '__unresolved__'


def build_scenes(nodes: list[StoryNode], edges: list[StoryEdge]) -> dict:
    """label 图 → 场景图

    返回 {'scenes': list[Scene], 'edges': list[SceneEdge],
           'unreachable': list[str]}
    """
    node_by = {n.label: n for n in nodes}

    # ---- 邻接表（动态跳转视为"指向未决"的出边，不参与链折叠）----
    out_edges: dict[str, list[StoryEdge]] = {}
    in_edges: dict[str, list[StoryEdge]] = {}
    for e in edges:
        if e.target is None:
            out_edges.setdefault(e.source, []).append(e)
            continue
        if e.target == e.source:
            continue  # 自环：不影响场景结构，丢弃
        if e.target not in node_by:
            continue  # 指向源码外 label（screen/引擎），丢弃
        out_edges.setdefault(e.source, []).append(e)
        in_edges.setdefault(e.target, []).append(e)

    # ---- 可达性：从 start BFS ----
    reachable = set()
    start_label = 'start' if 'start' in node_by else None
    if start_label:
        dq = deque([start_label])
        reachable.add(start_label)
        while dq:
            cur = dq.popleft()
            for e in out_edges.get(cur, []):
                if e.target and e.target not in reachable:
                    reachable.add(e.target)
                    dq.append(e.target)
    # 无 start 的游戏：全部视为可达（图本来就要全显）
    if start_label is None:
        reachable = set(node_by)
    unreachable = sorted(set(node_by) - reachable)

    def outdeg(lb: str) -> int:
        return len(out_edges.get(lb, []))

    def indeg(lb: str) -> int:
        return len(in_edges.get(lb, []))

    def has_call_in(lb: str) -> bool:
        return any(e.kind == 'call' for e in in_edges.get(lb, []))

    # ---- 链折叠：junction 之间的极大线性链 ----
    # junction = 分支点（出度>1）/汇合点（入度>1）/call 目标（子流程头）/start；
    # 结局点（出度 0）不是 junction——它是链的终点，并入所在链，
    # 否则每个结局都会碎成独立小场景。
    scenes: list[Scene] = []
    label_to_scene: dict[str, str] = {}

    def is_junction(lb: str) -> bool:
        return (lb == start_label or outdeg(lb) > 1 or indeg(lb) > 1
                or has_call_in(lb))

    def make_scene(chain: list[str]) -> Scene:
        first = node_by[chain[0]]
        sc = Scene(scene_id=chain[0], labels=list(chain),
                   file_path=first.file, line_start=first.line_start,
                   is_entry=(chain[0] == start_label))
        for lb in chain:
            n = node_by[lb]
            label_to_scene[lb] = sc.scene_id
            sc.dialogue_count += n.dialogue_count
            for sp in n.speakers:
                if sp not in sc.speakers:
                    sc.speakers.append(sp)
            if not sc.first_text and n.first_text:
                sc.first_text = n.first_text
                sc.first_text_cn = n.first_text_cn
            if not sc.thumb_file and n.thumb_file:
                sc.thumb_file = n.thumb_file
            for t in n.thumb_files:
                if t not in sc.thumb_candidates:
                    sc.thumb_candidates.append(t)
            if n.has_return:
                sc.is_return = True
        return sc

    def walk_chain(head: str) -> list[str]:
        chain = [head]
        cur = head
        while True:
            nxt_edges = out_edges.get(cur, [])
            if len(nxt_edges) != 1:
                break
            e0 = nxt_edges[0]
            # call 不折叠：子流程是独立场景（可能多处调用、有 return 结构）
            if e0.kind == 'call':
                break
            nxt = e0.target
            if nxt is None or nxt in label_to_scene:
                break
            if is_junction(nxt):
                # 简单分支点（出度>1 且入度=1 且非 call 目标）作为链的终点
                # 并入链内——场景以 menu 收尾，选项直接挂为场景出边；
                # 汇合点/call 目标则是边界，自成场景
                if outdeg(nxt) > 1 and indeg(nxt) == 1 \
                        and not has_call_in(nxt):
                    chain.append(nxt)
                break
            chain.append(nxt)
            label_to_scene[nxt] = head  # 占位防环（make_scene 会正式登记）
            cur = nxt
        for x in chain:
            label_to_scene.pop(x, None)
        return chain

    heads = []
    for lb in sorted(reachable):
        if is_junction(lb):
            heads.append(lb)
    for lb in sorted(reachable):
        if is_junction(lb):
            for e in out_edges.get(lb, []):
                if e.target and not is_junction(e.target):
                    heads.append(e.target)

    # 链首处理顺序 = 距 start 的 BFS 距离：上游链先把收尾分支点并进
    # 链尾，分支点就不会再碎成独立单 label 场景
    bfs_order = {start_label: 0} if start_label else {}
    if start_label:
        dq = deque([start_label])
        while dq:
            cur = dq.popleft()
            for e in out_edges.get(cur, []):
                if e.target and e.target not in bfs_order:
                    bfs_order[e.target] = bfs_order[cur] + 1
                    dq.append(e.target)
    heads.sort(key=lambda h: (bfs_order.get(h, len(bfs_order)), h))

    for head in heads:
        if head in label_to_scene:
            continue
        chain = walk_chain(head)
        if chain and chain[0] not in label_to_scene:
            scenes.append(make_scene(chain))

    # 纯循环兜底（环上全是 in/out=1，无 junction 引路）：任选一点破环成链
    for lb in sorted(reachable):
        if lb not in label_to_scene:
            scenes.append(make_scene(walk_chain(lb)))

    # ---- 场景级边聚合 ----
    # 分支点已作为链尾并入场景（walk_chain 的终点规则），
    # 选项文本直接挂在场景出边上——选择点以带标注的分支出边呈现，
    # 不另设 choice 节点（menu 密集型游戏里独立 choice 节点会让节点数翻倍）
    edge_map: dict[tuple, SceneEdge] = {}

    def add_edge(src: str, tgt: str, e: StoryEdge):
        key = (src, tgt)
        se = edge_map.get(key)
        if se is None:
            se = edge_map[key] = SceneEdge(source=src, target=tgt)
        if e.branch:
            se.branch = e.branch
            if e.text:
                se.texts.append(e.text)
                se.texts_cn.append(e.text_cn)
        if e.kind == 'call':
            se.has_call = True

    for e in edges:
        if e.target is None:
            # 动态跳转：场景 → 未决合成节点
            src_scene = label_to_scene.get(e.source)
            if src_scene:
                key = (src_scene, UNRESOLVED_ID)
                se = edge_map.get(key)
                if se is None:
                    se = edge_map[key] = SceneEdge(
                        source=src_scene, target=UNRESOLVED_ID,
                        unresolved=True)
                if e.expr:
                    se.texts.append(e.expr)
                    se.texts_cn.append('')
            continue
        src_scene = label_to_scene.get(e.source)
        tgt_scene = label_to_scene.get(e.target)
        if not src_scene or not tgt_scene:
            continue
        if src_scene == tgt_scene:
            continue
        add_edge(src_scene, tgt_scene, e)

    # ---- call 回流边：被调用子流程的终结场景（含 return 且出度 0）指向
    # 调用方场景。Ren'Py 的 call 会返回调用点继续执行——不建这条边，
    # 所有以 return 收尾的被调用场景都会被误判为结局（数百个假结局）。
    # 共享子流程会被多处调用：语义上返回任一调用方都成立，图上只保留
    # 一条（BFS 序最靠前的调用方），否则边数爆炸（沙盒游戏 14 万条） ----
    scene_out: dict[str, list[str]] = {}
    for se in edge_map.values():
        scene_out.setdefault(se.source, []).append(se.target)
    has_return_label = {s.scene_id: any(
        node_by[lb].has_return for lb in s.labels if lb in node_by)
        for s in scenes}

    call_edges = [e for e in edges if e.kind == 'call' and e.target]
    # 调用方按 BFS 距离排序：回流边优先挂在最上游的调用方
    call_edges.sort(key=lambda e: (bfs_order.get(e.source, 1 << 30),
                                   e.source, e.target))
    assigned: set = set()
    for e in call_edges:
        src_scene = label_to_scene.get(e.source)
        tgt_scene = label_to_scene.get(e.target)
        if not src_scene or not tgt_scene or src_scene == tgt_scene:
            continue
        # 前向走 callee 链，找终结场景（出度 0 且含 return label）
        seen = set()
        stack = [tgt_scene]
        while stack and len(seen) < 3000:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            nxt = scene_out.get(cur, [])
            if (not nxt and has_return_label.get(cur)
                    and cur != src_scene and cur not in assigned):
                assigned.add(cur)
                key = (cur, src_scene)
                if key not in edge_map:
                    edge_map[key] = SceneEdge(source=cur,
                                              target=src_scene,
                                              has_call=True)
            stack.extend(t for t in nxt if t not in seen)

    # ---- 结局标记：场景出度为 0 且非子流程返回点 ----
    # （入口场景无出边时同时是结局：直线到底的短故事，不与 entry 互斥）
    out_scenes = {se.source for se in edge_map.values()}
    for s in scenes:
        s.is_ending = s.scene_id not in out_scenes and not s.is_return

    return {'scenes': scenes, 'edges': list(edge_map.values()),
            'unreachable': unreachable}
