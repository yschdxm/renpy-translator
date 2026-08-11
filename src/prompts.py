"""提示词模板集中管理

translator / name_translation 的全部 prompt 常量与 builder。
重复片段（代码标记规则、术语提取规则、占位符/标签规则）抽成公共常量拼装，
文本内容与合并前逐字一致。
"""

import json
from typing import List

# ========== 公共片段 ==========

_CODE_MARKUP_HEADER = '代码标记规则（以下内容直接保留原样，不翻译、不删除、不修改）：'
_CODE_MARKUP_BRACKET = '- 方括号内容：[变量名]'
_CODE_MARKUP_BRACE = '- 花括号内容：{标签}'
_CODE_MARKUP_DOLLAR = '- 美元符内容：$变量'
_CODE_MARKUP_PERCENT = '- 格式化占位符：%s、%d、%% 等'


def _code_markup_rules(with_examples: bool) -> str:
    """代码标记规则；with_examples=True 为对话版（带示例与转义字符行）"""
    if with_examples:
        return '\n'.join([
            _CODE_MARKUP_HEADER,
            _CODE_MARKUP_BRACKET + '（如 [player_name]）',
            _CODE_MARKUP_BRACE + '（如 {b}、{/b}、{w=2}、{color=#fff}）',
            _CODE_MARKUP_DOLLAR + ' 或 $表达式',
            _CODE_MARKUP_PERCENT,
            '- 转义字符：\\n、\\t 等',
        ])
    return '\n'.join([
        _CODE_MARKUP_HEADER,
        _CODE_MARKUP_BRACKET,
        _CODE_MARKUP_BRACE,
        _CODE_MARKUP_DOLLAR,
        _CODE_MARKUP_PERCENT,
    ])


_TERMS_HEADER = '术语提取规则：'
_TERMS_WHAT = '- 只提取游戏内出现的专有名词：地名、物品名、技能名、组织名、种族名、特殊称呼等'
_TERMS_NOT_BASE = '- 不要提取：通用词汇、UI文字、技术术语'
_TERMS_NAME = ('- 人名对照表中已有的人物名不要放入术语；但只在台词中被提及、'
               '未作为角色出场的人物名，可以作为专有名词放入术语')
_TERMS_DEDUP = '- 如果术语表中已有该词的翻译，不要重复添加'
_TERMS_NONE = '- 如果没有新的游戏专有名词，不输出术语部分'


def _terms_rules(dialogue: bool) -> str:
    """术语提取规则；dialogue=True 的「不要提取」行多列三类排除项"""
    not_line = _TERMS_NOT_BASE + ('、许可证名称、框架名称、软件名称' if dialogue else '')
    return '\n'.join([
        _TERMS_HEADER,
        _TERMS_WHAT,
        not_line,
        _TERMS_NAME,
        _TERMS_DEDUP,
        _TERMS_NONE,
    ])


# 人名翻译/人物分析 prompt 共用的占位符与标签规则
NAME_PLACEHOLDER_RULES = """- 方括号包裹的内容（如 [cleo_name]、[mc_name]）是 Ren'Py 变量占位符，绝对不要翻译，直接保留原样
- 花括号包裹的内容（如 {i}、{/i}、{size=-5}）是 Ren'Py 标记标签，不是文本内容，忽略它们
- $ 开头的内容是代码变量，不要翻译"""


def _append_style_glossary(prompt: str, glossary_text: str,
                           style_guide: str = "") -> str:
    """追加作品风格指南与术语表（顺序：风格指南在前）"""
    if style_guide:
        prompt += f"\n\n【作品风格指南】\n{style_guide}"
    if glossary_text:
        prompt += f"\n\n{glossary_text}"
    return prompt


# ========== 对话翻译 ==========

