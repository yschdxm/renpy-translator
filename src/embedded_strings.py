"""内嵌文本提取：找出游戏中未包 _() 的可显示字符串，并原位包上 _()

未包 _() 的字符串完全绕过 Ren'Py 翻译系统（SDK 模板提取不到）。
本模块提供：
- find_candidates: 启发式扫描候选字符串（A类=屏幕语言，B类=python 内嵌）
- apply_wrapping: 把候选在原位置包成 _(...)——源码只读化后只在导出
  副本上调用（GameExporter._apply_marked_wraps），工作副本永不被写入
- relocate_wrap_candidates: 导出时按记录位置校验 + 同文件内容重定位
  （标记/更新都不改源码，行号可能漂移）

已知边界：
- define 期求值的数据（字典/列表字面量）在定义时定值，游戏中途切语言不更新
- 动态拼接字符串不提取（无法整体标记）
"""

import re
from dataclasses import dataclass
from pathlib import Path

from markup_check import add_tflag

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
    r'\b(textbutton|text|label|tooltip)\s+'
    r'("(?:[^"\\]|\\.)*?"|\'(?:[^\'\\]|\\.)*?\')'
)

# B 类：python 字符串字面量
_PY_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*?"|\'(?:[^\'\\]|\\.)*?\'')

# 单行闭合的三引号字面量（其中的字符串不可标记，否则破坏语法）
_TRIPLE_RE = re.compile(
    r'"""(?:[^"\\]|\\.|"(?!""))*?"""|\'\'\'(?:[^\'\\]|\\.|\'(?!\'\'))*?\'\'\''
)

