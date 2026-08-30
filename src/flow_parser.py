"""剧情控制流解析器：label/jump/call/menu/if 静态分析，产出剧情图的节点与边

Ren'Py 的叙事不靠文件顺序，靠 label + jump/call/menu 互相链接，天然是有向图。
本解析器逐 label 跟踪缩进上下文（menu 选项 / if-elif-else 条件），把显式跳转
解析为带分支标注的边；动态跳转（jump expression / $ renpy.jump 变量）静态不可解，
存表达式为未决边，由前端如实展示（不硬猜）。

场景语句（scene/show/hide）按行号顺序记录为节点的场景快照，供场景合成器
还原节点画面缩略图；对话 speaker 聚合为节点出场人物。

产出为纯数据结构（StoryNode/StoryEdge），入库由 graph_repo 负责。
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from renpy_parser import RenpyParser
from source_tree import SourceTree

_unescape = RenpyParser._unescape_renpy


@dataclass
class SceneOp:
    """一条场景语句快照（行号用于定位"首次对话时的画面状态"）"""
    line: int
    op: str                    # scene | show | hide
    images: list               # 图像名组件，如 ['bg', 'room']
    at: list = field(default_factory=list)  # show 的 at 位置/transform 名
    dynamic: bool = False      # 含 expression，静态不可解析


@dataclass
class StoryNode:
    """剧情节点（一个 label）"""
    label: str
    file: str                  # 相对源码根的 posix 路径
    line_start: int
    line_end: int = 0
    speakers: list = field(default_factory=list)   # 有序去重的 speaker 变量名
    scene_ops: list = field(default_factory=list)  # list[SceneOp]
    dialogue_count: int = 0
    first_text: str = ''       # 首条台词原文（反转义完整文本）
    first_text_cn: str = ''    # 首条台词译文（构建管线回填）
    first_dlg_line: int = 0    # 首条台词行号（场景合成取此前画面状态）
    last_dlg_line: int = 0     # 末条台词行号（场景合成取此处画面状态）
    is_entry: bool = False     # label start
    has_return: bool = False
    is_terminal: bool = False  # 出度为 0（解析完成后回填）
    thumb_file: str = ''       # 场景缩略图文件名（构建管线回填）


@dataclass
class StoryEdge:
    """剧情边（一次跳转/调用/分支选择）"""
    source: str                # 源 label
    target: Optional[str]      # 目标 label；动态跳转不可解为 None
    kind: str                  # jump | call
    branch: str = ''           # '' | menu | condition（分支来源）
    text: str = ''             # 分支文字（menu 选项文本 / if 条件表达式）
    text_cn: str = ''          # 分支文字译文（构建管线回填）
    expr: str = ''             # 动态跳转表达式（target 为 None 时）
    implicit: bool = False     # 隐式 fall-through 边（label 体无 jump/return
                               # 结尾时流入同文件下一个 label，Ren'Py 语义）
    line: int = 0              # 语句所在行号（源文件内）


# 行模式
_LABEL_RE = re.compile(r'^label\s+(\w+)(?:\([^)]*\))?(?:\s+\w+)*\s*:')
_JUMP_RE = re.compile(r'^jump\s+(\w+)\s*$')
_JUMP_EXPR_RE = re.compile(r'^jump\s+expression\s+(.+)$')
_CALL_RE = re.compile(r'^call\s+(\w+)')
_CALL_EXPR_RE = re.compile(r'^call\s+expression\s+(.+)$')
_RETURN_RE = re.compile(r'^return\b')
_MENU_RE = re.compile(r'^menu\s*(?:"(?:[^"\\]|\\.)*?")?\s*:')
_OPTION_RE = re.compile(r'^"((?:[^"\\]|\\.)*?)"\s*:')
_IF_RE = re.compile(r'^if\s+(.+?)\s*:')
_ELIF_RE = re.compile(r'^elif\s+(.+?)\s*:')
_ELSE_RE = re.compile(r'^else\s*:')
_SCENE_RE = re.compile(r'^scene\s+(.+)$')
_SHOW_RE = re.compile(r'^show\s+(.+)$')
_HIDE_RE = re.compile(r'^hide\s+(.+)$')
_RENPY_PY_RE = re.compile(r'^\$\s*renpy\.(jump|call)\s*\(\s*(.+?)\s*\)')
# 对话：角色 "..."（旁白 "..." 对 speaker 无贡献，但计入台词数）。
# who 允许带点表达式（mc.name，Lab Rats 2 主角台词全用这种形式）与
# 图像属性（say 变体：e happy / e @ vhappy / e -concerned，Wartribe、
# HPMF 大量使用）；台词串到第一个未转义引号截止，行尾参数
# （如 (what_color="#8c8")）不影响截取
_CHAR_DLG_RE = re.compile(
    r'^([\w.]+)((?:\s+(?:-?\w+|@\s*\w+))*)\s+"((?:[^"\\]|\\.)*)"')
_NARR_DLG_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"')
# monologue 三引号台词：e """..."""；跨行块整体计 1 条（7.4+ 默认
# monologue 模式的写法）。who 后必须紧跟引号，python 多行字符串
# （x = """、foo("""）因此天然不匹配
_MONO_RE = re.compile(
    r'^([\w.]+)((?:\s+(?:-?\w+|@\s*\w+))*)\s+("""|\'\'\')')