def build_system_prompt(glossary_text: str = "",
                        character_profile: str = "",
                        style_guide: str = "") -> str:
    """构建对话翻译系统提示词"""
    prompt = f"""你是一位资深的游戏本地化翻译家。请将以下文本翻译成简体中文。

核心规则：
- 只翻译【请翻译以下文本】中的内容，【前文参考】和【后文参考】不翻译
- 如果原文只有标点符号或空白，直接原样返回，不要翻译

标点风格规则：
- 保留原文的标点风格：原文首尾若用双引号包裹（游戏的对话显示风格），译文首尾也必须用双引号包裹
- 原文中用于强调、引用的引号，以及省略号、感叹号、问号等标点的语气和用法在译文中保留
- 不要自行添加或删除首尾引号

{_code_markup_rules(with_examples=True)}

翻译风格：
- 对话和旁白使用自然流畅的中文，像真人说话
- 根据角色性格和语境调整语气和用词
- 俚语、咒骂、感叹等按中文习惯本土化，保留原始情感强度
- 避免翻译腔，善用中文成语、俗语、语气词

{_terms_rules(dialogue=True)}"""

    # 作品风格指南 / 术语表 / 角色特征
    prompt = _append_style_glossary(prompt, glossary_text, style_guide)
    if character_profile:
        prompt += f"\n\n{character_profile}"

    return prompt


def build_user_prompt(text: str, character: str = "",
                      context_before: List[dict] = None,
                      context_after: List[dict] = None) -> str:
    """构建单句翻译用户提示词

    context_before/after 格式：
    [{'original_text': '...', 'translated_text': '...', 'character': '...'}]
    """
    prompt = ""

    # 前文参考（已翻译 + 未翻译）
    if context_before:
        prompt += "【前文参考 - 用于理解剧情和翻译风格】\n"
        for item in context_before:
            char = item.get('character', '') or '旁白'
            orig = item.get('original_text', '')
            trans = item.get('translated_text', '')
            if trans:
                prompt += f"[已译] {char}: \"{orig}\" → \"{trans}\"\n"
            else:
                prompt += f"{char}: \"{orig}\"\n"
        prompt += "\n"

    # 待翻译文本
    prompt += f"【请翻译以下文本】\n{text}"

    # 角色信息
    if character:
        prompt += f"\n\n【角色信息】\n说话角色：{character}"
        prompt += "\n请根据该角色的身份和性格，使用符合其特点的语言风格翻译。"

    # 后文参考
    if context_after:
        prompt += "\n\n【后文参考 - 用于理解剧情走向】\n"
        for item in context_after:
            char = item.get('character', '') or '旁白'
            orig = item.get('original_text', '')
            trans = item.get('translated_text', '')
            if trans:
                prompt += f"[已译] {char}: \"{orig}\" → \"{trans}\"\n"
            else:
                prompt += f"{char}: \"{orig}\"\n"

    prompt += """

【输出格式】
第一行：翻译结果（只输出译文，不要加任何前缀）
如果原文中有新的游戏专有名词（地名、物品名、技能名等，且术语表中没有），在译文后空一行输出：
【术语】
原文1 → 译文1
原文2 → 译文2
如果术语表中已有，或没有新的游戏专有名词，不输出【术语】部分。"""

    return prompt


# ========== 人名翻译 ==========

_NAME_SYSTEM_PROMPT = """你是一位游戏翻译专家。请将以下人名翻译成中文，只返回中文名。

规则：
- 只返回翻译后的中文名，不要添加解释
- 如果原文只有标点符号或空白，直接原样返回
- 方括号内容是变量占位符（如 [xxx_name]），直接返回原文
- $ 开头是代码变量，直接返回原文
- 只翻译真正的人名"""


def build_name_system_prompt(glossary_text: str = "") -> str:
    """构建人名翻译系统提示词"""
    prompt = _NAME_SYSTEM_PROMPT
    if glossary_text:
        prompt += f"\n\n{glossary_text}"
    return prompt


def build_name_user_prompt(name: str) -> str:
    return f"请将以下人名翻译成中文，只返回中文名：\n{name}"


# ========== UI 翻译 ==========

def build_ui_system_prompt(glossary_text: str = "",
                           style_guide: str = "") -> str:
    """构建 UI 翻译系统提示词"""
    prompt = f"""你是一位游戏本地化翻译家。请将以下文本翻译成简体中文。

核心规则：
- 如果原文只有标点符号或空白，直接原样返回
- 按钮和菜单文字要简洁，符合中文游戏用语习惯
- 保留原文的标点风格：原文首尾若用双引号包裹，译文首尾也用双引号包裹，不要自行添加或删除

{_code_markup_rules(with_examples=False)}

翻译风格：
- 简洁明了，符合中文表达习惯
- 专业术语保持一致
- 适当本地化，保留原意

{_terms_rules(dialogue=False)}"""

    return _append_style_glossary(prompt, glossary_text, style_guide)


