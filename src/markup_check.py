"""译文标记一致性校验（[插值] 与 {标签}）——翻译链路与导出闸门共用

Ren'Py 文本里有两类机器语法，译文破坏它们会让游戏在渲染该句时报错：
- [表达式] 插值：渲染时 eval，表达式被改写（如 [tribe_name] 译成 [部落名]）
  即 NameError；未闭合的 [ 抛 "String ends with an open format operation"。
- {标签} 文本标签：未知标签 7.x 必崩、8.x 降级为纯文本显示；丢失 {w}/{i}
  等则停顿/强调静默损失。

历史教训（Lab Rats/Wartribe 系生产事故）：提示词只有"保留原样"的软约束，
AI 违反时全链路无代码级检查，坏译文一路入库、导出、进游戏。本模块把
约束变成硬校验，两侧输入统一用库内的 rpy 转义形式（[[ 表示字面 [，
{{ 表示字面 {，与 AI 看到的原文同源）。

校验规则（原文 vs 译文，[[/{{ 先占位再比较集合，{#注释} 两端剔除）：
- 译文出现原文没有的插值 → 违规（改写/编造表达式，渲染时 NameError）
- 原文插值在译文中缺失 → 违规（玩家名等动态内容不再显示）
- 译文出现原文没有的标签 → 违规（编造标签在 7.x 上崩）
- 原文标签在译文中缺失 → 违规（停顿/强调丢失）
- 译文中 [ 或 { 单侧不配对 → 违规（未闭合格式，渲染必炸）；
  原文本身就不平衡时豁免——那是源头的错误（英文版同样会炸），
  译文保持原样不该背锅

Python str.format 占位符（{} / {0} / {0:>3}）无需特判：本身配对，
平衡扫描天然通过；{0} 这类带内容的会作为"标签"参与集合比较，
丢失即违规——正是期望行为（.format 参数丢了运行期一样炸）。

check_pair 返回违规原因列表（空 = 通过）；消息写成对模型的纠正指令，
可直接拼进重试提示。
"""

import re

_TAG_RE = re.compile(r'\{([^{}]+)\}')


def _strip_escapes(text: str) -> str:
    # [[ 与 {{ 是字面转义，先占位避免被当语法解析
    return text.replace('[[', '\x00').replace('{{', '\x01')


def extract_interps(text: str) -> set:
    """提取最外层完整插值表达式集合（strip 归一空白）

    栈式扫描、嵌套感知：'[len(x[i])]' 提取 'len(x[i])'——正则取内层的
    做法会把外层改写（len( → size()）漏检。未闭合的尾部不开票
    （由 check_pair 的平衡扫描单独报）。
    """
    t = _strip_escapes(text)
    out = set()
    depth = 0
    start = 0
    for i, ch in enumerate(t):
        if ch == '[':
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ']' and depth > 0:
            depth -= 1
            if depth == 0:
                expr = t[start:i].strip()
                if expr:
                    out.add(expr)
    return out


def extract_tags(text: str) -> set:
    """提取文本标签集合；{#...} 是注释标签（渲染丢弃、翻译查找时剥掉），
    不参与比较"""
    t = _strip_escapes(text)
    return {g.strip() for g in _TAG_RE.findall(t)
            if not g.strip().startswith('#')}


def check_newline(original: str, translation: str) -> list:
    """译文真实换行检查，返回违规原因列表（空 = 通过）

    官方文档口径：Ren'Py 字符串不支持跨行（say/menu/translate 字符串
    都是单逻辑行），字符串内的换行必须写成转义形式 \\n；多行文本要用
    三引号或 _p() 块。AI 把原文的 \\n（两字符）"贴心"展开成真实换行是
    最常见的违规形态——原文没有真实换行而译文有，即为违规。

    原文本身含真实换行（多行模板/_p 块）时译文同形不算违规；
    导出层 escape_translation 本来就能把真实换行安全写成 \\n，
    所以这里只进翻译期纠正链路，不进导出拦截闸门。
    """
    if ('\n' in translation or '\r' in translation) \
            and '\n' not in original and '\r' not in original:
        return ['译文包含原文没有的真实换行符——Ren\'Py 字符串不支持跨行，'
                '请把换行写成转义形式 \\n（反斜杠+n 两字符），'
                '与原文的 \\n 位置一一对应']
    return []


def check_pair(original: str, translation: str) -> list:
    """校验译文相对原文的标记一致性，返回违规原因列表（空 = 通过）

    消息即纠正指令：翻译重试时原样拼给模型，导出闸门里拼进拦截清单。
    """
    problems = []
    o = _strip_escapes(original)
    t = _strip_escapes(translation)

    # 未闭合检测：括号平衡扫描（嵌套感知，如 [len(x[i])] 是合法插值）。
    # 孤立的 ] 与 } 是合法字面字符（depth 为 0 时的闭合侧忽略），Ren'Py
    # 只要求 [ 与 { 转义。原文本身不平衡是源头的错误，译文保持原样不拦
    if _unbalanced(t, '[', ']') and not _unbalanced(o, '[', ']'):
        problems.append('译文存在未闭合的 [（没有配对的 ]）。'
                        '字面方括号必须写成 [[')
    if _unbalanced(t, '{', '}') and not _unbalanced(o, '{', '}'):
        problems.append('译文存在未闭合的 {（没有配对的 }）。'
                        '字面花括号必须写成 {{')

    lost_i = extract_interps(original) - extract_interps(translation)
    if lost_i:
        problems.append('译文丢失了原文的插值表达式 '
                        f'{_fmt(sorted(lost_i), "[", "]")}——必须原样保留，'
                        '不要翻译、不要改名、不要删除')
    new_i = extract_interps(translation) - extract_interps(original)
    if new_i:
        problems.append('译文出现了原文没有的插值表达式 '
                        f'{_fmt(sorted(new_i), "[", "]")}——禁止添加或改写，'
                        '只能使用原文中的表达式')

    lost_t = extract_tags(original) - extract_tags(translation)
    if lost_t:
        problems.append('译文丢失了原文的文本标签 '
                        f'{_fmt(sorted(lost_t), "{", "}")}——必须原样保留')
    new_t = extract_tags(translation) - extract_tags(original)
    if new_t:
        problems.append('译文出现了原文没有的文本标签 '
                        f'{_fmt(sorted(new_t), "{", "}")}——'
                        '不要添加原文没有的标签')

    return problems


def _unbalanced(t: str, open_ch: str, close_ch: str) -> bool:
    """括号平衡扫描：嵌套合法（[len(x[i])]）；孤立闭合侧视为字面字符"""
    depth = 0
    for ch in t:
        if ch == open_ch:
            depth += 1
        elif ch == close_ch and depth > 0:
            depth -= 1
    return depth > 0


def _fmt(items: list, open_ch: str, close_ch: str) -> str:
    shown = [f'{open_ch}{i}{close_ch}' for i in items[:3]]
    more = f' 等 {len(items)} 个' if len(items) > 3 else ''
    return '、'.join(shown) + more
