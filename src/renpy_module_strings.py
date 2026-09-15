# -*- coding: utf-8 -*-
"""_ren.py（纯 Python 模块）显示字符串定向提取

Ren'Py 游戏里 *_ren.py 是编译进 store 的 Python 模块（Reformulate 系
把数据定义全放在这里）：Goal/SerumTrait/Action/Room 的构造参数、
display_name/tooltip 等展示 kwarg、get_*_string 函数的阶层返回串——
全部不在 rpy 扫描范围（find_candidates 只 rglob *.rpy）。

纯 Python 用 ast 精确提取（比 rpy 行扫描可靠），三条规则：
1. 构造器首参：已知显示类构造器的首个位置参数字符串
2. 显示 kwarg：任意 Call 的 name/title/display_name/... 字符串字面量
3. 字符串返回：函数名含 string/name/title 的函数体内 return "<字面量>"

产物与 find_candidates 同形（Candidate, kind='python'），走同一
筛查/标记/zz/导出管线。`_()` 在 _ren.py 的 store 上下文可用
（ClimaxController_ren.py 补丁已实证），wrap 路径同样安全。
"""
import ast
from pathlib import Path

from embedded_strings import Candidate, _EXCLUDE_DIRS, _is_noise
from markup_check import add_tflag

# 规则 1：显示类构造器（首位置参数是显示名）
_DISPLAY_CTORS = {
    'Goal', 'SerumTrait', 'SerumDesign', 'Action', 'Room', 'JobDefinition',
    'Role', 'Duty', 'Outfit', 'Clothing', 'Position', 'SexPosition',
    'Trainable', 'Fetish', 'Opinion', 'Policy', 'Business', 'Division',
    'Location', 'Hub', 'IT_Project', 'Project', 'Serum', 'Stat',
}

# 规则 2：展示用途的 kwarg 名
_DISPLAY_KWARGS = {
    'name', 'title', 'display_name', 'formal_name', 'description',
    'tooltip', 'the_tooltip', 'menu_tooltip', 'button_text', 'caption',
    'label', 'text', 'hover_text', 'placeholder',
}


def _func_name(func) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ''


def _string_args(node: ast.Call) -> list:
    """按规则 1/2 收集 Call 里的显示字符串字面量（(value, lineno, col_offset)）"""
    out = []
    fname = _func_name(node.func)
    if fname in _DISPLAY_CTORS and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.append((first.value, first.lineno, first.col_offset))
        # 构造器的第二参数常是描述（Goal("名", "描述...")）
        if len(node.args) > 1:
            second = node.args[1]
            if isinstance(second, ast.Constant) \
                    and isinstance(second.value, str):
                out.append((second.value, second.lineno, second.col_offset))
    for kw in node.keywords:
        if kw.arg in _DISPLAY_KWARGS and isinstance(kw.value, ast.Constant) \
                and isinstance(kw.value.value, str):
            out.append((kw.value.value, kw.value.lineno,
                        kw.value.col_offset))
    return out


def find_module_string_candidates(game_dir: str) -> list:
    """扫描 *_ren.py，定向提取显示字符串候选（kind='python'）"""
    game_path = Path(game_dir)
    roots = [p for p in (game_path / 'game', game_path) if p.exists()]
    base = game_path / 'game' if (game_path / 'game').exists() else game_path

    candidates = []
    seen_files = set()
    for root in roots:
        for py_file in sorted(root.rglob('*_ren.py')):
            if py_file in seen_files:
                continue
            seen_files.add(py_file)
            if _EXCLUDE_DIRS & set(py_file.parts):
                continue
            try:
                content = py_file.read_text(encoding='utf-8', errors='ignore')
                tree = ast.parse(content)
            except (OSError, SyntaxError, ValueError):
                continue
            try:
                rel = py_file.relative_to(base).as_posix()
            except ValueError:
                rel = py_file.relative_to(game_path).as_posix()
            _scan_module(str(py_file), rel, content, tree, candidates)

    candidates.sort(key=lambda c: (c.rel_file, c.line, c.col_start))
    return candidates