def build_ui_user_prompt(text: str) -> str:
    return f"""请翻译：\n{text}

【输出格式】
第一行：翻译结果（只输出译文）
如果原文中有专有名词，空一行输出：
【术语】
原文1 → 译文1
没有专有名词则不输出【术语】部分。"""


# ========== 批次翻译 ==========

def build_batch_user_prompt(items: List[dict],
                            context_before: List[dict] = None,
                            content_type: str = 'dialogue') -> str:
    """构建批次翻译用户提示词（JSON 结构化输入）

    待译文本以 JSON 数组给出，每项含 id/speaker/text/scene/hint，
    speaker 在独立字段中，模型不会把它当待译文本翻译。
    输出靠 id 对齐，不要求位置对应。
    """
    prompt = ""

    if context_before:
        prompt += "【前文参考 - 用于理解剧情和翻译风格】\n"
        for item in context_before:
            char = item.get('character', '') or '旁白'
            orig = item.get('original_text', '')
            trans = item.get('translated_text', '')
            if trans:
                prompt += f"[已译] {char}: \"{orig}\" → \"{trans}\"\n"
            else:
                prompt += f"{char}: \"{orig}\"\n"
        prompt += "\n"

    n = len(items)
    structured = []
    for i, item in enumerate(items, 1):
        entry = {"id": i, "text": item.get('original_text', '').replace('\n', ' ')}
        char = item.get('character', '')
        if char:
            entry["speaker"] = char
        if content_type == 'dialogue':
            label = item.get('label', '')
            if label:
                entry["scene"] = label
        else:
            hint = item.get('context_hint', '')
            if hint:
                entry["hint"] = hint
        structured.append(entry)

    prompt += f"【请翻译以下 JSON 数组中每一项的 text 字段，共 {n} 条】\n"
    prompt += json.dumps(structured, ensure_ascii=False, indent=None)

    prompt += f"""

【字段说明】
- id：条目编号，输出时用它与译文一一对应
- text：待翻译的原文（唯一需要翻译的字段）
- speaker：说话人（仅参考语气，不要翻译，不要放进译文）
- scene：所属场景标签（仅提供语境，不要翻译）
- hint：字符串出处提示（仅帮助推断词义，不要翻译）

【输出要求】
- 调用 submit_translations 函数提交结果
- translations 数组必须恰好包含 {n} 条，每条用 id 对应输入条目的译文，不要合并、拆分或跳过任何一条
- 只翻译 text 字段；speaker/scene/hint 只是上下文，绝不翻译也不出现在译文中
- 原文中新出现的游戏专有名词（地名、物品名、技能名等，且术语表中没有的）放入 terms 数组；人名对照表中已有的人物名不要放入，但只在台词中被提及、未出场的人物名可以放入；术语表中已有或没有新名词时传空数组"""

    return prompt


# ========== 文本分析 / 风格指南 ==========

ANALYSIS_SYSTEM_PROMPT = "你是一个专业的文本分析师。请按照用户的要求进行分析，直接输出分析结果，不要添加额外的解释。"

STYLE_GUIDE_PROMPT = """你是一位资深的游戏本地化专家。以下是某部视觉小说/游戏的台词抽样，请分析这部作品的文字风格，生成一份供翻译人员使用的【作品风格指南】。

台词抽样：
{sample}

请按以下维度输出（每项 1-3 句，直接给出结论，不要复述台词）：

【题材基调】作品的题材、氛围、世界观调性（如：暗黑奇幻、轻松日常、悬疑惊悚……）
【叙事人称与视角】旁白的人称、视角特点、与读者的距离感
【作者文风】句式长短与节奏、用词习惯（文雅/口语/粗俗）、修辞特点、幽默或抒情的方式
【情感强度】情感表达是克制还是外露，激烈场景的语言烈度
【翻译基调建议】中译时应保持的整体口吻、应避免的译法、需要特别注意的语言习惯（如口癖、双关、梗）"""