_NARR_DLG_RE = re.compile(r'^"((?:[^"\\]|\\.)*?)"')

# 顶层非 label 语句：label 体结束的标志
_TOPLEVEL_RE = re.compile(
    r'^(?:define|default|image|init|screen|style|transform|layeredimage'
    r'|python|translate|testcase|voice|textbutton)\b')

# show/scene 语句中图像名的终止关键字
_SPEC_STOP = {'with', 'onlayer', 'at', 'as', 'behind', 'zorder', 'expression',
              'multiple', 'around', 'fade'}
# at 之后的 transform 参数终止
_AT_STOP = {'with', 'onlayer', 'behind', 'zorder', 'as'}

# 不是角色名的代码关键字（与 renpy_parser.CODE_KEYWORDS 对齐）
_CODE_KEYWORDS = {
    'textbutton', 'text', 'label', 'hbox', 'vbox', 'frame', 'bar', 'button',
    'image', 'show', 'hide', 'scene', 'play', 'stop', 'queue', 'voice',
    'with', 'pause', 'jump', 'call', 'return', 'menu', 'if', 'elif', 'else',
    'while', 'for', 'pass', 'init', 'default', 'define', 'transform',
    'screen', 'style', 'python', 'nvl', 'nvl_clear', 'nvl_narrator',
}


def _parse_image_spec(rest: str):
    """解析 scene/show/hide 的图像描述，返回 (images, at, dynamic)

    scene bg black → (['bg','black'], [], False)
    show eileen happy at left, flip → (['eileen','happy'], ['left','flip'], False)
    含 expression 标记为 dynamic（图像名静态不可解）。
    """
    tokens = rest.split()
    images, at = [], []
    dynamic = False
    i = 0
    while i < len(tokens):
        tok = tokens[i].rstrip(',')
        if tok == 'expression':
            dynamic = True
            i += 1
            continue
        if tok == 'at':
            i += 1
            while i < len(tokens):
                t = tokens[i].rstrip(',')
                if t in _AT_STOP:
                    i -= 1
                    break
                at.append(t)
                i += 1
        elif tok in _SPEC_STOP:
            i += 1
        else:
            images.append(tok)
        i += 1
    return images, at, dynamic


