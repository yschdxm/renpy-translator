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

变量流向只追踪 SSA 式简单情形（全项目单次赋值、无重绑）；
多赋值/条件赋值/跨容器传递一律 unknown。
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
    'renpy.say(', 'renpy.display_notify(',
)

# 非显示用途（黑名单）：前缀/语句匹配
_NONDISPLAY_CALLS = (
    'renpy.has_image(', 'renpy.image(', 'renpy.show(', 'renpy.hide(',
    'renpy.scene(', 'renpy.jump(', 'renpy.call(', 'renpy.call_screen(',
    'renpy.show_screen(', 'renpy.has_label(', 'renpy.loadable(',
    'renpy.file(', 'renpy.music.play(', 'renpy.music.queue(',
    'renpy.sound.play(', 'renpy.sound.queue(', 'open(', 'os.path.join(',
    '.startswith(', '.endswith(', '.get(', '.pop(', '.setdefault(',
    'style ', 'style=',
)
_NONDISPLAY_STMT_RE = re.compile(
    r'^(play|queue|scene|show|hide|jump|call|image|voice)\s')
_COMPARE_PREFIX_RE = re.compile(r'(==|!=)\s*$')
_COMPARE_SUFFIX_RE = re.compile(r'^(==|!=)')
_MEMBER_SUFFIX_RE = re.compile(r'^(not\s+)?in\b')

# 赋值点：define/default/普通赋值，右侧恰好是本字面量
_ASSIGN_PREFIX_RE = re.compile(
    r'^(?:\s*(?:define|default)\s+)?\s*([\w.]+)\s*=\s*$')


class UsageAnalyzer:
    """对一批候选做静态用途分流"""

    def __init__(self, game_root: str):
        """game_root: 与 find_candidates 的 rel_file 基准一致的源码根目录"""
        self.root = Path(game_root)
        # rel_path -> 行列表（排除引擎/资源目录与注释行在查找时处理）
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

    def classify_all(self, candidates: list):
        """批量分流，结果写回候选的 static_verdict / static_reason / static_danger"""
        occ_map = self._find_all_occurrences(candidates)
        for i, c in enumerate(candidates):
            verdict, reason, danger = self._classify_one(occ_map.get(i, []))
            c.static_verdict = verdict
            c.static_reason = reason
            c.static_danger = danger

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
        """返回 (verdict, reason, danger)"""
        has_display = has_nondisplay = has_unknown = False
        display_sites, nondisplay_sites = [], []
        var_names = set()

        for rel, line_no, line, start, end in occs:
            kind, var = self._site_kind(line, start, end)
            site = f'{rel}:{line_no}'
            if kind == 'display':
                has_display = True
                display_sites.append(site)
            elif kind == 'nondisplay':
                has_nondisplay = True
                nondisplay_sites.append(site)
            elif kind == 'assign':
                var_names.add(var)
            else:
                has_unknown = True

        # 赋值点触发变量流向追踪
        for name in var_names:
            v_disp, v_nd, v_unk = self._trace_var(name)
            if v_disp:
                has_display = True
                display_sites.append(f'变量 {name}')
            if v_nd:
                has_nondisplay = True
                nondisplay_sites.append(f'变量 {name}')
            has_unknown = has_unknown or v_unk

        danger = has_nondisplay
        if has_unknown:
            reason = '出现点无法完全分类'
            if danger:
                reason += f'，且有非显示用途（{nondisplay_sites[0]}）'
            return UNKNOWN, reason, danger
        if has_display and has_nondisplay:
            return UNKNOWN, (f'显示用途（{display_sites[0]}）与逻辑/资源用途'
                             f'（{nondisplay_sites[0]}）混合'), True
        if has_display:
            return KEEP, f'显示上下文（{display_sites[0]}）', False
        if has_nondisplay:
            return DROP, f'全部为非显示用途（{nondisplay_sites[0]}）', True
        return UNKNOWN, '未找到任何出现点', False

    # ========== 出现点上下文分类 ==========

    def _site_kind(self, line: str, start: int, end: int):
        """分类一个出现点，返回 ('display'|'nondisplay'|'assign'|None, 变量名)

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

        # -- 赋值点（可能触发变量追踪） --
        m = _ASSIGN_PREFIX_RE.match(line[:start])
        if m and not suffix_l:
            return 'assign', m.group(1)

        return None, None

    # ========== 变量流向追踪（仅 SSA 式简单情形） ==========

    def _trace_var(self, name: str):
        """追踪单赋值变量的使用去向，返回 (有显示, 有非显示, 有未知)"""
        # 全项目赋值次数：>1（重绑/条件赋值）则放弃追踪
        assign_re = re.compile(
            r'^\s*(?:(?:define|default)\s+)?' + re.escape(name) + r'\s*=[^=]')
        assign_count = 0
        word_re = re.compile(r'(?<![\w.])' + re.escape(name) + r'\b')
        usages = []
        for rel, lines in self.files.items():
            for line_no, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                if assign_re.match(line):
                    assign_count += 1
                    continue
                if word_re.search(line):
                    usages.append((rel, line_no, line))
        if assign_count > 1:
            return False, False, True

        has_display = has_nondisplay = has_unknown = False
        bracket_re = re.compile(r'\[\s*' + re.escape(name) + r'\s*\]')
        disp_use_re = re.compile(
            r'\b(?:text|textbutton|tooltip)\s+' + re.escape(name) + r'\b'
            r'|(?:renpy\.notify|notify|renpy\.input|Text)\(\s*'
            + re.escape(name) + r'\s*[),]')
        danger_re = re.compile(
            re.escape(name) + r'\s*(==|!=)|(?:==|!=)\s*' + re.escape(name)
            + r'|\b(?:if|while|elif)\s+' + re.escape(name) + r'\s*:')
        for rel, line_no, line in usages:
            interp = index = False
            for m in bracket_re.finditer(line):
                j = m.start() - 1
                # [name] 前面是标识符/括号 → 索引取值；
                # 前面是引号说明在字符串内部 → 文本插值
                if j >= 0 and (line[j].isalnum() or line[j] in '_])'):
                    index = True
                else:
                    interp = True
            if index or danger_re.search(line):
                has_nondisplay = True
            elif disp_use_re.search(line) or interp:
                has_display = True
            else:
                has_unknown = True
        if not usages:
            has_unknown = True  # 赋值后去向全无（注释/跨文件拼接等）
        return has_display, has_nondisplay, has_unknown
