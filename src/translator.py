"""AI翻译器 - 使用OpenAI兼容接口进行翻译"""

import json
import re
import time
from typing import List, Optional, Dict, Any, Callable
import openai
from openai import OpenAI
from dataclasses import dataclass
import tiktoken

# 模块级 BPE 编码器（cl100k_base，对 GPT 系列精确，对国产模型是误差 <15% 的近似，
# 远好于 len//3 对中文的 3 倍失真）。加载一次复用。
_ENC = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    """估算文本 token 数"""
    return len(_ENC.encode(text or ""))


class FatalAPIError(Exception):
    """不可重试的 API 错误（认证失败、余额不足等），批量任务应立即中止"""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


# 致命错误码（不可重试）及中文说明
_FATAL_REASONS = {
    400: '请求格式错误（请检查模型名称是否正确、参数是否合法、消息格式是否符合接口要求）',
    401: 'API Key 无效或认证失败（请检查 API Key 与 Base URL 是否匹配、请求头格式是否正确）',
    402: '账户余额不足，请及时充值',
    403: '拒绝访问（服务暂不支持当前地区，或 API Key 被风控，请尝试新建 API Key）',
    404: '接口或模型不存在（请检查模型名称与 Base URL 是否正确）',
}

# 可重试错误码及中文说明
_RETRYABLE_REASONS = {
    421: '内容审核拦截',
    429: '请求频率超限',
    500: '服务器内部故障',
    502: '网关错误',
    503: '服务器负载过高',
    504: '网关超时',
}


@dataclass
class TranslationConfig:
    """翻译配置"""
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-3.5-turbo"
    temperature: float = 0.3
    max_tokens: int = 1000
    context_lines: int = 3
    timeout: int = 30