class FlowParser:
    """解析整个游戏源码树，产出剧情图节点与边"""

    def __init__(self, game_root: str):
        # game_root 与 SourceTree 一致：game/game 存在时需由调用方下钻
        self.tree = SourceTree(game_root)

    def parse(self) -> dict:
        """返回 {'nodes': list[StoryNode], 'edges': list[StoryEdge]}"""
        nodes = []
        edges = []
        for rel in self.tree.files():
            self._parse_file(rel, nodes, edges)
        self._postprocess(nodes, edges)
        return {'nodes': nodes, 'edges': edges}

    # ========== 单文件解析 ==========

    def _parse_file(self, rel: str, nodes: list, edges: list):
        lines = self.tree.lines(rel)
        node: Optional[StoryNode] = None
        # fall-through 跟踪：label 体基准缩进与最后一条基准语句类别。
        # Ren'Py 里 label 体结尾没有 jump/return 时会隐式流入同文件
        # 下一个 label（而非结束），这条隐式边必须补上，否则图大量断裂；
        # 但 menu 全选项跳走/if 结尾时不加（过度连接会把图搞碎）
        body_indent = -1
        last_base_stmt = ''   # jump | return | menu | if | other
        ctx: list = []        # (indent, kind, text, seq)；kind ∈ menu/option/cond
        menu_seq = 0          # menu 序号（label 内唯一）
        last_base_menu = 0    # 最后一条基准 menu 的序号
        opt_records: dict = {}  # id(option ctx entry) -> {'menu_seq','last'}
        mono_q = None         # 跨行 monologue 三引号块的闭合 token（None=不在块内）

        def close_node(end_line: int):
            nonlocal body_indent, last_base_stmt, last_base_menu
            if node is not None:
                node.line_end = end_line
                node._last_base_stmt = last_base_stmt  # type: ignore[attr-defined]
                # menu 结尾时记录其选项是否全部跳走（决定有无 fall-through）
                if last_base_stmt == 'menu':
                    opts = [r for r in opt_records.values()
                            if r['menu_seq'] == last_base_menu]
                    node._menu_terminal = bool(opts) and all(  # type: ignore[attr-defined]
                        r['last'] in ('jump', 'return') for r in opts)
            body_indent = -1
            last_base_stmt = ''
            last_base_menu = 0

        def touch_option(cat: str):
            """记录最内层 menu 选项块的最新语句类别（jump/return/other）"""
            for entry in reversed(ctx):
                if entry[1] == 'option':
                    rec = opt_records.get(id(entry))
                    if rec is not None:
                        rec['last'] = cat
                    break

        for idx, raw in enumerate(lines, 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith('#'):
                continue
            indent = len(raw) - len(raw.lstrip())

            # 跨行 monologue 块内：文本行不参与结构解析（缩进任意，
            # 不能让它触发上下文弹栈），仅把定位推进到闭合行
            if mono_q is not None:
                if mono_q in stripped:
                    mono_q = None
                    node.last_dlg_line = idx
                continue

            # 弹出缩进不小于当前行的上下文
            while ctx and indent <= ctx[-1][0]:
                ctx.pop()

            # 顶层（indent 0）非 label 语句：label 体结束
            if indent == 0 and not stripped.startswith('label '):
                if node is not None:
                    close_node(idx - 1)
                node = None
                ctx.clear()
                # 顶层语句本身无需处理（image 定义由场景合成器扫描）
                continue

            m = _LABEL_RE.match(stripped)
            if m and indent == 0:
                if node is not None:
                    close_node(idx - 1)
                node = StoryNode(label=m.group(1), file=rel, line_start=idx,
                                 is_entry=(m.group(1) == 'start'))
                nodes.append(node)
                ctx.clear()
                continue
            if node is None:
                continue

            # label 体基准缩进：首条语句的缩进
            if body_indent < 0:
                body_indent = indent

            # ---- 分支上下文 ----
            if _MENU_RE.match(stripped):
                menu_seq += 1
                ctx.append((indent, 'menu', '', menu_seq))
                if indent <= body_indent:
                    last_base_stmt = 'menu'
                    last_base_menu = menu_seq
                continue
            m = _OPTION_RE.match(stripped)
            if m and ctx and ctx[-1][1] in ('menu', 'option'):
                # 同 menu 的下一个选项：替换栈顶 option（弹栈后 menu 露出）
                # 选项文本反转义存储：与 dialogues/ui_texts 的原文键对齐，
                # 构建时据此映射译文
                cur_menu = ctx[-1][3]
                entry = (indent, 'option', _unescape(m.group(1)), cur_menu)
                ctx.append(entry)
                opt_records[id(entry)] = {'menu_seq': cur_menu, 'last': ''}
                continue
            m = _IF_RE.match(stripped) or _ELIF_RE.match(stripped)
            if m:
                ctx.append((indent, 'cond', m.group(1), 0))
                if indent <= body_indent:
                    last_base_stmt = 'if'
                continue
            if _ELSE_RE.match(stripped):
                ctx.append((indent, 'cond', '否则', 0))
                if indent <= body_indent:
                    last_base_stmt = 'if'
                continue

            # ---- 跳转/调用 ----
            edge = self._match_flow(stripped, node.label, idx, ctx)
            if edge is not None:
                edges.append(edge)
                touch_option('jump' if edge.kind == 'jump' else 'other')
                if indent <= body_indent:
                    # call 会返回继续执行，只有 jump/动态 jump 终结 fall-through
                    last_base_stmt = 'jump' if edge.kind == 'jump' else 'other'
                continue
            if _RETURN_RE.match(stripped):
                node.has_return = True
                touch_option('return')
                if indent <= body_indent:
                    last_base_stmt = 'return'
                continue

            # ---- 场景语句 ----
            m = _SCENE_RE.match(stripped)
            if m:
                images, at, dynamic = _parse_image_spec(m.group(1))
                node.scene_ops.append(SceneOp(idx, 'scene', images, at, dynamic))
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue
            m = _SHOW_RE.match(stripped)
            if m:
                images, at, dynamic = _parse_image_spec(m.group(1))
                node.scene_ops.append(SceneOp(idx, 'show', images, at, dynamic))
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue
            m = _HIDE_RE.match(stripped)
            if m:
                images, _, dynamic = _parse_image_spec(m.group(1))
                node.scene_ops.append(SceneOp(idx, 'hide', images, [], dynamic))
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue

            m3 = _MONO_RE.match(stripped)
            if m3 and m3.group(1).lower() not in _CODE_KEYWORDS:
                q = m3.group(3)
                rest = stripped[m3.end():]
                if q in rest:
                    # 同行闭合：e """text"""
                    self._count_dialogue(node, rest[:rest.index(q)], idx)
                else:
                    # 跨行块开始：整体计 1 条，定位到闭合行
                    mono_q = q
                    node.dialogue_count += 1
                    if not node.first_dlg_line:
                        node.first_dlg_line = idx
                    if rest.strip() and not node.first_text:
                        node.first_text = _unescape(rest.strip())
                    node.last_dlg_line = idx
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue

            # ---- 对话（speaker 与台词数）----
            m = _CHAR_DLG_RE.match(stripped)
            if m and m.group(1).lower() not in _CODE_KEYWORDS:
                var = m.group(1)
                if var not in node.speakers:
                    node.speakers.append(var)
                self._count_dialogue(node, m.group(3), idx)
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue
            m = _NARR_DLG_RE.match(stripped)
            if m:
                self._count_dialogue(node, m.group(1), idx)
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue

            # 其余语句（define/default/$/pause/with/voice 等）不转移控制流
            touch_option('other')
            if indent <= body_indent:
                last_base_stmt = 'other'

        if node is not None:
            close_node(len(lines))

        # ---- 隐式 fall-through 边：label 体以普通语句结尾（无 jump/return，
        # 也不是全选项跳走的 menu/if 分支结构）→ 同文件下一个 label
        # （跳过中间顶层 init 语句）。menu 的每个选项块都跟踪了末尾语句，
        # 全跳走才判终结；有内联内容的 menu 仍会补 fall-through（汇聚路径）
        file_nodes = [n for n in nodes if n.file == rel]
        for i, n in enumerate(file_nodes[:-1]):
            last = getattr(n, '_last_base_stmt', '')
            if last in ('jump', 'return', 'if'):
                continue
            if last == 'menu' and getattr(n, '_menu_terminal', False):
                continue
            edges.append(StoryEdge(source=n.label,
                                   target=file_nodes[i + 1].label,
                                   kind='jump', implicit=True))

    @staticmethod
    def _count_dialogue(node: StoryNode, text: str, line: int = 0):
        if not text.strip():
            return
        node.dialogue_count += 1
        if not node.first_text:
            # 保留完整原文（反转义）：译文映射要按完整原文查键，
            # 展示截断由前端负责
            node.first_text = _unescape(text.strip())
            node.first_dlg_line = line
        node.last_dlg_line = line

    def _match_flow(self, stripped: str, source: str, line: int,
                    ctx: list) -> Optional[StoryEdge]:
        """匹配 jump/call（含 expression 与 $ renpy.xxx），挂上分支上下文"""
        kind = target = expr = None

        m = _JUMP_EXPR_RE.match(stripped)
        if m:
            kind, expr = 'jump', m.group(1).strip()
        else:
            m = _JUMP_RE.match(stripped)
            if m:
                kind, target = 'jump', m.group(1)
        if kind is None:
            m = _CALL_EXPR_RE.match(stripped)
            if m:
                kind, expr = 'call', m.group(1).strip()
            else:
                m = _CALL_RE.match(stripped)
                if m and m.group(1) != 'screen':  # call screen 是界面调用，非剧情
                    kind, target = 'call', m.group(1)
        if kind is None:
            m = _RENPY_PY_RE.match(stripped)
            if m:
                kind = m.group(1)
                arg = m.group(2)
                q = re.match(r'^["\'](\w+)["\']$', arg)
                if q:
                    target = q.group(1)
                else:
                    expr = arg
        if kind is None:
            return None

        # 分支归属：最近的 menu 选项优先，其次 if 条件
        branch, text = '', ''
        for _, ckind, ctext, _ in reversed(ctx):
            if ckind == 'option':
                branch, text = 'menu', ctext
                break
            if ckind == 'cond' and not branch:
                branch, text = 'condition', ctext
        return StoryEdge(source=source, target=target, kind=kind,
                         branch=branch, text=text, expr=expr or '', line=line)

    # ========== 后处理 ==========

    @staticmethod
    def _postprocess(nodes: list, edges: list):
        """回填 terminal 标记、按 source/target/branch/text/line 去重边"""
        seen = set()
        deduped = []
        for e in edges:
            key = (e.source, e.target, e.branch, e.text, e.expr)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(e)

        known = {n.label for n in nodes}
        out_labels = {e.source for e in deduped if e.target}
        for n in nodes:
            n.is_terminal = n.label not in out_labels
        # 目标指向不存在 label 的边保留（前端可标"外部/缺失"），不强行清除
        edges[:] = deduped
