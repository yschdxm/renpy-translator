"""内嵌文本提取：找出游戏中未包 _() 的可显示字符串，并原位包上 _()

未包 _() 的字符串完全绕过 Ren'Py 翻译系统（SDK 模板提取不到）。
本模块提供：
- find_candidates: 启发式扫描候选字符串（A类=屏幕语言，B类=python 内嵌）
- apply_wrapping: 把勾选的候选在原位置包成 _(...)，SDK 重生成模板后即可入库翻译

已知边界：
- define 期求值的数据（字典/列表字面量）在定义时定值，游戏中途切语言不更新
- 动态拼接字符串不提取（无法整体标记）
"""

import re
from dataclasses import dataclass
from pathlib import Path

_EXCLUDE_DIRS = {'renpy', 'lib', 'saves', 'cache', 'tl', 'audio', 'sound',
                 'images', 'image', 'fonts', 'font', 'video', 'movies'}

# 资源扩展名（路径样字符串过滤）
_RESOURCE_EXTS = {
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.ogg', '.mp3', '.wav', '.opus',
    '.ttf', '.otf', '.ttc', '.rpy', '.rpyc', '.rpym', '.rpa', '.json', '.webm',
    '.mp4', '.avi', '.mkv', '.txt', '.rpymc',
}

# A 类：屏幕语言裸字符串（textbutton/text/label/tooltip，未包 _()）
_SCREEN_STRING_RE = re.compile(
    r'\b(textbutton|text|label|tooltip)\s+("(?:[^"\\]|\\.)*?")'
)

# B 类：python 字符串字面量
_PY_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*?"|\'(?:[^\'\\]|\\.)*?\'')

# 单行闭合的三引号字面量（其中的字符串不可标记，否则破坏语法）
_TRIPLE_RE = re.compile(
    r'"""(?:[^"\\]|\\.|"(?!""))*?"""|\'\'\'(?:[^\'\\]|\\.|\'(?!\'\'))*?\'\'\''
)

# python 上下文识别
_PY_BLOCK_START_RE = re.compile(r'^(init\s+(-?\d+\s+)?)?python\b.*:')
_INIT_BLOCK_RE = re.compile(r'^init\s+(-?\d+\s+)?:')
_DEFINE_RE = re.compile(r'^(define|default)\s+[\w.]+\s*=\s*(.*)$')
_SCOPE_PATTERNS = [
    (re.compile(r'^screen\s+(\w+)'), 'screen'),
    (re.compile(r'^label\s+(\w+)\s*:'), 'label'),
]

_SCOPE_KIND_NAMES = {'screen': '界面', 'label': '场景'}


@dataclass
class Candidate:
    """一个可提取的内嵌字符串候选"""
    file: str          # 绝对路径
    rel_file: str      # 相对项目 game/ 的路径（展示用）
    line: int          # 1-based 行号
    col_start: int     # 行内列偏移：带引号字面量起点
    col_end: int       # 终点（不含）
    raw: str           # 原始带引号字面量（如 "Messages"，含引号）
    text: str          # 反转义后的原文
    kind: str          # 'screen' | 'python'
    hint: str          # 出处描述
    confidence: str    # 'high' | 'low'（启发式）
    ai_keep: object = None   # AI 预筛结果：True/False/None(未筛或未决)
    ai_confident: bool = True  # AI 自评置信度（False 时进入精审）
    ai_reason: str = ''


def _unescape(s: str) -> str:
    """Ren'Py/Python 字符串反转义"""
    return (s.replace('\\"', '"').replace("\\'", "'")
             .replace('\\n', '\n').replace('\\t', '\t')
             .replace('\\\\', '\\'))


def _is_noise(text: str, kind: str) -> bool:
    """判断是否噪音字符串（不可能是玩家可见文本）"""
    if len(text) < 2:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    # 颜色码
    if re.fullmatch(r'#[0-9a-fA-F]{3,8}', stripped):
        return True
    # 路径样：含路径分隔符或以资源扩展名结尾
    if '/' in stripped or '\\' in stripped:
        return True
    if any(stripped.lower().endswith(ext) for ext in _RESOURCE_EXTS):
        return True
    # 纯格式串（%%、%s、%m 等 strftime/代码格式）
    if re.fullmatch(r'(%%|%[a-zA-Z%]|\W)+', stripped):
        return True
    # 不含任何字母（纯符号/数字：▶、❮、123、—— 等）
    if not re.search(r'[a-zA-Z一-鿿]', stripped):
        return True
    # @ 开头（推特 handle 等）
    if stripped.startswith('@'):
        return True
    # （含字母或非 ASCII 字符如 emoji/CJK 的保留，如 "💰 [money] $"）
    remainder = re.sub(r'\[[^\]]*\]', '', stripped)
    if not remainder or not re.search(r'[a-zA-Z]|[^\x00-\x7f]', remainder):
        return True
        return True
    # B 类中，纯小写标识符/蛇形命名大概率是键名而非文本
    if kind == 'python' and re.fullmatch(r'[a-z_][a-z0-9_]*', stripped):
        return True
    return False