# ========== 人名翻译 + 人物分析（services/name_translation 使用） ==========

def build_analyze_only_prompt(en_name, lines_text, batch_idx, total_batches):
    return f"""【任务类型：文本分析，不是翻译】

分析游戏角色 "{en_name}" 的台词，总结人物特征。

重要规则：
{NAME_PLACEHOLDER_RULES}

以下是该角色的台词（第{batch_idx+1}批，共{total_batches}批）：
{lines_text}

请按以下格式输出分析结果：

性格特点：
说话风格：<语气、句长、用词雅俗、情绪表达方式>
口癖与口头禅：<反复出现的口头禅、语气词、句尾习惯，没有则写"无">
称谓习惯：<如何称呼他人（名字/昵称/敬语/蔑称），举例>
外貌特征：
行为习惯：
人物关系：
背景故事：
角色定位：
代表台词：<摘 1-2 句最能体现该角色口吻的原文台词>
翻译建议：<仅针对该角色 "{en_name}" 的台词翻译给出建议，如语气、用词风格、特殊表达的处理方式，不要给其他角色的翻译建议>"""


def build_translate_analyze_prompt(en_name, lines_text, batch_idx, total_batches, dict_text):
    return f"""你是一位资深的游戏本地化专家。请同时完成以下两个任务：

## 任务1：翻译人名
将角色名 "{en_name}" 翻译成中文。

翻译时请考虑：
- 从台词中推断角色的性格、背景、文化特征
- 音译、意译还是混合？选择最符合角色气质的方式
- 是否有双关、谐音、文化梗需要保留？
- 与已有的人名翻译保持风格一致

重要规则：
{NAME_PLACEHOLDER_RULES}

## 任务2：分析角色
从台词中分析该角色的人物特征。

以下是该角色的台词（第{batch_idx+1}批，共{total_batches}批）：
{lines_text}

{"已有的人名翻译（供参考）：" + chr(10) + dict_text if dict_text else ""}

## 输出格式（严格按此格式，不要添加其他内容）

【人名翻译】
中文名：<翻译结果>

【人物分析】
性格特点：<分析>
说话风格：<语气、句长、用词雅俗、情绪表达方式>
口癖与口头禅：<反复出现的口头禅、语气词、句尾习惯，没有则写"无">
称谓习惯：<如何称呼他人（名字/昵称/敬语/蔑称），举例>
外貌特征：<分析>
行为习惯：<分析>
人物关系：<分析>
背景故事：<分析>
角色定位：<分析>
代表台词：<摘 1-2 句最能体现该角色口吻的原文台词>
翻译建议：<仅针对该角色 "{en_name}" 的台词翻译给出建议，如语气、用词风格、特殊表达的处理方式，不要给其他角色的翻译建议>"""


def build_continue_prompt(en_name, lines_text, batch_idx, total_batches, summaries):
    return f"""【任务类型：文本分析，不是翻译】

继续分析游戏角色 "{en_name}" 的更多台词（第{batch_idx+1}批，共{total_batches}批）。

之前已有的分析摘要：
{chr(10).join(summaries[-2:]) if summaries else "无"}

新的台词：
{lines_text}

请简要总结这批台词中展现的新特征（性格、说话风格、关系变化等），补充到已有分析中。不要重复已有内容。"""


def build_merge_summaries_prompt(name, summaries):
    summaries_text = '\n\n'.join([f'第{i+1}批分析：\n{s}' for i, s in enumerate(summaries)])
    return f"""根据以下对角色 "{name}" 的多段分析，合并为一个完整的人物特征报告。

{summaries_text}

重要规则：
- 方括号包裹的内容（如 [cleo_name]）是变量占位符，不要翻译
- 翻译建议仅针对角色 "{name}" 的台词，不要给其他角色的翻译建议

请按以下格式输出完整的人物特征（合并所有发现，去除重复）：

性格特点：
外貌特征：
说话风格：
行为习惯：
人物关系：
背景故事：
角色定位：
翻译建议："""
