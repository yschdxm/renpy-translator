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
    op: str                    # scene | show | hide | exec
    images: list               # 图像名组件，如 ['bg', 'room']
    at: list = field(default_factory=list)  # show 的 at 位置/transform 名
    dynamic: bool = False      # 含 expression，静态不可解析
    code: str = ''             # op=exec 时的 python 语句（引擎沙盒回放用，
                               # 如 the_person.draw_person(...)）


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
    thumb_files: list = field(default_factory=list)  # 候选缩略图（构建回填）


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
    via_screen: bool = False   # 源自 show screen 的界面按钮（环境屏幕被滤除，
                               # 见 _filter_screen_edges）
    screen: str = ''           # via_screen 边的来源 screen 名
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
_RENPY_PY_RE = re.compile(
    r'^\$\s*renpy\.(jump|call|call_screen|show_screen)\s*\(\s*(.+?)\s*\)')
# $ renpy.show("tag", what = bg_manager.background("Name"), ...) —— Python
# 式 show（沙盒游戏的背景引用多走这种，不走 scene/show 语句）
_RENPY_SHOW_RE = re.compile(r'^\$\s*renpy\.show\s*\(')
_SHOW_WHAT_RE = re.compile(r'what\s*=\s*[^,)"]*?["\']([^"\']+)["\']')
# 地点系统（沙盒游戏）：room 变量 = Room("id", "名称", "Bg_Name", ...)
# 定义 + $ xxx.change_location(room 变量) 切换 → 合成 scene 快照；
# 名称参数容忍 _(...) 翻译包裹（_("Living Room") 形式常见）
_ROOM_DEF_RE = re.compile(
    r'(\w+)\s*=\s*Room\s*\(\s*_?\(?\s*["\'][^"\']*["\']\s*\)?'
    r'\s*,\s*_?\(?\s*["\'][^"\']*["\']\s*\)?\s*,\s*["\']([^"\']+)["\']')
_CHANGE_LOC_RE = re.compile(r'\.change_location\s*\(\s*(\w+)')
# exec 白名单：引擎沙盒里原样回放的 python 语句（程序化角色绘制，
# 如 LR2 的 the_person = jennifer / the_person.draw_person(position=...)）
_EXEC_WHITELIST_RE = re.compile(
    r'^\$\s*(the_person\s*=\s*\w+|\w+\.draw_person\s*\().*$')
# 注册池：构造器参数里恰好是 label 名的字符串（沙盒游戏的
# Action("名称", 需求, "effect_label") / Crisis(...) 事件注册，
# 含 xxx_list.append(Action(...)) 形式），供 .effect 派发展开；
# 只排除控制流/查询行（字符串最终按 label 名过滤，误收无害）
_POOL_EXCLUDE_RE = re.compile(r'jump|call|seen_label|has_label|renpy\.')
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

# 事件袋动态跳转的静态恢复：
#   $ bag.append("label_x") / bag = ["a", "b"]  → 注册表 bag → {候选 label}
#   $ ev = bag.pop()                            → 别名（label 作用域）ev → bag
#   $ renpy.jump(ev) / jump expression ev       → 沿别名取袋、注册表展开为多边
_APPEND_RE = re.compile(r'(\w+)\s*\.\s*(?:append|insert|extend)\s*\(([^)]*)')
_BAG_LIST_RE = re.compile(r'(\w+)\s*\+?=\s*\[([^\]]*)\]')
_STRS_RE = re.compile(r'["\'](\w+)["\']')
# screen 动作里设置的导航变量（SetVariable("nav_screen", "xxx_navigation")）
# ——跨 label 的 UI 状态，派发 label 处按注册表展开
_SETVAR_RE = re.compile(
    r'\bSetVariable\(\s*["\'](\w+)["\']\s*,\s*["\'](\w+)["\']')