def _confidence(text: str, kind: str) -> str:
    """置信度：A 类屏幕文本默认高；B 类要求更强的文本信号"""
    if kind == 'screen':
        return 'high'
    words = text.split()
    has_sentence_punct = bool(re.search(r'[.!?…。！？]', text))
    if len(words) >= 3 or (len(words) >= 2 and has_sentence_punct):
        return 'high'
    return 'low'


def find_candidates(game_dir: str) -> list:
    """扫描游戏目录，找出未包 _() 的可显示字符串候选

    Args:
        game_dir: 项目游戏目录（其下应有 game/ 子目录）
    Returns:
        list[Candidate]，按文件、行、列排序
    """
    game_path = Path(game_dir)
    roots = [p for p in (game_path / 'game', game_path) if p.exists()]
    base = game_path / 'game' if (game_path / 'game').exists() else game_path

    candidates = []
    seen_files = set()
    for root in roots:
        for rpy_file in sorted(root.rglob('*.rpy')):
            if rpy_file in seen_files:
                continue
            seen_files.add(rpy_file)
            if _EXCLUDE_DIRS & set(rpy_file.parts):
                continue
            try:
                content = rpy_file.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            rel = rpy_file.relative_to(base).as_posix()
            _scan_file(str(rpy_file), rel, content, candidates)

    candidates.sort(key=lambda c: (c.rel_file, c.line, c.col_start))
    return candidates