class AITranslator:
    """AI翻译器"""

    MAX_RETRIES = 3  # 可重试错误的最大尝试次数

    def __init__(self, config: TranslationConfig):
        self.config = config
        self.client: Optional[OpenAI] = None
        self.api_log_callback: Optional[Callable[[dict, dict, str], None]] = None
        # api_log_callback(request_body, response_body, task_type)
        self._init_client()

    def _init_client(self):
        if self.config.api_key:
            self.client = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.api_base,
                max_retries=0,  # 关闭 SDK 自带重试，由 _call_api 统一控制
            )

    def update_config(self, config: TranslationConfig):
        self.config = config
        self._init_client()

    def _call_api(self, messages: list, temperature: float, max_tokens: int,
                  tools: list = None, tool_choice: dict = None,
                  return_message: bool = False, task_type: str = '') -> str:
        """统一的 API 调用：按错误码分类处理

        - 致命错误（400/401/402/403/404）：抛出 FatalAPIError，批量任务应立即中止
        - 可重试错误（421/429/5xx/超时/连接错误/空内容拦截）：指数退避重试，最多 MAX_RETRIES 次
        - 重试耗尽：抛出普通异常，调用方记失败并跳过该条目
        - return_message=True 时返回完整 message（用于 tool calls），否则返回 content 文本
        - api_log_callback 存在时，成功响应后将完整请求体/返回体交给回调记录
        """
        reason = '未知错误'
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                kwargs = dict(
                    model=self.config.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=self.config.timeout,
                )
                if tools:
                    kwargs['tools'] = tools
                    kwargs['tool_choice'] = tool_choice
                response = self.client.chat.completions.create(**kwargs)
                if self.api_log_callback:
                    try:
                        self.api_log_callback(kwargs, response.model_dump(), task_type)
                    except Exception:
                        pass  # 日志失败不影响翻译
                message = response.choices[0].message
                if return_message:
                    return message
                content = message.content
                if content is not None:
                    return content.strip()
                # 内容审核拦截时部分服务返回空内容，按可重试错误处理
                reason = '内容审核拦截（返回空内容）'
            except openai.APIStatusError as e:
                code = e.status_code
                if code in _FATAL_REASONS:
                    raise FatalAPIError(code, f'API 错误 {code}：{_FATAL_REASONS[code]}') from e
                reason = _RETRYABLE_REASONS.get(code, f'HTTP {code} 错误')
            except openai.APITimeoutError:
                reason = '请求超时'
            except openai.APIConnectionError:
                reason = '网络连接错误'

            if attempt < self.MAX_RETRIES:
                delay = min(2 ** attempt, 30)
                print(f'[API] {reason}，{delay} 秒后进行第 {attempt + 1}/{self.MAX_RETRIES} 次尝试...')
                time.sleep(delay)

        raise Exception(f'API 调用失败（已重试 {self.MAX_RETRIES} 次）：{reason}')

    def _build_system_prompt(self, character: str = "",
                              glossary_text: str = "",
                              character_profile: str = "",
                              style_guide: str = "") -> str:
        """构建系统提示词"""
        prompt = """你是一位资深的游戏本地化翻译家。请将以下文本翻译成简体中文。

核心规则：
- 只翻译【请翻译以下文本】中的内容，【前文参考】和【后文参考】不翻译
- 如果原文只有标点符号或空白，直接原样返回，不要翻译

标点风格规则：
- 保留原文的标点风格：原文首尾若用双引号包裹（游戏的对话显示风格），译文首尾也必须用双引号包裹
- 原文中用于强调、引用的引号，以及省略号、感叹号、问号等标点的语气和用法在译文中保留
- 不要自行添加或删除首尾引号

代码标记规则（以下内容直接保留原样，不翻译、不删除、不修改）：
- 方括号内容：[变量名]（如 [player_name]）
- 花括号内容：{标签}（如 {b}、{/b}、{w=2}、{color=#fff}）
- 美元符内容：$变量 或 $表达式
- 格式化占位符：%s、%d、%% 等
- 转义字符：\\n、\\t 等

翻译风格：
- 对话和旁白使用自然流畅的中文，像真人说话
- 根据角色性格和语境调整语气和用词
- 俚语、咒骂、感叹等按中文习惯本土化，保留原始情感强度
- 避免翻译腔，善用中文成语、俗语、语气词

术语提取规则：
- 只提取游戏内出现的专有名词：地名、物品名、技能名、组织名、种族名、特殊称呼等
- 不要提取：通用词汇、UI文字、技术术语、许可证名称、框架名称、软件名称
- 人名对照表中已有的人物名不要放入术语；但只在台词中被提及、未作为角色出场的人物名，可以作为专有名词放入术语
- 如果术语表中已有该词的翻译，不要重复添加
- 如果没有新的游戏专有名词，不输出术语部分"""

        # 作品风格指南
        if style_guide:
            prompt += f"\n\n【作品风格指南】\n{style_guide}"

        # 术语表
        if glossary_text:
            prompt += f"\n\n{glossary_text}"

        # 角色特征
        if character_profile:
            prompt += f"\n\n{character_profile}"

        return prompt

    def _build_user_prompt(self, text: str, character: str = "",
                           context_before: List[dict] = None,
                           context_after: List[dict] = None) -> str:
        """构建用户提示词

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

    def translate_text(self, text: str, character: str = "",
                       context_before: List[dict] = None,
                       context_after: List[dict] = None,
                       glossary_text: str = "",
                       character_profile: str = "",
                       style_guide: str = "",
                       debug: bool = False) -> tuple[str, list[dict]]:
        """翻译单行文本，返回 (译文, 术语列表)

        术语列表格式: [{'en_term': '...', 'cn_term': '...'}]
        """
        if not self.client:
            raise ValueError("请先配置API Key")

        if not text.strip():
            return "", []

        # 标点符号或空白直接返回
        if not any(c.isalnum() for c in text):
            return text, []

        system_prompt = self._build_system_prompt(
            character=character,
            glossary_text=glossary_text,
            character_profile=character_profile,
            style_guide=style_guide,
        )
        user_prompt = self._build_user_prompt(
            text, character, context_before, context_after
        )

        if debug:
            print(f'\n{"="*50}')
            print(f'[翻译] 角色: {character or "旁白"}')
            print(f'[翻译] 原文: {text}')
            print(f'[翻译] 系统提示词:\n{system_prompt[:500]}...')
            print(f'[翻译] 用户提示词:\n{user_prompt}')
            print(f'{"="*50}\n')

        raw = self._call_api(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            task_type='dialogue',
        )
        translated, terms = self._parse_translation_response(raw)

        # 兜底：剥掉模型误加的说话人前缀（[角色]/【角色】/角色:）
        if character and translated:
            for prefix in (f'[{character}]', f'【{character}】', f'{character}:', f'{character}：'):
                if translated.startswith(prefix):
                    translated = translated[len(prefix):].strip()
                    break

        if debug:
            print(f'[翻译] 翻译结果: {translated}')
            if terms:
                print(f'[翻译] 术语: {terms}')

        return translated, terms

    @staticmethod
    def _parse_translation_response(raw: str) -> tuple[str, list[dict]]:
        """解析 AI 翻译响应，分离译文和术语

        AI 返回格式：
        翻译结果

        【术语】
        原文1 → 译文1
        原文2 → 译文2
        """
        terms = []

        # 检查是否有术语部分
        if '【术语】' in raw:
            parts = raw.split('【术语】', 1)
            translated = parts[0].strip()
            terms_text = parts[1].strip()

            for line in terms_text.split('\n'):
                line = line.strip()
                if not line or '→' not in line:
                    continue
                term_parts = line.split('→', 1)
                if len(term_parts) == 2:
                    en = term_parts[0].strip()
                    cn = term_parts[1].strip()
                    if en and cn:
                        terms.append({'en_term': en, 'cn_term': cn})
        else:
            translated = raw

        return translated, terms

    def translate_name(self, name: str, glossary_text: str = "",
                       debug: bool = False) -> str:
        """翻译人名"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not name.strip():
            return ""

        if name.startswith('[') and name.endswith(']'):
            return name

        # 标点符号直接返回
        if not any(c.isalnum() for c in name):
            return name

        system_prompt = """你是一位游戏翻译专家。请将以下人名翻译成中文，只返回中文名。

规则：
- 只返回翻译后的中文名，不要添加解释
- 如果原文只有标点符号或空白，直接原样返回
- 方括号内容是变量占位符（如 [xxx_name]），直接返回原文
- $ 开头是代码变量，直接返回原文
- 只翻译真正的人名"""

        if glossary_text:
            system_prompt += f"\n\n{glossary_text}"

        user_prompt = f"请将以下人名翻译成中文，只返回中文名：\n{name}"

        if debug:
            print(f'\n{"="*50}')
            print(f'[人名翻译] 原文: {name}')
            print(f'{"="*50}\n')

        raw = self._call_api(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=self.config.max_tokens,
            task_type='name',
        )
        result = raw.replace('"', '').replace("'", '').replace('。', '')

        if debug:
            print(f'[人名翻译] 翻译结果: {result}')

        return result

    @staticmethod
    def _build_ui_system_prompt(glossary_text: str = "",
                                style_guide: str = "") -> str:
        """构建 UI 翻译系统提示词"""
        system_prompt = """你是一位游戏本地化翻译家。请将以下文本翻译成简体中文。

核心规则：
- 如果原文只有标点符号或空白，直接原样返回
- 按钮和菜单文字要简洁，符合中文游戏用语习惯
- 保留原文的标点风格：原文首尾若用双引号包裹，译文首尾也用双引号包裹，不要自行添加或删除

代码标记规则（以下内容直接保留原样，不翻译、不删除、不修改）：
- 方括号内容：[变量名]
- 花括号内容：{标签}
- 美元符内容：$变量
- 格式化占位符：%s、%d、%% 等

翻译风格：
- 简洁明了，符合中文表达习惯
- 专业术语保持一致
- 适当本地化，保留原意

术语提取规则：
- 只提取游戏内出现的专有名词：地名、物品名、技能名、组织名、种族名、特殊称呼等
- 不要提取：通用词汇、UI文字、技术术语
- 人名对照表中已有的人物名不要放入术语；但只在台词中被提及、未作为角色出场的人物名，可以作为专有名词放入术语
- 如果术语表中已有该词的翻译，不要重复添加
- 如果没有新的游戏专有名词，不输出术语部分"""

        if style_guide:
            system_prompt += f"\n\n【作品风格指南】\n{style_guide}"
        if glossary_text:
            system_prompt += f"\n\n{glossary_text}"
        return system_prompt

    def translate_ui(self, text: str, glossary_text: str = "",
                     character_dict: Dict[str, str] = None,
                     style_guide: str = "",
                     debug: bool = False) -> tuple[str, list[dict]]:
        """翻译UI文字，返回 (译文, 术语列表)"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not text.strip():
            return "", []

        # 标点符号直接返回
        if not any(c.isalnum() for c in text):
            return text, []

        system_prompt = self._build_ui_system_prompt(glossary_text, style_guide=style_guide)

        user_prompt = f"""请翻译：\n{text}