_PY_ASSIGN_RE = re.compile(r'^(?:\$\s*)?(\w+)\s*=\s*(.+?)\s*$')
_BARE_VAR_RE = re.compile(r'^\w+$')
_EXPR_POP_RE = re.compile(r'(\w+)\.pop')
# screen 导航：screen 体内的 Jump("x") / Call("x") 动作 → call screen 的
# label 连向这些目标（imagemap hotspot / imagebutton / textbutton 通用）
_SCREEN_RE = re.compile(r'^screen\s+(\w+)')
_JUMP_ACTION_RE = re.compile(r'\bJump\(\s*["\'](\w+)["\']\s*')
_CALL_ACTION_RE = re.compile(r'\bCall\(\s*["\'](\w+)["\']\s*')
_CALL_SCREEN_RE = re.compile(r'^call\s+screen\s+(\w+)')
_SHOW_SCREEN_RE = re.compile(r'^show\s+screen\s+(\w+)')


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
        # game_root 与 SourceTree 一致：game/game 存在时需由调用方下钻；
        # include_renpy_py：事件注册/动态派发常写在 *_ren.py 里（沙盒游戏）
        self.tree = SourceTree(game_root, include_renpy_py=True)
        # screen 名 → 体内 Jump/Call 动作引用的 label（跨文件收集，供
        # call screen 连边）
        self._screens: dict = {}
        # 地点系统：room 变量 → 背景图名（change_location 合成 scene 快照）
        self._rooms: dict = {}

    def parse(self) -> dict:
        """返回 {'nodes': list[StoryNode], 'edges': list[StoryEdge]}"""
        nodes = []
        edges = []
        regs, aliases, pool = self._collect_event_bags()
        self._collect_screens()
        self._collect_rooms()
        for rel in self.tree.files():
            self._parse_file(rel, nodes, edges)
        self._postprocess(nodes, edges, regs, aliases, pool)
        return {'nodes': nodes, 'edges': edges}

    # ========== 地点系统收集 ==========

    def _collect_rooms(self):
        """room 变量 = Room("id", "名称", "Bg_Name", ...) 定义收集"""
        for rel in self.tree.files():
            for raw in self.tree.lines(rel):
                m = _ROOM_DEF_RE.search(raw)
                if m:
                    self._rooms.setdefault(m.group(1), m.group(2))

    # ========== screen 导航收集 ==========

    def _collect_screens(self):
        """先于边解析扫全项目 screen 定义（call screen 可以前向引用）"""
        for rel in self.tree.files():
            cur = ''
            for raw in self.tree.lines(rel):
                s = raw.strip()
                if not s or s.startswith('#'):
                    continue
                indent = len(raw) - len(raw.lstrip())
                if indent == 0:
                    m = _SCREEN_RE.match(s)
                    cur = m.group(1) if m else ''
                    continue
                if not cur:
                    continue
                for lit in _JUMP_ACTION_RE.findall(s):
                    self._screens.setdefault(cur, set()).add(lit)
                for lit in _CALL_ACTION_RE.findall(s):
                    self._screens.setdefault(cur, set()).add(lit)

    # ========== 事件袋收集 ==========

    def _collect_event_bags(self) -> tuple[dict, list, set]:
        """全项目扫描事件袋注册、跳转变量赋值与 Action 注册池

        返回 (regs, aliases, pool)：
        - regs: {袋名: set(字符串字面量)}——解析后只保留能对上 label 的
        - aliases: [(file, label, line, var, rhs)]——`$ var = ...` 且右值
          含 .pop( 或字符串字面量的赋值点；解析时按位置就近取
        - pool: set(字符串字面量)——构造器参数中可能是 label 名的字符串
          （Action/Crisis 等事件注册），供 `.effect` 派发展开
        """
        regs: dict = {}
        aliases: list = []
        pool: set = set()
        for rel in self.tree.files():
            cur_label = ''
            for no, raw in enumerate(self.tree.lines(rel), 1):
                s = raw.strip()
                if not s or s.startswith('#'):
                    continue
                indent = len(raw) - len(raw.lstrip())
                lm = _LABEL_RE.match(s)
                if lm and indent == 0:
                    cur_label = lm.group(1)
                    continue
                m = _APPEND_RE.search(s)
                if m:
                    for lit in _STRS_RE.findall(m.group(2)):
                        regs.setdefault(m.group(1), set()).add(lit)
                    continue
                m = _BAG_LIST_RE.search(s)
                if m:
                    for lit in _STRS_RE.findall(m.group(2)):
                        regs.setdefault(m.group(1), set()).add(lit)
                    continue
                for var, lit in _SETVAR_RE.findall(s):
                    regs.setdefault(var, set()).add(lit)
                m = _PY_ASSIGN_RE.match(s)
                if m and cur_label and indent > 0:
                    rhs = m.group(2)
                    if ('.pop(' in rhs or _STRS_RE.search(rhs)
                            or _BARE_VAR_RE.match(rhs)):
                        aliases.append((rel, cur_label, no, m.group(1), rhs))
                # 注册池：构造器调用参数中的字符串（非控制流/查询行）
                if '(' in s and not _POOL_EXCLUDE_RE.search(s):
                    pool.update(_STRS_RE.findall(s))
        return regs, aliases, pool

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
        cur_screen = ''       # 正在收集体内的 screen 名（'' = 不在 screen 里）
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

            # screen 体内（缩进 > 0）：收集 Jump/Call 动作引用的 label
            if cur_screen and indent > 0:
                for lit in _JUMP_ACTION_RE.findall(stripped):
                    self._screens.setdefault(cur_screen, set()).add(lit)
                for lit in _CALL_ACTION_RE.findall(stripped):
                    self._screens.setdefault(cur_screen, set()).add(lit)
                continue

            # 顶层（indent 0）非 label 语句：label 体结束
            if indent == 0 and not stripped.startswith('label '):
                if node is not None:
                    close_node(idx - 1)
                node = None
                ctx.clear()
                # screen 定义开始：后续缩进体内收集导航动作
                m = _SCREEN_RE.match(stripped)
                cur_screen = m.group(1) if m else ''
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
                cur_screen = ''
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
            # exec 白名单（程序化角色绘制，沙盒回放）。draw_person 调用
            # 注入 wipe_scene=False：默认 True 会清掉整个场景（含已渲染
            # 的背景），缩略图合成语境下只要叠角色不要清屏
            if _EXEC_WHITELIST_RE.match(stripped):
                code = stripped.lstrip('$').strip()
                if '.draw_person(' in code and 'wipe_scene' not in code:
                    paren = code.rfind(')')
                    if paren > code.rfind('draw_person('):
                        code = (code[:paren]
                                + ('' if code.rfind('(') == paren - 1
                                   else ', ')
                                + 'wipe_scene=False, show_person_info=False'
                                + code[paren:])
                node.scene_ops.append(
                    SceneOp(idx, 'exec', [], [], code=code))
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue
            # $ xxx.change_location(room)：地点系统切背景 → scene 快照；
            # code 存原文行（沙盒 exec 后 mc.location 才有光照等运行时状态）
            cm = _CHANGE_LOC_RE.search(stripped)
            if cm and cm.group(1) in self._rooms:
                node.scene_ops.append(
                    SceneOp(idx, 'scene', [self._rooms[cm.group(1)]], [],
                            code=stripped.lstrip('$').strip()))
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue
            # $ renpy.show("tag", what = xxx("Name"), ...)：Python 式背景
            # 引用（沙盒游戏常见）。show 快照给 Pillow 合成器；原文行同时存
            # code——引擎沙盒里按原文 exec 是高保真路径（bg_manager 这类
            # 自建显示器的图像名静态不可解析）
            if _RENPY_SHOW_RE.match(stripped):
                wm = _SHOW_WHAT_RE.search(stripped)
                if wm:
                    node.scene_ops.append(
                        SceneOp(idx, 'show', [wm.group(1)], [],
                                code=stripped.lstrip('$').strip()))
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue
            # call screen：连向 screen 体内 Jump/Call 动作引用的 label
            #（screen 返回后流程继续，不影响 fall-through）
            m = _CALL_SCREEN_RE.match(stripped)
            if m and self._screens.get(m.group(1)):
                branch, text = FlowParser._branch_of(ctx)
                for t in sorted(self._screens[m.group(1)]):
                    edges.append(StoryEdge(source=node.label, target=t,
                                           kind='jump', branch=branch,
                                           text=text, screen=m.group(1),
                                           line=idx))
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue
            # show screen：常驻界面按钮（HUD/小游戏操作盘）。边先生成，
            # 后处理滤除环境屏幕（被海量 label show 的 HUD）
            m = _SHOW_SCREEN_RE.match(stripped)
            if m and self._screens.get(m.group(1)):
                branch, text = FlowParser._branch_of(ctx)
                for t in sorted(self._screens[m.group(1)]):
                    edges.append(StoryEdge(source=node.label, target=t,
                                           kind='jump', branch=branch,
                                           text=text, via_screen=True,
                                           screen=m.group(1), line=idx))
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue
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
                # show screen 是界面调用不是图像（show screen overlay_scr
                # 这类进快照会让引擎渲染出红字报错）
                if not images or images[0] != 'screen':
                    node.scene_ops.append(
                        SceneOp(idx, 'show', images, at, dynamic))
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue
            m = _HIDE_RE.match(stripped)
            if m:
                images, _, dynamic = _parse_image_spec(m.group(1))
                if not images or images[0] != 'screen':
                    node.scene_ops.append(
                        SceneOp(idx, 'hide', images, [], dynamic))
                touch_option('other')
                if indent <= body_indent:
                    last_base_stmt = 'other'
                continue

            # ---- 对话（speaker 与台词数）----
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
            m = _CHAR_DLG_RE.match(stripped)
            if m and m.group(1).lower() not in _CODE_KEYWORDS:
                # 归一为根标识符：mc.name "..." 的说话人本体是 mc，
                # 属性后缀（.name/.title）不是变量，角色映射按根匹配
                var = m.group(1).split('.')[0]
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

    @staticmethod
    def _branch_of(ctx: list) -> tuple:
        """分支归属：最近的 menu 选项优先，其次 if 条件"""
        branch, text = '', ''
        for _, ckind, ctext, _ in reversed(ctx):
            if ckind == 'option':
                branch, text = 'menu', ctext
                break
            if ckind == 'cond' and not branch:
                branch, text = 'condition', ctext
        return branch, text

    def _match_flow(self, stripped: str, source: str, line: int,
                    ctx: list) -> Optional[StoryEdge]:
        """匹配 jump/call（含 expression 与 $ renpy.xxx），挂上分支上下文"""
        kind = target = expr = None
        fn = ''

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
                fn, arg = m.group(1), m.group(2)
                # call_screen 语义同 call（返回后继续）；show_screen 为
                # 常驻界面按钮（经环境屏幕过滤）
                kind = {'jump': 'jump', 'call': 'call',
                        'call_screen': 'call', 'show_screen': 'jump'}[fn]
                q = re.match(r'^["\'](\w+)["\']$', arg)
                if q:
                    target = q.group(1)
                else:
                    expr = arg
        if kind is None:
            return None

        # 分支归属：最近的 menu 选项优先，其次 if 条件
        branch, text = FlowParser._branch_of(ctx)
        return StoryEdge(source=source, target=target, kind=kind,
                         branch=branch, text=text, expr=expr or '',
                         via_screen=(fn == 'show_screen') if fn else False,
                         screen=target or '', line=line)

    # ========== 后处理 ==========

    def _postprocess(self, nodes: list, edges: list, regs: dict,
                     aliases: list, pool: set):
        """回填 terminal 标记、按 source/target/branch/text/line 去重边、
        用事件袋注册表展开动态跳转"""
        seen = set()
        deduped = []
        for e in edges:
            key = (e.source, e.target, e.branch, e.text, e.expr)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(e)

        known = {n.label for n in nodes}
        FlowParser._resolve_dynamic(nodes, deduped, regs, aliases, known,
                                    self._screens, pool)
        FlowParser._expand_screen_targets(deduped, self._screens, known)
        FlowParser._filter_screen_edges(deduped)


        out_labels = {e.source for e in deduped if e.target}
        for n in nodes:
            n.is_terminal = n.label not in out_labels
        # 目标指向不存在 label 的边保留（前端可标"外部/缺失"），不强行清除
        edges[:] = deduped

    @staticmethod
    def _filter_screen_edges(edges: list):
        """screen 边的两级过滤

        1. show screen（via_screen）候选边滤除"环境屏幕"：HUD 等常驻界面
           被几乎所有 label show，全连会淹没剧情图；内容界面（小游戏
           操作盘、事件按钮盘）只被少数 label show，保留
        2. call screen 边折叠"hub 界面"：被 >30 个 label 调用的导航界面，
           只保留代表调用方（行号最小）的边——可达性不变，但不会产生
           "每个 label × 每个界面目标"的毛线团
        """
        by_screen: dict = {}
        for e in edges:
            if e.via_screen and e.target is not None:
                by_screen.setdefault(e.screen, set()).add(e.source)
        ambient = {s for s, labels in by_screen.items() if len(labels) > 30}

        call_srcs: dict = {}
        call_targets: dict = {}
        for e in edges:
            if not e.via_screen and e.screen and e.target is not None:
                call_srcs.setdefault(e.screen, set()).add(e.source)
                call_targets.setdefault(e.screen, set()).add(e.target)
        # hub 界面：调用方×目标 总量大（如 20 调用方 × 364 按钮 = 7280 条
        # 边的导航主界面）——折叠为代表调用方，保留可达性、去掉毛线团
        hub_screens = {s for s, srcs in call_srcs.items()
                       if len(srcs) > 1
                       and len(srcs) * len(call_targets.get(s, ())) > 300}

        # hub 边的代表调用方必须可达：hub-and-spoke 游戏里 hub 互相嵌套
        #（事件 label 结束又回到主界面），一次性断所有 hub 会找不到任何
        # 可达调用方——逐 hub 独立判断：只断当前 hub 的边做 BFS
        hub_keep: dict = {}
        for hub in hub_screens:
            adj: dict = {}
            for e in edges:
                if e.target is None or (not e.via_screen
                                        and e.screen == hub):
                    continue
                adj.setdefault(e.source, []).append(e.target)
            seen = {'start'}
            queue = ['start']
            while queue:
                cur = queue.pop()
                for t in adj.get(cur, []):
                    if t not in seen:
                        seen.add(t)
                        queue.append(t)
            reach = sorted(x for x in call_srcs[hub] if x in seen)
            # 无可达调用方（极端嵌套循环）按字母序兜底，保证目标不断连
            hub_keep[hub] = reach[0] if reach else sorted(call_srcs[hub])[0]

        kept = []
        for e in edges:
            if e.via_screen and e.screen in ambient:
                continue
            if (not e.via_screen and e.screen in hub_screens
                    and e.source != hub_keep.get(e.screen)):
                continue
            kept.append(e)
        # hub 的非代表调用方补回流边 C → 代表（语义：C 进入主界面/事件
        # 结束后回到主界面）——没有这条边，"以 call screen 主界面结尾"
        # 的事件链会全部出度为 0 被误判为结局
        for hub, rep in hub_keep.items():
            for src in call_srcs[hub]:
                if src != rep:
                    kept.append(StoryEdge(source=src, target=rep,
                                          kind='jump', screen=hub))
        edges[:] = kept

    @staticmethod
    def _resolve_dynamic(nodes: list, edges: list, regs: dict,
                         aliases: list, known: set, screens: dict,
                         pool: set):
        """动态跳转（target=None 且带表达式）→ 事件袋注册展开为多边

        解析链（依次尝试）：
        1. 表达式是裸标识符 → 按所在 label 就近找 `$ VAR = 右值` 别名。
           右值含 `袋.pop(...)` → 查袋注册表；右值是 screen 名（间接
           call_screen 派发：`$ nav_screen = "throne_navigation"`）→ 展开为
           该 screen 的跳转目标；否则对右值做前缀展开（`$ ev = "v_" + str(...)`）
        2. 表达式直接含 `袋.pop(...)` → 查袋注册表
        3. 表达式本身含字符串字面量拼接（`renpy.jump("fan_talk_" + str(x))`）
           → 前缀展开
        都失败才保留未决原边。别名按 (file, label, line<) 就近取，同一变量
        在 if/else 各分支的不同赋值取并集
        """
        node_by = {n.label: n for n in nodes}
        sorted_labels = sorted(known)

        def bag_targets(bag: str) -> list:
            if not bag:
                return []
            return sorted(t for t in regs.get(bag, ()) if t in known)

        def prefix_targets(expr: str) -> list:
            # 表达式中能匹配已知 label 的最长字面量前缀
            best, hits = '', []
            for p in _STRS_RE.findall(expr):
                if not p:
                    continue
                m = [l for l in sorted_labels if l.startswith(p)]
                if m and len(p) > len(best):
                    best, hits = p, m
            return hits[:100]

        def screen_targets(rhs: str) -> list:
            # 右值中的 screen 名 → 该 screen 内 Jump/Call 引用的 label
            out = []
            for lit in _STRS_RE.findall(rhs):
                if lit in screens and lit not in known:
                    out.extend(t for t in sorted(screens[lit])
                               if t in known)
            return out

        def reg_targets(var: str) -> list:
            # 变量注册表（SetVariable/screen 名/label 名混合）：label 直接
            # 用，screen 展开为其跳转目标
            out = []
            for lit in sorted(regs.get(var, ())):
                if lit in known:
                    out.append(lit)
                elif lit in screens:
                    out.extend(t for t in sorted(screens[lit])
                               if t in known)
            return out

        resolved = []
        for e in edges:
            if e.target is not None or not e.expr:
                resolved.append(e)
                continue
            expr = e.expr.strip()
            targets: list = []
            if _BARE_VAR_RE.match(expr):
                src = node_by.get(e.source)
                cands = [a for a in aliases
                         if a[2] < e.line and a[3] == expr
                         and src is not None and a[0] == src.file
                         and a[1] == e.source]
                if not cands and src is not None:
                    cands = [a for a in aliases
                             if a[2] < e.line and a[3] == expr
                             and a[0] == src.file]
                if cands:
                    # 并集：if/else 各分支给同一变量赋不同来源（袋/计算名/
                    # screen 名），任一分支都可能流到本跳转
                    seen_t: set = set()
                    for a in cands:
                        pm = _EXPR_POP_RE.search(a[4])
                        sc = ''
                        rhs = a[4]
                        # 右值是 .pop(...) / 裸变量（直接是袋子名）都查注册表
                        ts = bag_targets(pm.group(1) if pm else
                                         (rhs if _BARE_VAR_RE.match(rhs)
                                          else ''))
                        if not ts:
                            sts = screen_targets(a[4])
                            if sts:
                                sc = next(
                                    (lit for lit in _STRS_RE.findall(a[4])
                                     if lit in screens and lit not in known),
                                    '')
                                ts = sts
                        if not ts:
                            ts = prefix_targets(a[4])
                        for t in ts:
                            if t not in seen_t:
                                seen_t.add(t)
                                resolved.append(StoryEdge(
                                    source=e.source, target=t,
                                    kind=e.kind, branch=e.branch,
                                    text=e.text, text_cn=e.text_cn,
                                    expr=e.expr, implicit=e.implicit,
                                    via_screen=e.via_screen,
                                    screen=sc if e.via_screen else ''))
                    continue
                if expr in regs:
                    targets = bag_targets(expr) or reg_targets(expr)
            else:
                pm = _EXPR_POP_RE.search(expr)
                targets = bag_targets(pm.group(1) if pm else '') \
                    or screen_targets(expr) or prefix_targets(expr)
            if not targets and '.effect' in expr:
                # 沙盒事件派发（call expression crisis.effect）：
                # 展开为注册池（Action/Crisis 构造器里的 label 名字符串）。
                # 沙盒游戏的池就是主体内容：按台词量排序优先保留重内容
                targets = sorted(
                    (t for t in pool if t in known),
                    key=lambda t: -node_by[t].dialogue_count)[:1500]
            if not targets:
                resolved.append(e)
                continue
            for t in targets:
                resolved.append(StoryEdge(source=e.source, target=t,
                                          kind=e.kind, branch=e.branch,
                                          text=e.text, text_cn=e.text_cn,
                                          expr=e.expr, implicit=e.implicit,
                                          via_screen=e.via_screen,
                                          screen=e.screen, line=e.line))
        edges[:] = resolved

    @staticmethod
    def _expand_screen_targets(edges: list, screens: dict, known: set):
        """目标是不存在 label 但存在 screen（`renpy.call_screen("xxx")` 直写
        screen 名）→ 展开为该 screen 内 Jump/Call 引用的 label；screen 无
        剧情跳转则丢弃该边（纯 HUD/界面）"""
        kept = []
        for e in edges:
            if (e.target is None or e.target not in screens
                    or e.target in known):
                kept.append(e)
                continue
            for t in sorted(screens[e.target]):
                if t in known:
                    kept.append(StoryEdge(
                        source=e.source, target=t, kind=e.kind,
                        branch=e.branch, text=e.text, text_cn=e.text_cn,
                        expr=e.expr, implicit=e.implicit,
                        via_screen=e.via_screen, screen=e.screen, line=e.line))
        edges[:] = kept