def _scan_file(file_path: str, rel_file: str, content: str, out: list):
    """扫描单个 .rpy 文件，把候选写入 out"""
    scope_stack = []      # [(indent, kind, name)]，screen/label 作用域
    py_indent = None      # python 块的内容缩进（None=不在 python 块中）
    define_balance = 0    # define/default 表达式的括号余额（>0 表示跨行继续）
    triple_quote = None   # 三引号块状态（''' 或 \"\"\"）
    all_lines = content.split('\n')

    for line_no, raw_line in enumerate(all_lines, 1):
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip())

        # 三引号块：跳过整块（docstring/多行字符串，不做标记）
        if triple_quote:
            if triple_quote in raw_line:
                triple_quote = None
            continue

        # 弹出缩进不大于当前行的作用域 / python 块
        if stripped:
            while scope_stack and indent <= scope_stack[-1][0]:
                scope_stack.pop()
            if py_indent is not None and indent <= py_indent:
                py_indent = None

        if not stripped or stripped.startswith('#'):
            continue

        # 作用域定义行
        for pattern, kind in _SCOPE_PATTERNS:
            m = re.match(pattern, stripped)
            if m:
                scope_stack.append((indent, kind, m.group(1)))
                break

        scope_name = scope_stack[-1][2] if scope_stack else ''
        scope_kind = scope_stack[-1][1] if scope_stack else ''

        def make_hint(kind_name):
            if scope_name:
                suffix = _SCOPE_KIND_NAMES.get(scope_kind, scope_kind)
                return f'{scope_name}{suffix}·{kind_name}'
            return kind_name

        # menu 选项行（"选项" :）不会命中 A 类关键词、也不在 python 上下文中，
        # 自然不会被提取，无需特判（曾因此误杀 dict 的 "键": 行）

        # ---- A 类：屏幕语言裸字符串 ----
        for m in _SCREEN_STRING_RE.finditer(raw_line):
            raw_literal = m.group(2)
            text = _unescape(raw_literal[1:-1])
            if _is_noise(text, 'screen'):
                continue
            kind_name = {'textbutton': '按钮', 'text': '界面文本',
                         'label': '标题', 'tooltip': '提示'}[m.group(1)]
            out.append(Candidate(
                file=file_path, rel_file=rel_file, line=line_no,
                col_start=m.start(2), col_end=m.end(2),
                raw=raw_literal, text=text, kind='screen',
                hint=make_hint(kind_name),
                confidence=_confidence(text, 'screen'),
            ))

        # ---- B 类：python 上下文字符串字面量 ----
        # 判定当前行是否在 python 上下文
        in_python = False
        if py_indent is not None:
            in_python = True
        if define_balance > 0:
            in_python = True
        if stripped.startswith('$'):
            in_python = True

        block_m = _PY_BLOCK_START_RE.match(stripped)
        init_m = _INIT_BLOCK_RE.match(stripped) if not block_m else None
        define_m = _DEFINE_RE.match(stripped)

        if block_m or init_m:
            py_indent = indent
            continue  # 块声明行本身无候选
        if define_m:
            in_python = True
            # Character/DynamicCharacter 定义行（允许括号前有空格）：
            # 名字走人名管线，不在这里标记
            if re.search(r'\bCharacter\s*\(', stripped):
                define_balance = 0
                continue

        if in_python:
            # 三引号起点（单行闭合的不算）
            for tq in ('"""', "'''"):
                if raw_line.count(tq) % 2 == 1:
                    triple_quote = tq

            # 单行三引号字面量区间：其中的内容不是可标记的字符串
            triple_spans = [m.span() for m in _TRIPLE_RE.finditer(raw_line)]

            for m in _PY_STRING_RE.finditer(raw_line):
                if any(s <= m.start() and m.end() <= e for s, e in triple_spans):
                    continue
                raw_literal = m.group(0)
                # 已被 _() 包裹的跳过（重复运行安全）
                prefix = raw_line[max(0, m.start() - 3):m.start()]
                if prefix.endswith('_('):
                    continue
                # 隐式拼接链中的字面量跳过（单独包 _() 会破坏语法）：
                # a) 字面量后跟 \ 行继续符  b) 字面量后紧跟另一个引号
                # c) 前一个字面量紧邻本字面量（同行相邻拼接）
                after = raw_line[m.end():]
                if after.rstrip().endswith('\\'):
                    continue
                if after.lstrip().startswith(('"', "'")):
                    continue
                before = raw_line[:m.start()].rstrip()
                # 仅空白分隔的相邻字面量才是拼接；("r", "text") 这类带逗号的不算
                if re.search(r'["\']\s*$', raw_line[:m.start()]):
                    continue
                # d) 括号内跨行拼接：字面量孤悬行尾且下一行以引号开头
                if not after.strip() and line_no < len(all_lines):
                    nxt = all_lines[line_no].lstrip()  # line_no 即下一行下标(1-based 对齐)
                    if nxt.startswith(('"', "'")):
                        continue
                # e) 拼接链的续行：本行以引号开头（且不是 dict 键），
                #    上一非空行以 \ 或引号结尾
                if not raw_line[:m.start()].strip() and not after.lstrip().startswith(':'):
                    j = line_no - 2  # 上一行下标（0-based）
                    while j >= 0 and not all_lines[j].strip():
                        j -= 1
                    if j >= 0:
                        prev = all_lines[j].rstrip()
                        if prev.endswith('\\') or prev.endswith(('"', "'")):
                            continue
                quote = raw_literal[0]
                text = _unescape(raw_literal[1:-1])
                if _is_noise(text, 'python'):
                    continue
                out.append(Candidate(
                    file=file_path, rel_file=rel_file, line=line_no,
                    col_start=m.start(), col_end=m.end(),
                    raw=raw_literal, text=text, kind='python',
                    hint=make_hint('脚本文本'),
                    confidence=_confidence(text, 'python'),
                ))

        # define/default 跨行表达式：跟踪括号余额
        if define_m or define_balance > 0:
            define_balance += raw_line.count('{') + raw_line.count('[') + raw_line.count('(')
            define_balance -= raw_line.count('}') + raw_line.count(']') + raw_line.count(')')
            if define_balance < 0:
                define_balance = 0


def apply_wrapping(candidates: list) -> tuple:
    """把候选字符串在原位置包成 _(...)

    按文件分组后从文件末尾向开头替换（保持列偏移不失效）；
    替换前校验目标位置确实是期望的字面量（源码被改动过时跳过）。

    Returns:
        (成功数, 跳过数)
    """
    by_file = {}
    for c in candidates:
        by_file.setdefault(c.file, []).append(c)

    wrapped = skipped = 0
    for file_path, cands in by_file.items():
        path = Path(file_path)
        try:
            lines = path.read_text(encoding='utf-8').split('\n')
        except OSError:
            skipped += len(cands)
            continue

        changed = False
        for c in sorted(cands, key=lambda x: (x.line, x.col_start), reverse=True):
            idx = c.line - 1
            if idx >= len(lines):
                skipped += 1
                continue
            line = lines[idx]
            if line[c.col_start:c.col_end] != c.raw:
                skipped += 1
                continue
            lines[idx] = line[:c.col_start] + '_(' + c.raw + ')' + line[c.col_end:]
            wrapped += 1
            changed = True

        if changed:
            path.write_text('\n'.join(lines), encoding='utf-8')

    return wrapped, skipped
