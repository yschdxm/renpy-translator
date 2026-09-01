"""静态用途分流：用确定性规则把内嵌文本候选分成 keep / drop / unknown

设计原则（可靠性的来源）：
- 规则只在有铁证时下结论；任何无法分类的出现点都把候选降级为
  unknown（交回 AI 判定）。规则宁可不表态，也不表错态。
- 出现点搜索是 verbatim 子串匹配，机械完备，没有智能成分。
- 任何非显示用途（比较、键名、索引、资源引用）都是"危险"信号：
  apply_wrapping 只包候选自己的定义点，其他位置的 == 比较/键查找
  仍在和原文比，翻译后逻辑会失效——因此显示+非显示混合用途的候选
  永不自动 keep。

判定聚合（对一个候选的全部出现点）：
    有未分类出现点          → unknown（交 AI）
    显示 + 非显示 混合      → unknown + danger（交 AI，附警告）
    仅显示                  → keep
    仅非显示                → drop

拼接/格式化用途（"前缀" + name、"%s" % x、.format()）归显示侧
（文本终将到达玩家），但会给候选打上 static_fragment 标记——
渲染时字符串已变形，strings 表 old/new 查不中，apply 必须走
_() 包裹（decide_apply_path）。

变量流向追踪是 BFS（深度上限 2）：重赋值链、容器成员、for 循环变量、
可唯一解析的函数参数都可追踪；任何一跳无法分类即 unknown
（保守原则：宁可交 AI，不可判错）。
"""

import re
from pathlib import Path

KEEP, DROP, UNKNOWN = 'keep', 'drop', 'unknown'

# 规则判定理由的统一前缀（server 端据此区分判定来源，前端显示不同标识）
RULE_REASON_PREFIX = '规则: '

_EXCLUDE_DIRS = {'renpy', 'lib', 'saves', 'cache', 'tl', 'output',
                 'audio', 'sound', 'images', 'image', 'fonts', 'font',
                 'video', 'movies'}

# ---- 出现点上下文模式 ----

# 显示用途（白名单）：前缀匹配
_DISPLAY_PREFIX_RE = re.compile(r'\b(text|textbutton|tooltip|label)\s*$')
_DISPLAY_CALLS = (
    '_(', 'renpy.notify(', 'notify(', 'renpy.input(', 'Text(',
    'renpy.say(', 'renpy.display_notify(', 'Notify(',
)

# 非显示用途（黑名单）：前缀/语句匹配
_NONDISPLAY_CALLS = (
    'renpy.has_image(', 'renpy.image(', 'renpy.show(', 'renpy.hide(',
    'renpy.scene(', 'renpy.jump(', 'renpy.call(', 'renpy.call_screen(',
    'renpy.show_screen(', 'renpy.has_label(', 'renpy.loadable(',
    'renpy.file(', 'renpy.music.play(', 'renpy.music.queue(',
    'renpy.sound.play(', 'renpy.sound.queue(', 'open(', 'os.path.join(',
    '.startswith(', '.endswith(', '.get(', '.pop(', '.setdefault(',
    'Show(', 'Hide(', 'ShowMenu(', 'Jump(', 'Call(',
    'style ', 'style=',
)
_NONDISPLAY_STMT_RE = re.compile(
    r'^(play|queue|scene|show|hide|jump|call|image|voice)\s')
_COMPARE_PREFIX_RE = re.compile(r'(==|!=)\s*$')
_COMPARE_SUFFIX_RE = re.compile(r'^(==|!=)')
_MEMBER_SUFFIX_RE = re.compile(r'^(not\s+)?in\b')

# 赋值点：define/default/$ 单行/普通赋值，右侧恰好是本字面量
_ASSIGN_PREFIX_RE = re.compile(
    r'^(?:\s*(?:define|default)\s+|\s*\$\s*)?\s*([\w.]+)\s*=\s*$')