# screen 参数默认值：screen stats(title="Player", sub='x')
_KWARG_STR_RE = re.compile(
    r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*?"|\'(?:[^\'\\]|\\.)*?\')')

# screen 作用域内的 Notify("...") 动作串（Show/Hide/Play 的参数是
# 屏幕名/资源路径，不提取）
_NOTIFY_STR_RE = re.compile(
    r'\bNotify\(\s*("(?:[^"\\]|\\.)*?"|\'(?:[^\'\\]|\\.)*?\')')

# 括号屏幕表达式：textbutton/text/label/tooltip 后跟 ( ——
# 表达式内的字符串字面量（条件选择/拼接）也是显示文本
_SCREEN_PAREN_RE = re.compile(r'\b(?:textbutton|text|label|tooltip)\s*\(')

# python 显示调用点（字符串是第一参数）：hint 与置信度增强用
_DISPLAY_CALL_RE = re.compile(
    r'(renpy\.notify|notify|renpy\.display_notify|renpy\.input|Text'
    r'|renpy\.say)\(\s*$')
_DISPLAY_CALL_HINTS = {
    'renpy.notify': '通知', 'notify': '通知', 'renpy.display_notify': '通知',
    'renpy.input': '输入提示', 'Text': '界面文本', 'renpy.say': '对话',
}

# python 上下文识别
_PY_BLOCK_START_RE = re.compile(r'^(init\s+(-?\d+\s+)?)?python\b.*:')
_INIT_BLOCK_RE = re.compile(r'^init\s+(-?\d+\s+)?:')
_DEFINE_RE = re.compile(r'^(define|default)\s+[\w.]+\s*=\s*(.*)$')
_SCOPE_PATTERNS = [
    (re.compile(r'^screen\s+(\w+)'), 'screen'),
    (re.compile(r'^label\s+(\w+)\s*:'), 'label'),
]

_SCOPE_KIND_NAMES = {'screen': '界面', 'label': '场景'}


def _string_prefix(line: str, start: int) -> str:
    """字面量的前缀（f/r/b/u 组合，如 'f'、'rf'），无则 ''

    f"..." 被包 _() 会变成 f_(...) 非法语法且位置校验挡不住；
    r/b 前缀串的转义语义与 _unescape 不符——带前缀的字面量整体跳过。
    前缀串前若是标识符字符（df"x" 的 f 是变量名尾部）则不是前缀。
    """
    j = start - 1
    while j >= 0 and line[j] in 'fFrRbBuU':
        j -= 1
    if j < start - 1 and (j < 0 or not (line[j].isalnum() or line[j] in '_.')):
        return line[j + 1:start]
    return ''


def resolve_source_root(game_root) -> Path:
    """find_candidates 的 rel_file 基准：game/game 存在则下钻一层

    AI 预筛、内嵌管线、导出自愈的源码读取都以此为根，
    三处共用此推导避免基准不一致导致工具读文件 404。
    """
    root = Path(game_root)
    nested = root / 'game'
    return nested if nested.is_dir() else root


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
    ai_confident: bool = True  # 级联判定内部状态（非模型自评）
    ai_reason: str = ''
    ai_evidence: str = ''      # AI 判决引用的证据（file:line）
    apply_path: str = ''       # 'table'（进翻译表）| 'wrap'（源码包 _()）


def _unescape(s: str) -> str:
    """Ren'Py/Python 字符串反转义（含 \\uXXXX/\\xXX——
    不处理会被路径过滤误杀，如 "Vandenberg\\u00A0Ltd. Lobby"）"""
    s = (s.replace('\\"', '"').replace("\\'", "'")
          .replace('\\n', '\n').replace('\\t', '\t'))
    s = re.sub(r'\\u[0-9a-fA-F]{4}',
               lambda m: chr(int(m.group(0)[2:], 16)), s)
    s = re.sub(r'\\x[0-9a-fA-F]{2}',
               lambda m: chr(int(m.group(0)[2:], 16)), s)
    return s.replace('\\\\', '\\')


def relocate_wrap_candidates(rows: list, source_root: str) -> tuple:
    """按记录位置校验并在同文件内重定位 wrap 候选

    源码只读原则：标记/项目更新不再改源码，记录行号可能在版本间漂移。
    导出时在目标目录（导出副本）上做定位：
    - 记录位置 raw 命中 → 原样采用
    - 未命中 → 同文件内搜该字面量：唯一匹配采用；多匹配取与记录行
      号最近者；零匹配丢弃

    rows: get_marked_embedded() 的 dict（id/rel_file/line/col_start/raw/
    text/kind/hint）
    返回 (candidates, moved_ids, lost_ids)：
    - candidates 带重定位后坐标、file 指向 source_root 下文件
    - moved_ids 需调用方回写 db.update_embedded_position
    """
    source_root = Path(source_root)
    candidates = []
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

    for r in rows:
        lines = _lines(r['rel_file'])
        if lines is None:
            lost_ids.append(r['id'])
            continue
        idx = r['line'] - 1
        pos_ok = (0 <= idx < len(lines)
                  and lines[idx][r['col_start']:r['col_start'] + len(r['raw'])]
                  == r['raw'])
        if pos_ok:
            candidates.append(Candidate(
                file=str(source_root / r['rel_file']), rel_file=r['rel_file'],
                line=r['line'], col_start=r['col_start'],
                col_end=r['col_start'] + len(r['raw']), raw=r['raw'],
                text=r['text'], kind=r['kind'], hint=r.get('hint', ''),
                confidence=''))
            continue
        # 行号漂移：同文件内容重定位
        hits = []
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
        if (ln, col) != (r['line'], r['col_start']):
            moved_ids.append((r['id'], ln, col))
        candidates.append(Candidate(
            file=str(source_root / r['rel_file']), rel_file=r['rel_file'],
            line=ln, col_start=col, col_end=col + len(r['raw']),
            raw=r['raw'], text=r['text'], kind=r['kind'],
            hint=r.get('hint', ''), confidence=''))
    return candidates, moved_ids, lost_ids


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
    # （先剥 {/size}{/color} 等 Ren'Py 标签——闭合标签带 /，
    # 含闭合标签的界面文本会被误判为路径而漏提取）
    no_tags = re.sub(r'\{[^}]*\}', '', stripped)
    if '/' in no_tags or '\\' in no_tags:
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
            try:
                rel = rpy_file.relative_to(base).as_posix()
            except ValueError:
                # 游戏根目录（game/ 之外）的 .rpy 不属于 base，
                # 改用相对 game_path 的路径，避免整个扫描崩溃
                rel = rpy_file.relative_to(game_path).as_posix()
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
                if kind == 'screen':
                    # screen 参数默认值：screen stats(title="Player") 的
                    # 字符串参数（渲染时被 kwarg 引用，是常见漏提取点）
                    for km in _KWARG_STR_RE.finditer(raw_line):
                        kw_literal = km.group(2)
                        kw_text = add_tflag(_unescape(kw_literal[1:-1]))
                        if _is_noise(kw_text, 'screen'):
                            continue
                        out.append(Candidate(
                            file=file_path, rel_file=rel_file, line=line_no,
                            col_start=km.start(2), col_end=km.end(2),
                            raw=kw_literal, text=kw_text, kind='screen',
                            hint=f'{m.group(1)}界面·参数默认',
                            confidence=_confidence(kw_text, 'screen'),
                        ))
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
            text = add_tflag(_unescape(raw_literal[1:-1]))
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

        # ---- A 类补充：括号屏幕表达式里的字符串 ----
        # textbutton ("None!" if not x else "Theoretical Research") —
        # A 类正则要求字面量紧跟关键字，括号表达式内的漏提
        if _SCREEN_PAREN_RE.search(raw_line):
            for m in _PY_STRING_RE.finditer(raw_line):
                # 跳过 _SCREEN_STRING_RE 已覆盖的位置（避免重复）
                if any(sm.start(2) == m.start()
                       for sm in _SCREEN_STRING_RE.finditer(raw_line)):
                    continue
                # 已包 _() 跳过
                if raw_line[max(0, m.start() - 3):m.start()].endswith('_('):
                    continue
                raw_literal = m.group(0)
                text = add_tflag(_unescape(raw_literal[1:-1]))
                if _is_noise(text, 'screen'):
                    continue
                out.append(Candidate(
                    file=file_path, rel_file=rel_file, line=line_no,
                    col_start=m.start(), col_end=m.end(),
                    raw=raw_literal, text=text, kind='screen',
                    hint=make_hint('按钮表达式'),
                    confidence=_confidence(text, 'screen'),
                ))

        # ---- screen 作用域内的 Notify("...") 动作串 ----
        if scope_kind == 'screen':
            for m in _NOTIFY_STR_RE.finditer(raw_line):
                # 已包 _()（Notify(_("x"))）跳过
                if raw_line[max(0, m.start(1) - 3):m.start(1)].endswith('_('):
                    continue
                raw_literal = m.group(1)
                text = add_tflag(_unescape(raw_literal[1:-1]))
                if _is_noise(text, 'screen'):
                    continue
                out.append(Candidate(
                    file=file_path, rel_file=rel_file, line=line_no,
                    col_start=m.start(1), col_end=m.end(1),
                    raw=raw_literal, text=text, kind='screen',
                    hint=make_hint('通知'),
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

            # 单行闭合三引号字面量本身是候选（_("""...""") 是合法语法，
            # Ren'Py 的 _() 扫描也支持三引号）；多行块仍在上面整块跳过
            for tm in _TRIPLE_RE.finditer(raw_line):
                if _string_prefix(raw_line, tm.start()):
                    continue
                if raw_line[max(0, tm.start() - 3):tm.start()].endswith('_('):
                    continue
                raw_literal = tm.group(0)
                text = add_tflag(_unescape(raw_literal[3:-3]))
                if _is_noise(text, 'python'):
                    continue
                out.append(Candidate(
                    file=file_path, rel_file=rel_file, line=line_no,
                    col_start=tm.start(), col_end=tm.end(),
                    raw=raw_literal, text=text, kind='python',
                    hint=make_hint('脚本文本'),
                    confidence=_confidence(text, 'python'),
                ))

            for m in _PY_STRING_RE.finditer(raw_line):
                if any(s <= m.start() and m.end() <= e for s, e in triple_spans):
                    continue
                raw_literal = m.group(0)
                # f/r/b/u 前缀字面量：包 _() 会变 f_(...) 非法语法或转义
                # 语义被破坏——整体跳过（含插值的 f-string 也无法 old/new 匹配）
                if _string_prefix(raw_line, m.start()):
                    continue
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
                text = add_tflag(_unescape(raw_literal[1:-1]))
                if _is_noise(text, 'python'):
                    continue
                # 显示调用点（renpy.notify("...") 等第一参数）：
                # 用途几乎没有悬念，hint 写具体、置信度拉满
                dm = _DISPLAY_CALL_RE.search(before)
                if dm:
                    hint = make_hint(_DISPLAY_CALL_HINTS[dm.group(1)])
                    conf = 'high'
                else:
                    hint = make_hint('脚本文本')
                    conf = _confidence(text, 'python')
                out.append(Candidate(
                    file=file_path, rel_file=rel_file, line=line_no,
                    col_start=m.start(), col_end=m.end(),
                    raw=raw_literal, text=text, kind='python',
                    hint=hint,
                    confidence=conf,
                ))

        # define/default 跨行表达式：跟踪括号余额
        if define_m or define_balance > 0:
            define_balance += raw_line.count('{') + raw_line.count('[') + raw_line.count('(')
            define_balance -= raw_line.count('}') + raw_line.count(']') + raw_line.count(')')
            if define_balance < 0:
                define_balance = 0


def apply_wrapping(candidates: list) -> tuple:
    """把候选字符串在原位置包成 __(...)

    __() = translate_string（立即翻译）：_() 是恒等函数（翻译留给显示
    层整串查找）——wrap 路径的候选全是拼接/格式化片段，拼好的整串在
    显示层必然查不中，必须在数据层用 __() 现译。
    注意：define/default 期的 __() 会把英文烤进默认值（语言设置可能
    未就位）——片段类候选几乎全在运行时位置，可接受。

    按文件分组后从文件末尾向开头替换（保持列偏移不失效）；
    替换前校验目标位置确实是期望的字面量（源码被改动过时跳过）。

    Returns:
        (成功数, 跳过数, 成功位置集合)——成功位置为 (file, line, col_start)，
        调用方据此只把真正包裹成功的候选标 marked（跳过的留在待复核，
        否则源码未变却标了 marked，下轮扫描会重现且永远失去处理机会）
    """
    by_file = {}
    for c in candidates:
        by_file.setdefault(c.file, []).append(c)

    wrapped = skipped = 0
    ok_positions = set()
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
            lines[idx] = line[:c.col_start] + '__(' + c.raw + ')' + line[c.col_end:]
            wrapped += 1
            ok_positions.add((c.file, c.line, c.col_start))
            changed = True

        if changed:
            path.write_text('\n'.join(lines), encoding='utf-8')

    return wrapped, skipped, ok_positions


def unwrap_candidates(candidates: list) -> tuple:
    """拆除候选位置的 _(...) 包裹（apply_wrapping 的逆操作）

    【legacy】源码只读化后生产路径不再拆包（工作副本从未被包裹，
    healer 改为标 skipped + 重导出）——保留供测试/手工修复旧项目
    工作副本中遗留的包裹用。
    记录的 col_start 指向带引号字面量起点；包裹后为 _(raw)，
    从文件末尾向开头处理保持偏移有效，目标位置不匹配时跳过。

    Returns:
        (成功拆除的 Candidate 列表, 跳过数)——调用方只对成功行
        更新状态，位置校验失败的行保持原状态以便下轮重试
    """
    by_file = {}
    for c in candidates:
        by_file.setdefault(c.file, []).append(c)

    unwrapped = []
    skipped = 0
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
            # 兼容两代包裹：_()（旧）与 __()（现；__ = translate_string）
            prefix_len = 2
            if line[c.col_start:c.col_start + 3] == '__(':
                prefix_len = 3
            elif line[c.col_start:c.col_start + 2] != '_(':
                skipped += 1
                continue
            end = c.col_start + prefix_len + len(c.raw)
            if (line[c.col_start + prefix_len:end] != c.raw
                    or line[end:end + 1] != ')'):
                skipped += 1
                continue
            lines[idx] = line[:c.col_start] + c.raw + line[end + 1:]
            unwrapped.append(c)
            changed = True

        if changed:
            path.write_text('\n'.join(lines), encoding='utf-8')

    return unwrapped, skipped
