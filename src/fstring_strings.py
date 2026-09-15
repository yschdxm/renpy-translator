# -*- coding: utf-8 -*-
"""f-string 动态文本翻译：AST 模板化扫描 + 导出时改写

问题：Ren'Py 翻译查找（strings 表渲染时查整串、_() 查参数值）都是精确
匹配，f-string 的值在到达翻译系统之前就已拼接完成，永远查不中。

解法：模板与插值分离，单一急切形态（两个历史教训：形态 A 惰性
_("[expr!t]") 在"赋值后流转"场景把括号原样漏出；`_()` 在 Ren'Py 是
**恒等函数**（minstore.py: `def _(s): return s`，翻译留给显示层），
数据层要拿译文必须用 `__()`（= translate_string，minstore.py:54）：

    f"...{expr}..." → __("...{0}...").format(__(expr))

__() 在构造时刻查静态模板（zz 表 old 条目）得中文模板，.format 拼值；
实参包 __() 让值在数据层现译（__() 与 strings 表共用存储；非字符串值
引擎内 str() 后查不中即原样）。构造产物是全中文串，显示层二次查表
必然查不中（old 全是英文），无双重翻译风险。

define/default 行的 f-string 跳过（init 期求值时语言设置可能未就位，
__() 会把英文烤进默认值）——init python 块里几乎全是 def 定义，
函数体运行时才执行，不在此列。

扫描期用 AST 预生成模板写进 Candidate.text（zz 表 old 条目的文本）；
导出时从 raw 现派生模板/参数做改写，并与 DB 模板一致性校验
（不一致=源码/扫描口径变了，跳过+告警）。全部改写只在导出副本发生。

v1 边界：单行、纯 f 前缀（rf/rb/fr/三引号/多行由扫描器跳过）。
"""
import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from embedded_strings import (Candidate, _EXCLUDE_DIRS, _PY_STRING_RE,
                              _PY_BLOCK_START_RE, _RESOURCE_EXTS,
                              _string_prefix)
from markup_check import add_tflag


# ---------------------------------------------------------------- 解析

@dataclass
class Fmt:
    """一个 f-string 的模板化解析结果"""
    form: str            # 恒 'B'（单一急切形态；保留字段供 hint 兼容）
    template: str        # 翻译模板（{i}/{i!r}/{i:spec}，字面花括号双写）
    args: list = field(default_factory=list)   # 插值表达式源码（按序）


def parse_fstring(raw: str, in_def: bool = False):
    """解析单行 f-string 字面量（含 f 前缀），返回 Fmt 或 None（跳过）

    raw: 'f"..."' 形式的源码文本（单/双引号均可）。
    跳过：ast 解析失败、无插值。
    in_def: 保留参数（兼容旧调用），急切形态与作用域无关。
    """
    try:
        tree = ast.parse(raw, mode='eval')
    except (SyntaxError, ValueError):
        return None
    if not isinstance(tree.body, ast.JoinedStr):
        return None

    parts = []   # ('lit', text) | ('fmt', idx)
    args = []    # (expr_src, conv, spec)
    for node in tree.body.values:
        if isinstance(node, ast.Constant):
            parts.append(('lit', str(node.value)))
        elif isinstance(node, ast.FormattedValue):
            expr = ast.get_source_segment(raw, node.value)
            if expr is None:
                return None
            conv = ''
            if node.conversion in (114, 115, 97):   # !r !s !a
                conv = chr(node.conversion)
            spec = ''
            if node.format_spec is not None:
                spec_text = ast.get_source_segment(raw, node.format_spec)
                if spec_text is None:
                    return None
                # format_spec 节点的源码段可能带上前导冒号（实现相关）
                spec = (spec_text[1:] if spec_text.startswith(':')
                        else spec_text)
                # 嵌套替换字段（{x:{w}}）：format 支持 {0:{1}}，
                # 但作为参数表达式重排太绕，v1 保守跳过
                if any(isinstance(n, ast.FormattedValue)
                       for n in ast.walk(node.format_spec)):
                    return None
            args.append((expr, conv, spec))
            parts.append(('fmt', len(args) - 1))
    if not args:
        return None   # 无插值：静态 f-string，不是本管线对象

    template = ''
    for p in parts:
        if p[0] == 'lit':
            # f-string 的 {{ 与 .format 的转义一致——ast 已单反，重新双写
            # 字面片段里的 Ren'Py 插值 [x] 补 !t：f-string 急切求值后，
            # 拼好的值里的 [x] 仍会在显示层替换——值通道靠 !t 过表
            # （f"[energy_label]: {...}" 字面片段的 [energy_label] 不加 !t
            # 时 energy_label 的值"Energy"原样插入=英文）
            template += add_tflag(
                p[1].replace('{', '{{').replace('}', '}}'))
        else:
            _expr, conv, spec = args[p[1]]
            template += ('{' + str(p[1])
                         + (('!' + conv) if conv else '')
                         + ((':' + spec) if spec else '') + '}')
    return Fmt(form='B', template=template, args=[a[0] for a in args])