【输出格式】
第一行：翻译结果（只输出译文）
如果原文中有专有名词，空一行输出：
【术语】
原文1 → 译文1
没有专有名词则不输出【术语】部分。"""

        raw = self._call_api(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=self.config.max_tokens,
            task_type='ui',
        )
        translated, terms = self._parse_translation_response(raw)
        return translated, terms

    def _build_batch_user_prompt(self, items: List[dict],
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

    @staticmethod
    def _parse_tool_response(message, expected: int) -> tuple[Optional[List[str]], List[dict]]:
        """解析 tool calls 响应

        translations 是 [{id, translation}, ...]，按 id 放回位置（id 从 1 开始）。
        数量不足/id 越界/重复 → 返回 (None, [])。
        """
        calls = getattr(message, 'tool_calls', None)
        if not calls:
            return None, []
        try:
            args = json.loads(calls[0].function.arguments)
        except (json.JSONDecodeError, TypeError, AttributeError):
            return None, []

        raw = args.get('translations')
        if not isinstance(raw, list):
            return None, []

        # 按 id 对齐（兼容模型乱序返回）
        result = [None] * expected
        seen = set()
        for entry in raw:
            if not isinstance(entry, dict):
                return None, []
            idx = entry.get('id')
            text = entry.get('translation')
            if not isinstance(idx, int) or not (1 <= idx <= expected) or idx in seen:
                return None, []
            seen.add(idx)
            result[idx - 1] = text if isinstance(text, str) else ''

        # 必须覆盖全部 id
        if len(seen) != expected or any(r is None for r in result):
            return None, []

        terms = []
        for t in args.get('terms') or []:
            if isinstance(t, dict) and t.get('en_term') and t.get('cn_term'):
                terms.append({'en_term': str(t['en_term']), 'cn_term': str(t['cn_term'])})

        return result, terms

    def translate_batch(self, items: List[dict], content_type: str = 'dialogue',
                        glossary_text: str = "", character_profiles: str = "",
                        context_before: List[dict] = None,
                        style_guide: str = "",
                        context_window_tokens: Optional[int] = None,
                        debug: bool = False) -> tuple[Optional[List[str]], List[dict]]:
        """批次翻译多句文本，返回 (译文列表, 术语列表)

        items: [{'original_text': ..., 'character': ...}]，译文列表与之按顺序一一对应。
        解析失败（句数不匹配）时返回 (None, [])，调用方应回退逐句翻译。
        """
        if not self.client:
            raise ValueError("请先配置API Key")
        if not items:
            return [], []

        if content_type == 'ui':
            system_prompt = self._build_ui_system_prompt(glossary_text, style_guide=style_guide)
        else:
            system_prompt = self._build_system_prompt(
                glossary_text=glossary_text,
                character_profile=character_profiles,
                style_guide=style_guide,
            )
        user_prompt = self._build_batch_user_prompt(items, context_before, content_type)

        if debug:
            print(f'\n{"="*50}')
            print(f'[批次翻译] 共 {len(items)} 句')
            print(f'[批次翻译] 系统提示词:\n{system_prompt[:500]}...')
            print(f'[批次翻译] 用户提示词:\n{user_prompt}')
            print(f'{"="*50}\n')

        # 输出长度按批放大：译文长度通常不超过原文（英→中会缩短），1.2 倍余量确保不被截断
        est_input_tokens = sum(_count_tokens(it.get('original_text', '')) for it in items)
        max_tokens = max(self.config.max_tokens, int(est_input_tokens * 1.2) + 300)
        if context_window_tokens:
            # 输入 + 输出不得超出模型上下文窗口（deepseek 等服务超限直接返回 400）
            est_total_input = _count_tokens(system_prompt) + _count_tokens(user_prompt)
            max_tokens = min(max_tokens, max(1000, context_window_tokens - est_total_input))

        # Tool Calls：translations 用 {id, translation} 对象数组，靠 id 对齐
        n = len(items)
        tools = [{
            "type": "function",
            "function": {
                "name": "submit_translations",
                "description": "按 id 提交全部译文，以及原文中新出现的游戏专有名词术语",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "translations": {
                            "type": "array",
                            "description": f"恰好 {n} 条译文，每条用 id 对应输入条目的 text 译文",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "integer", "description": "输入条目的 id"},
                                    "translation": {"type": "string", "description": "该条 text 的译文"},
                                },
                                "required": ["id", "translation"],
                            },
                            "minItems": n,
                            "maxItems": n,
                        },
                        "terms": {
                            "type": "array",
                            "description": "原文中新出现且术语表中没有的游戏专有名词，没有则为空数组",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "en_term": {"type": "string"},
                                    "cn_term": {"type": "string"},
                                },
                                "required": ["en_term", "cn_term"],
                            },
                        },
                    },
                    "required": ["translations"],
                },
            },
        }]

        message = self._call_api(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=self.config.temperature,
            max_tokens=max_tokens,
            tools=tools,
            # 注意：不能强制指定函数（{"type": "function", ...}），
            # deepseek 思考模式会拒绝该 tool_choice（400），auto 下模型也会可靠调用
            tool_choice="auto",
            return_message=True,
            task_type=content_type,
        )
        translated_list, terms = self._parse_tool_response(message, n)

        if translated_list is not None:
            # 兜底：剥掉模型误抄进译文开头的说话人标记（[角色]/【角色】/角色:）
            cleaned = []
            for it, text in zip(items, translated_list):
                char = it.get('character', '')
                if char and text:
                    for prefix in (f'[{char}]', f'【{char}】', f'{char}:', f'{char}：'):
                        if text.startswith(prefix):
                            text = text[len(prefix):].strip()
                            break
                cleaned.append(text)
            translated_list = cleaned

        if debug:
            if translated_list is None:
                print(f'[批次翻译] 解析失败（句数不匹配），需回退逐句翻译')
            else:
                print(f'[批次翻译] 成功解析 {len(translated_list)} 句')
            if terms:
                print(f'[批次翻译] 术语: {terms}')

        return translated_list, terms

    def analyze_text(self, prompt: str) -> str:
        """分析文本（不使用翻译系统提示词）"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not prompt.strip():
            return ""

        system_prompt = "你是一个专业的文本分析师。请按照用户的要求进行分析，直接输出分析结果，不要添加额外的解释。"

        return self._call_api(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            task_type='analysis',
        )

    # 风格指南分析提示词模板
    _STYLE_GUIDE_PROMPT = """你是一位资深的游戏本地化专家。以下是某部视觉小说/游戏的台词抽样，请分析这部作品的文字风格，生成一份供翻译人员使用的【作品风格指南】。

台词抽样：
{sample}

请按以下维度输出（每项 1-3 句，直接给出结论，不要复述台词）：

【题材基调】作品的题材、氛围、世界观调性（如：暗黑奇幻、轻松日常、悬疑惊悚……）
【叙事人称与视角】旁白的人称、视角特点、与读者的距离感
【作者文风】句式长短与节奏、用词习惯（文雅/口语/粗俗）、修辞特点、幽默或抒情的方式
【情感强度】情感表达是克制还是外露，激烈场景的语言烈度
【翻译基调建议】中译时应保持的整体口吻、应避免的译法、需要特别注意的语言习惯（如口癖、双关、梗）"""

    def generate_style_guide(self, sample_text: str) -> str:
        """根据台词抽样生成作品风格指南"""
        if not sample_text.strip():
            return ""
        return self.analyze_text(self._STYLE_GUIDE_PROMPT.format(sample=sample_text))

    def test_connection(self) -> Dict[str, Any]:
        """测试API连接"""
        if not self.client:
            return {'success': False, 'error': '请先配置API Key'}

        try:
            content = self._call_api(
                messages=[{"role": "user", "content": "Hello, this is a test."}],
                temperature=0.0,
                max_tokens=10,
                task_type='test',
            )
            return {
                'success': True,
                'model': self.config.model,
                'response': content
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