# f-string 起始标记（前缀字母前不能是标识符字符）
_FSTRING_MARK_RE = re.compile(r"[fF](\"{3}|'{3}|\"|')")

# 追踪深度上限：name → 传递 → 传递 → 叶子分类（更深一律 unknown）
_MAX_TRACE_DEPTH = 2


def decide_apply_path(c) -> str:
    """应用路径决策（静态为主、精审建议为辅）：
    - 拼接/格式化用途（static_fragment）→ wrap：渲染时字符串已变形，
      strings 表 old/new 整串匹配查不中，必须在数据层包 _()
    - 其余默认 table：零源码改动，逻辑值保持原文，渲染时才替换——
      双重用途字符串在表路径下天然安全
    - 静态证据不足时精审 AI 可凭证据覆盖（submit_verdicts 的 apply 字段）
    """
    if getattr(c, 'static_fragment', False):
        return 'wrap'
    ai_apply = getattr(c, 'ai_apply', '')
    return ai_apply if ai_apply in ('table', 'wrap') else 'table'


def _in_fstring_braces(line: str, start: int) -> bool:
    """出现点是否在 f-string 的花括号插值内（此时它是被显示表达式的一部分，
    如 f"{' '.join(words)}" 里的 ' '）"""
    for m in _FSTRING_MARK_RE.finditer(line):
        if m.start() > 0 and (line[m.start() - 1].isalnum()
                              or line[m.start() - 1] in '_.'):
            continue
        q = m.group(1)
        close = line.find(q, m.end())
        if close == -1 or start < m.end() or start >= close:
            continue
        depth = 0
        for i in range(m.end(), start):
            if line[i] == '{':
                depth += 1
            elif line[i] == '}':
                depth -= 1
        if depth > 0:
            return True
    return False