def _scan_module(file_path: str, rel_file: str, content: str,
                 tree: ast.AST, out: list):
    lines = content.split('\n')

    def emit(text: str, lineno: int, col: int, hint: str,
             skip_snake_check: bool = False):
        # 候选 text 即带 !t（值通道）：运行字面量/zz old 经清扫也有 !t，
        # ui_texts 原文没有 !t 时填充会失配（生产事故：Search Mom's room）
        text = add_tflag(text)
        if _is_noise(text, 'screen' if skip_snake_check else 'python'):
            return
        line = lines[lineno - 1] if 0 < lineno <= len(lines) else ''
        raw = _literal_at(line, col, text)
        if raw is None:
            return
        out.append(Candidate(
            file=file_path, rel_file=rel_file, line=lineno,
            col_start=col, col_end=col + len(raw), raw=raw, text=text,
            kind='python', hint=hint, confidence='low'))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for text, lineno, col in _string_args(node):
                emit(text, lineno, col, '模块定义·' + (_func_name(node.func) or 'kwarg'))
            # lst.append/insert("str")：组合列表的显示元素（菜单小标题等）
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr in ('append', 'insert')):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) \
                            and isinstance(arg.value, str):
                        emit(arg.value, arg.lineno, arg.col_offset,
                             f'列表元素·{node.func.attr}')
        elif isinstance(node, ast.Dict):
            # 话题/映射 dict（{"flirting": "调情用语", ...}——键是话题名、
            # 值是显示短语，LR2 观点系统模式），键值都是显示文本
            for k, v in zip(node.keys, node.values):
                for elt in (k, v):
                    if (isinstance(elt, ast.Constant)
                            and isinstance(elt.value, str)):
                        emit(elt.value, elt.lineno, elt.col_offset,
                             '映射表', skip_snake_check=True)
        elif isinstance(node, ast.Assign):
            # 名称含 list/names 的字符串列表/元组赋值：
            # text_opinion_list = ["I hate", ...]、opinion_names = ("hates", ...)
            # ——列表上下文保证是显示文本，跳过蛇形名单词过滤
            for target in node.targets:
                tname = target.id.lower() if isinstance(target, ast.Name) else ''
                if not ('list' in tname or 'names' in tname):
                    continue
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) \
                                and isinstance(elt.value, str):
                            emit(elt.value, elt.lineno, elt.col_offset,
                                 f'字符串列表·{tname}', skip_snake_check=True)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lowered = node.name.lower()
            # 字符串函数（get_*_string）与需求函数（*_requirement 的
            # return 串是"按钮不可用的原因"显示文本，LR2 模式）
            is_string_func = (any(k in lowered for k in ('string', 'name', 'title'))
                              or lowered.endswith('_requirement'))
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(
                        sub.value, ast.Constant) \
                        and isinstance(sub.value.value, str):
                    if is_string_func:
                        emit(sub.value.value, sub.value.lineno,
                             sub.value.col_offset, f'字符串函数·{node.name}')
                # return [ ... ] / return ( ... )：字符串列表返回
                # （init_list_of_opinions 的观点话题表）
                elif isinstance(sub, ast.Return) and isinstance(
                        sub.value, (ast.List, ast.Tuple)):
                    for elt in sub.value.elts:
                        if isinstance(elt, ast.Constant) \
                                and isinstance(elt.value, str):
                            emit(elt.value, elt.lineno, elt.col_offset,
                                 f'列表返回·{node.name}',
                                 skip_snake_check=True)


def _literal_at(line: str, col: int, text: str):
    """从源码行 col 处还原带引号字面量（ast col_offset 指向引号）。

    ast 的 col_offset 在引号上；取引号字符向后扫描到配对引号
    （处理转义）。返回含引号的 raw，失败返回 None。
    """
    if col >= len(line) or line[col] not in ('"', "'"):
        return None
    quote = line[col]
    i = col + 1
    while i < len(line):
        ch = line[i]
        if ch == '\\':
            i += 2
            continue
        if ch == quote:
            return line[col:i + 1]
        i += 1
    return None