def _literal_is_translatable(literal_text: str) -> bool:
    """字面部分（插值之外的静态文本）是否值得翻译"""
    # 先剥 {size=}{/size} 等 Ren'Py 标签——标签里的 / 会误判为路径，
    # 且全标签的静态内容不该被字母数误杀
    stripped = re.sub(r'\{\{|\}\}|\{[^}]*\}', '', literal_text).strip()
    # 至少两个字母/汉字（纯插值、纯标点的 f-string 无可译内容）
    if len(re.findall(r'[a-zA-Z一-鿿]', stripped)) < 2:
        return False
    # 路径样：含路径分隔符或以资源扩展名结尾
    if '/' in stripped or '\\' in stripped:
        return False
    if any(stripped.lower().endswith(ext) for ext in _RESOURCE_EXTS):
        return False
    return True


# ---------------------------------------------------------------- 扫描

_DEF_RE = re.compile(r'^def\s+\w+')


def _in_screen_prop(raw_line: str) -> bool:
    """screen 属性行的 f-string（text/textbutton/label/tooltip f"..."）"""
    return bool(re.search(
        r'\b(textbutton|text|label|tooltip)\s+f["\']', raw_line))


def find_fstring_candidates(game_dir: str) -> list:
    """扫描含插值的单行 f-string 候选（kind='fstring'）

    常规扫描器（_PY_STRING_RE 前缀跳过）刻意不含前缀字面量——
    本 pass 独立处理。.rpy 与 *_ren.py（纯 Python 模块，LR2 的数据
    定义层）都扫：_ren.py 里全是 python 上下文。返回按文件、行、
    列排序的 Candidate。
    """
    game_path = Path(game_dir)
    roots = [p for p in (game_path / 'game', game_path) if p.exists()]
    base = game_path / 'game' if (game_path / 'game').exists() else game_path

    candidates = []
    seen_files = set()
    for root in roots:
        for pattern in ('*.rpy', '*_ren.py'):
            for src_file in sorted(root.rglob(pattern)):
                if src_file in seen_files:
                    continue
                seen_files.add(src_file)
                if _EXCLUDE_DIRS & set(src_file.parts):
                    continue
                try:
                    content = src_file.read_text(encoding='utf-8',
                                                 errors='ignore')
                except OSError:
                    continue
                try:
                    rel = src_file.relative_to(base).as_posix()
                except ValueError:
                    rel = src_file.relative_to(game_path).as_posix()
                _scan_fstring_file(str(src_file), rel, content, candidates,
                                   in_python_always=(pattern == '*_ren.py'))

    candidates.sort(key=lambda c: (c.rel_file, c.line, c.col_start))
    return candidates