class UsageAnalyzer:
    """对一批候选做静态用途分流"""

    def __init__(self, game_root: str, files: dict = None):
        """game_root: 与 find_candidates 的 rel_file 基准一致的源码根目录
        files: 可选的预加载 {rel_path: 行列表}（如 SourceTree.as_dict()），
            传入后跳过全树扫描（与 AI 预筛共用同一源码缓存）"""
        self.root = Path(game_root)
        # rel_path -> 行列表（排除引擎/资源目录与注释行在查找时处理）
        if files is not None:
            self.files = files
            return
        self.files = {}
        for rpy in sorted(self.root.rglob('*.rpy')):
            if _EXCLUDE_DIRS & set(rpy.parts):
                continue
            try:
                self.files[rpy.relative_to(self.root).as_posix()] = (
                    rpy.read_text(encoding='utf-8', errors='ignore').split('\n'))
            except OSError:
                continue

    # ========== 总入口 ==========

    # static_sites 上限：出现点证据供粗筛/精审蒸馏与 apply 决策用
    _SITES_CAP = 50

    def classify_all(self, candidates: list):
        """批量分流，结果写回候选：
        - static_verdict / static_reason / static_danger（判定与警告）
        - static_sites / static_sites_truncated（出现点证据
          [{'site','kind','text'}]，P1 蒸馏输入与 P4 apply 决策的数据源）
        - static_fragment（有拼接/格式化用途 → apply 走 _() 包裹）
        """
        occ_map = self._find_all_occurrences(candidates)
        for i, c in enumerate(candidates):
            verdict, reason, danger, sites, fragment = self._classify_one(
                occ_map.get(i, []))
            c.static_verdict = verdict
            c.static_reason = reason
            c.static_danger = danger
            c.static_sites_truncated = len(sites) > self._SITES_CAP
            c.static_sites = sites[:self._SITES_CAP]
            c.static_fragment = fragment

    # ========== 出现点搜索 ==========

    def _find_all_occurrences(self, candidates: list) -> dict:
        """全源码 verbatim 搜索每个候选的带引号字面量，返回 {候选下标: [出现点]}

        出现点 = (rel, 行号, 行文本, 起始列, 结束列)。
        同一文本的另一种引号形式也纳入（源码里 "x" 和 'x' 是同一字符串）。
        """
        # 字面量 -> 候选下标集合
        lit_map = {}
        for i, c in enumerate(candidates):
            lit_map.setdefault(c.raw, set()).add(i)
            alt = self._alt_quote(c.raw)
            if alt:
                lit_map.setdefault(alt, set()).add(i)

        result = {}
        literals = sorted(lit_map, key=len, reverse=True)
        for chunk_start in range(0, len(literals), 500):
            chunk = literals[chunk_start:chunk_start + 500]
            pattern = re.compile('|'.join(re.escape(lit) for lit in chunk))
            for rel, lines in self.files.items():
                for line_no, line in enumerate(lines, 1):
                    if line.strip().startswith('#'):
                        continue
                    for m in pattern.finditer(line):
                        for i in lit_map[m.group(0)]:
                            result.setdefault(i, []).append(
                                (rel, line_no, line, m.start(), m.end()))
        return result

    @staticmethod
    def _alt_quote(raw: str):
        """另一种引号形式（内容里不含冲突引号时才有意义）"""
        if raw.startswith('"') and "'" not in raw:
            return "'" + raw[1:-1] + "'"
        if raw.startswith("'") and '"' not in raw:
            return '"' + raw[1:-1] + '"'
        return None

    # ========== 单个候选的聚合判定 ==========

    def _classify_one(self, occs: list):
        """返回 (verdict, reason, danger, sites, fragment)"""
        has_display = has_nondisplay = has_unknown = has_fragment = False
        display_sites, nondisplay_sites = [], []
        sites = []
        var_names = set()

        for rel, line_no, line, start, end in occs:
            kind, var = self._site_kind(line, start, end)
            site = f'{rel}:{line_no}'
            sites.append({'site': site, 'kind': kind or 'unknown',
                          'text': line.strip()[:120]})
            if kind == 'display':
                has_display = True
                display_sites.append(site)
            elif kind == 'nondisplay':
                has_nondisplay = True
                nondisplay_sites.append(site)
            elif kind in ('format', 'fragment'):
                # 拼接/格式化：文本终将到达玩家（归显示侧），但渲染时已
                # 变形——strings 表查不中，apply 必须走 _() 包裹
                has_display = True
                has_fragment = True
                display_sites.append(site)
            elif kind == 'assign':
                var_names.add(var)
            else:
                has_unknown = True

        # 赋值点触发变量流向追踪
        for name in var_names:
            v_disp, v_nd, v_unk, v_frag = self._trace_var(name)
            if v_disp:
                has_display = True
                display_sites.append(f'变量 {name}')
            if v_nd:
                has_nondisplay = True
                nondisplay_sites.append(f'变量 {name}')
            has_unknown = has_unknown or v_unk
            has_fragment = has_fragment or v_frag

        danger = has_nondisplay
        if has_unknown:
            reason = '出现点无法完全分类'
            if danger:
                reason += f'，且有非显示用途（{nondisplay_sites[0]}）'
            return UNKNOWN, reason, danger, sites, has_fragment
        if has_display and has_nondisplay:
            return UNKNOWN, (f'显示用途（{display_sites[0]}）与逻辑/资源用途'
                             f'（{nondisplay_sites[0]}）混合'), True, sites, has_fragment
        if has_display:
            return KEEP, f'显示上下文（{display_sites[0]}）', False, sites, has_fragment
        if has_nondisplay:
            return DROP, f'全部为非显示用途（{nondisplay_sites[0]}）', True, sites, has_fragment
        return UNKNOWN, '未找到任何出现点', False, sites, has_fragment

    # ========== 出现点上下文分类 ==========

    def _site_kind(self, line: str, start: int, end: int):
        """分类一个出现点，返回 ('display'|'nondisplay'|'format'|'fragment'|
        'assign'|None, 变量名)

        顺序有意义：显示白名单优先（textbutton "Save": 尾冒号是屏幕语法，
        不是 dict 键）；都不命中返回 (None, None) 表示未分类。
        """
        prefix_r = line[:start].rstrip()
        suffix_l = line[end:].lstrip()

        # -- 显示白名单 --
        if _DISPLAY_PREFIX_RE.search(prefix_r):
            return 'display', None
        if any(prefix_r.endswith(call) for call in _DISPLAY_CALLS):
            return 'display', None
        if _in_fstring_braces(line, start):
            # f-string 花括号内的字面量是被显示表达式的一部分
            return 'display', None

        # -- 非显示黑名单 --
        if _COMPARE_PREFIX_RE.search(prefix_r) or _COMPARE_SUFFIX_RE.match(suffix_l):
            return 'nondisplay', None
        if _MEMBER_SUFFIX_RE.match(suffix_l):
            return 'nondisplay', None
        if suffix_l.startswith(':'):
            # dict 键位（menu 选项不会被扫描为候选，见 embedded_strings）
            return 'nondisplay', None
        if prefix_r.endswith('['):
            # 索引 data["key"]（前面是标识符/括号）是键查找；
            # 容器字面量 ["a", "b"]（前面是 ( [ , = 或行首）不是
            before = prefix_r[:-1].rstrip()
            if before and (before[-1].isalnum() or before[-1] in '_])"\''):
                return 'nondisplay', None
            return None, None  # 容器元素：去向不明
        if _NONDISPLAY_STMT_RE.match(line.strip()):
            return 'nondisplay', None
        if any(prefix_r.endswith(call) for call in _NONDISPLAY_CALLS):
            return 'nondisplay', None

        # -- 拼接/格式化（归显示侧，但 apply 必须走 _() 包裹） --
        if suffix_l.startswith('.format(') or suffix_l.startswith('%'):
            return 'format', None
        if prefix_r.endswith('+') or suffix_l.startswith('+'):
            return 'fragment', None

        # -- 赋值点（可能触发变量追踪） --
        m = _ASSIGN_PREFIX_RE.match(line[:start])
        if m and not suffix_l:
            return 'assign', m.group(1)

        return None, None

    # ========== 变量流向追踪（BFS，深度上限 2） ==========

    def _trace_var(self, name: str, depth: int = 0,
                   visited: frozenset = frozenset()):
        """追踪变量使用去向，返回 (有显示, 有非显示, 有未知, 有拼接)

        保守原则：任何一跳无法分类即 unknown。可追踪的传递形态：
        重绑定 b = a（含 b = a + ...，记拼接）、容器成员
        （lst = [a] / lst.append(a) / for x in lst）、函数参数
        foo(a)（foo 在全项目唯一定义时）；多赋值（重绑/条件赋值）放弃。
        """
        if name in visited or depth > _MAX_TRACE_DEPTH:
            return False, False, True, False
        visited = visited | {name}

        assign_re = re.compile(
            r'^\s*(?:(?:define|default)\s+|\$\s*)?' + re.escape(name)
            + r'\s*=[^=]')
        word_re = re.compile(r'(?<![\w.])' + re.escape(name) + r'\b')
        assign_count = 0
        usages = []
        for rel, lines in self.files.items():
            for line_no, line in enumerate(lines, 1):
                if line.strip().startswith('#'):
                    continue
                if assign_re.match(line):
                    assign_count += 1
                    continue
                if word_re.search(line):
                    usages.append((rel, line_no, line))
        if assign_count > 1 or not usages:
            # 重绑/条件赋值去向不定；赋值后去向全无（注释/跨文件拼接等）
            return False, False, True, False

        return self._aggregate_usages(usages, name, visited, depth)

    def _aggregate_usages(self, usages: list, name: str,
                          visited: frozenset, depth: int):
        """把一组使用行聚合成 (有显示, 有非显示, 有未知, 有拼接)"""
        has_display = has_nondisplay = has_unknown = has_fragment = False
        for rel, line_no, line in usages:
            kind, target, frag = self._var_use_kind(line, name)
            if frag:
                has_fragment = True
            if kind == 'display':
                has_display = True
            elif kind == 'nondisplay':
                has_nondisplay = True
            elif kind in ('reassign', 'container', 'loop', 'param'):
                if depth >= _MAX_TRACE_DEPTH:
                    has_unknown = True
                    continue
                d, n, u, f = self._trace_hop(kind, target, visited, depth,
                                             name)
                has_display = has_display or d
                has_nondisplay = has_nondisplay or n
                has_unknown = has_unknown or u
                has_fragment = has_fragment or f
            else:
                has_unknown = True
        return has_display, has_nondisplay, has_unknown, has_fragment

    def _var_use_kind(self, line: str, name: str):
        """单个变量使用行的分类，返回 (kind, target, is_fragment)

        kind: display / nondisplay / reassign / container / loop / param /
        None（无法分类）。叶子分类（显示/危险）优先于传递形态。
        """
        esc = re.escape(name)
        stripped = line.strip()

        # -- 叶子：显示用途 --
        # [name] 前面是引号/行首 → 文本插值；前面是标识符/括号 → 索引取值
        for m in re.finditer(r'\[\s*' + esc + r'\s*\]', line):
            j = m.start() - 1
            if j >= 0 and (line[j].isalnum() or line[j] in '_])'):
                return 'nondisplay', None, False
            return 'display', None, False
        if re.search(r'\b(?:text|textbutton|tooltip)\s+' + esc + r'\b', line):
            return 'display', None, False
        if re.search(r'(?:renpy\.notify|notify|renpy\.input|Text|renpy\.say'
                     r'|renpy\.display_notify)\(\s*' + esc + r'\s*[),]', line):
            return 'display', None, False
        # call screen name / show screen name（screen 符号被渲染）
        if re.search(r'\b(?:call|show)\s+screen\s+' + esc + r'\b', line):
            return 'display', None, False

        # -- 叶子：非显示用途 --
        if re.search(esc + r'\s*(==|!=)|(?:==|!=)\s*' + esc
                     + r'|\b(?:if|while|elif)\s+' + esc + r'\s*:', line):
            return 'nondisplay', None, False

        # -- 传递形态 --
        # b = a（纯重绑定）
        m = re.match(r'^\s*(?:(?:define|default)\s+|\$\s*)?(\w+)\s*=\s*'
                     + esc + r'\s*$', line)
        if m:
            return 'reassign', m.group(1), False
        # b = a + ... / b = ... + a（拼接传递，记 fragment）
        m = re.match(r'^\s*(?:(?:define|default)\s+|\$\s*)?(\w+)\s*=\s*[^=]*'
                     + esc, line)
        if m and re.search(r'(' + esc + r'\s*\+|\+\s*' + esc + r')', line):
            return 'reassign', m.group(1), True
        # lst = [...a...] / d = {k: a}（容器字面量传递）
        m = re.match(r'^\s*(?:(?:define|default)\s+|\$\s*)?(\w+)\s*=\s*[\[{]',
                     line)
        if m:
            return 'container', m.group(1), False
        # lst.append(a) / lst.extend([a]) / lst.insert(0, a)
        m = re.search(r'\b(\w+)\s*\.\s*(?:append|extend|insert)\s*\(', line)
        if m:
            return 'container', m.group(1), False
        # for x in a:（循环变量传递）
        m = re.match(r'^\s*for\s+(\w+)\s+in\s+' + esc + r'\s*:', line)
        if m:
            return 'loop', m.group(1), False
        # foo(a)：函数参数传递（记录函数名与完整实参文本，可解析时追踪）
        m = re.search(r'\b(\w+)\s*\(([^)]*)\)', line)
        if m and re.search(r'(?<![\w.])' + esc + r'\b', m.group(2)):
            return 'param', (m.group(1), m.group(2)), False

        return None, None, False

    def _trace_hop(self, kind: str, target, visited: frozenset, depth: int,
                   name: str):
        """传递一跳：reassign/container/loop 直接追目标符号；param 先解析函数"""
        if kind == 'param':
            fname, arg_text = target
            return self._trace_param(fname, arg_text, name, visited, depth)
        return self._trace_var(target, depth + 1, visited)

    def _trace_param(self, fname: str, arg_text: str, name: str,
                     visited: frozenset, depth: int):
        """函数参数追踪：foo(a) 且 foo 全项目唯一定义时，把对应形参在
        函数体内的使用做同样的分类；多定义/找不到/参数形态复杂 → unknown"""
        def_re = re.compile(
            r'^(\s*)def\s+' + re.escape(fname) + r'\s*\(([^)]*)\)\s*:')
        found = None
        for rel, lines in self.files.items():
            for line_no, line in enumerate(lines, 1):
                m = def_re.match(line)
                if m:
                    if found is not None:
                        return False, False, True, False  # 多定义
                    found = (rel, line_no, m.group(2), len(m.group(1)))
        if found is None:
            return False, False, True, False
        rel, def_line, params_text, def_indent = found

        # 实参与形参按位置对齐（含关键字参数/嵌套括号则放弃）
        if any(ch in arg_text for ch in '([{='):
            return False, False, True, False
        args = [a.strip() for a in arg_text.split(',')]
        params = [p.split('=')[0].strip() for p in params_text.split(',')]
        pos = next((i for i, a in enumerate(args) if a == name), None)
        if pos is None or pos >= len(params) or not params[pos]:
            return False, False, True, False
        param = params[pos]

        # 函数体：def 行之后、缩进大于 def 的连续行
        body = []
        lines = self.files.get(rel, [])
        word_re = re.compile(r'(?<![\w.])' + re.escape(param) + r'\b')
        for line_no in range(def_line + 1, len(lines) + 1):
            line = lines[line_no - 1]
            if line.strip() and len(line) - len(line.lstrip()) <= def_indent:
                break
            if line.strip().startswith('#'):
                continue
            if word_re.search(line):
                body.append((rel, line_no, line))
        if not body:
            return False, False, True, False
        return self._aggregate_usages(body, param, visited | {param},
                                      depth + 1)

    # ========== 符号引用报告（精审 trace_symbol 工具的数据源） ==========

    def trace_symbol_report(self, name: str, cap: int = 50) -> dict:
        """符号（变量/screen/函数名）定义点与引用点分类报告

        回答精审的核心问题"这个符号最终流向哪里"：
        定义点（赋值/def/screen/label 声明）+ 全部引用行（按 _var_use_kind
        分类：display/nondisplay/reassign/container/loop/param/unknown）。
        """
        esc = re.escape(name)
        word_re = re.compile(r'(?<![\w.])' + esc + r'\b')
        decl_re = re.compile(
            r'^\s*(?:(?:define|default)\s+|\$\s*)?' + esc + r'\s*=[^=]'
            r'|^\s*(?:def|screen|label)\s+' + esc + r'\b')
        defs, refs = [], []
        for rel, lines in self.files.items():
            for line_no, line in enumerate(lines, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                if not word_re.search(line):
                    continue
                site = f'{rel}:{line_no}'
                if decl_re.match(line):
                    defs.append({'site': site, 'text': stripped[:120]})
                    continue
                kind, _, _ = self._var_use_kind(line, name)
                refs.append({'site': site, 'kind': kind or 'unknown',
                             'text': stripped[:120]})
        truncated = len(refs) > cap
        return {'name': name, 'definitions': defs[:cap],
                'references': refs[:cap], 'truncated': truncated}