def _scan_fstring_file(file_path: str, rel_file: str, content: str,
                       out: list, in_python_always: bool = False):
    py_indent = None
    triple_quote = None
    screen_indent = None   # screen 块缩进（screen 内表达式行的 f-string 也扫——
                           # 动作列表 SetScreenVariable("x", f"...") 等；
                           # label 不放宽：menu 选项是翻译 id 哈希敏感区）
    all_lines = content.split('\n')

    for line_no, raw_line in enumerate(all_lines, 1):
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip())

        if triple_quote:
            if triple_quote in raw_line:
                triple_quote = None
            continue

        if stripped:
            if py_indent is not None and indent <= py_indent:
                py_indent = None
            if screen_indent is not None and indent <= screen_indent:
                screen_indent = None
        if not stripped or stripped.startswith('#'):
            continue

        in_python = (in_python_always or py_indent is not None
                     or stripped.startswith('$'))
        block_m = _PY_BLOCK_START_RE.match(stripped)
        if block_m:
            py_indent = indent
            continue
        if re.match(r'^screen\s+\w+', stripped):
            screen_indent = indent
            continue
        # screen 属性行（text f"..."）不在 python 上下文也扫；
        # screen 块内的表达式行（含动作列表）同样扫
        if (not in_python and screen_indent is None
                and not _in_screen_prop(raw_line)):
            continue
        # define/default 行（init 期求值）跳过：__() 立即翻译会在语言
        # 设置就位前把英文烤进默认值
        if re.match(r'^(define|default)\s', stripped):
            continue

        for m in _PY_STRING_RE.finditer(raw_line):
            prefix = _string_prefix(raw_line, m.start())
            if prefix.lower() != 'f':
                continue
            # 已被 _() 包裹（重复运行安全）
            col_start = m.start() - len(prefix)
            before = raw_line[:col_start]
            if before.rstrip().endswith('_('):
                continue
            raw = raw_line[col_start:m.end()]
            # 隐式拼接守卫（与 B 类扫描同一套：单独改写会破坏语法）
            after = raw_line[m.end():]
            if after.rstrip().endswith('\\'):
                continue
            if after.lstrip().startswith(('"', "'")):
                continue
            if re.search(r'["\']\s*$', before):
                continue

            fmt = parse_fstring(raw)
            if fmt is None:
                continue
            # 可译性判定：去掉 {N} 占位后，静态部分（含 Ren'Py 插值的
            # 字面片段）须值得翻译。两个放宽：
            # - 字面片段含 Ren'Py 插值（[energy_label]）→ !t 值通道需要
            # - 多参数字符串（{0} {1} 形态）→ 参数现译通道需要
            # （f"{trait.name} {get_trait_tags(trait)}" 静态只有一个空格，
            # 但不变换 trait.name 永远英文）
            static_part = re.sub(r'\{\d+\}', '', fmt.template)
            # 含 [ 的字面片段都算值通道候选：f"[{a_tag}]" 的括号是字面
            # 文本、参数是显示值（rt_translate 现译），f"[energy_label]"
            # 的插值要 !t——两者都需要这条候选
            has_renpy_interp = '[' in static_part
            # 静态内容全是文本标签（{{color=...}}{0}{{/color}}）也算：
            # 参数是显示值（get_label_tag 的彩色标签 f-string）
            has_text_tag = '{' in static_part
            if (not _literal_is_translatable(static_part)
                    and not has_renpy_interp and not has_text_tag
                    and len(fmt.args) < 2):
                continue

            out.append(Candidate(
                file=file_path, rel_file=rel_file, line=line_no,
                col_start=col_start, col_end=m.end(),
                raw=raw, text=fmt.template, kind='fstring',
                hint='f-string模板',
                confidence='low',
            ))


# ---------------------------------------------------------------- 导出改写

def build_replacement(fmt: Fmt, quote: str) -> str:
    """模板 → 替换源码文本：__("模板").format(rt_translate(arg1), ...)

    __() = translate_string（立即翻译；_() 是恒等函数——显示层查的是
    拼好的整串，模板/实参都必须在这里于数据层现译，否则全英文）。
    实参用 rt_translate（导出注入的存储助手，非字符串原样返回）——
    __() 直调 translate_string，内部 re.sub 收到 float/int 即 TypeError
    （显示层替换式会先 str()，直调不防护）。
    命名不能以下划线开头：屏幕代码里 `_name` 会被 Ren'Py 改编成
    屏幕局部名而 NameError。"""
    esc = (fmt.template
           .replace('\\', '\\\\')
           .replace('\n', '\\n')
           .replace(quote, '\\' + quote))
    lit = quote + esc + quote
    args = ', '.join('rt_translate(' + a + ')' for a in fmt.args)
    return '__(' + lit + ').format(' + args + ')'


def transform_fstring_literals(rows: list, source_root: str) -> tuple:
    """导出副本上把 f-string 候选改写为模板翻译形态

    定位纪律与 relocate_wrap_candidates 一致：记录位置校验 raw →
    漂移则同文件搜唯一/最近匹配（返回 moved 供调用方回写坐标）→
    按文件分组倒序替换。替换内容从 raw 现派生并与 DB 模板（row['text']）
    一致性校验——不一致说明源码/扫描口径已变，跳过+告警。

    返回 (done, moved_ids, lost_ids)：lost 含定位失败与派生失败。
    """
    source_root = Path(source_root)
    done = 0
    moved_ids = []
    lost_ids = []
    file_cache: dict = {}

    def _lines(rel):
        if rel not in file_cache:
            try:
                file_cache[rel] = (source_root / rel).read_text(
                    encoding='utf-8').split('\n')
            except OSError:
                file_cache[rel] = None
        return file_cache[rel]

    # 先按文件分组并逐行定位（raw 校验 → 同文件重定位）
    located = []   # (row, rel, ln, col, fmt)
    for r in rows:
        lines = _lines(r['rel_file'])
        if lines is None:
            lost_ids.append(r['id'])
            continue
        hits = []
        idx = r['line'] - 1
        if (0 <= idx < len(lines)
                and lines[idx][r['col_start']:r['col_start'] + len(r['raw'])]
                == r['raw']):
            hits.append((r['line'], r['col_start']))
        else:
            for ln, line in enumerate(lines, 1):
                start = 0
                while True:
                    col = line.find(r['raw'], start)
                    if col < 0:
                        break
                    hits.append((ln, col))
                    start = col + 1
        if not hits:
            lost_ids.append(r['id'])
            continue
        if len(hits) > 1:
            hits.sort(key=lambda h: abs(h[0] - r['line']))
        ln, col = hits[0]

        # 从 raw 现派生，与 DB 模板一致性校验
        fmt = parse_fstring(r['raw'])
        if fmt is None or fmt.template != r['text']:
            lost_ids.append(r['id'])
            continue
        located.append((r, ln, col, fmt))
        if (ln, col) != (r['line'], r['col_start']):
            moved_ids.append((r['id'], ln, col))

    # 按文件分组倒序替换
    by_file: dict = {}
    for item in located:
        by_file.setdefault(item[0]['rel_file'], []).append(item)
    for rel, items in by_file.items():
        lines = _lines(rel)
        changed = False
        for r, ln, col, fmt in sorted(items, key=lambda x: (x[1], x[2]),
                                      reverse=True):
            idx = ln - 1
            line = lines[idx]
            if line[col:col + len(r['raw'])] != r['raw']:
                lost_ids.append(r['id'])
                continue
            quote = r['raw'][-1]
            repl = build_replacement(fmt, quote)
            lines[idx] = line[:col] + repl + line[col + len(r['raw']):]
            done += 1
            changed = True
        if changed:
            (source_root / rel).write_text('\n'.join(lines), encoding='utf-8')

    return done, moved_ids, lost_ids
