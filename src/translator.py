"""AI翻译器 - 使用OpenAI兼容接口进行翻译"""

import json
import re
import time
from typing import List, Optional, Dict, Any, Callable
import openai
from openai import OpenAI
from dataclasses import dataclass


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

    MAX_RETRIES = 5  # 可重试错误的最大尝试次数

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
                              character_profile: str = "") -> str:
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
- 如果术语表中已有该词的翻译，不要重复添加
- 如果没有新的游戏专有名词，不输出术语部分"""

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
            character_profile=character_profile
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
    def _build_ui_system_prompt(glossary_text: str = "") -> str:
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
- 如果术语表中已有该词的翻译，不要重复添加
- 如果没有新的游戏专有名词，不输出术语部分"""

        if glossary_text:
            system_prompt += f"\n\n{glossary_text}"
        return system_prompt

    def translate_ui(self, text: str, glossary_text: str = "",
                     character_dict: Dict[str, str] = None,
                     debug: bool = False) -> tuple[str, list[dict]]:
        """翻译UI文字，返回 (译文, 术语列表)"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not text.strip():
            return ""

        # 标点符号直接返回
        if not any(c.isalnum() for c in text):
            return text

        system_prompt = self._build_ui_system_prompt(glossary_text)

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
                                 context_before: List[dict] = None) -> str:
        """构建批次翻译用户提示词（编号输入、编号输出）"""
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
        prompt += f"【请翻译以下文本，共 {n} 句，按编号顺序逐句翻译】\n"
        for i, item in enumerate(items, 1):
            char = item.get('character', '')
            text = item.get('original_text', '').replace('\n', ' ')
            if char:
                prompt += f"{i}. [{char}] {text}\n"
            else:
                prompt += f"{i}. {text}\n"

        prompt += f"""
【输出要求】
- 调用 submit_translations 函数提交结果
- translations 数组必须恰好包含 {n} 条译文，顺序与输入编号 1 到 {n} 一一对应，不要合并、拆分或跳过任何一句
- [角色] 是说话人标记，不要翻译，也不要出现在译文中
- 原文中新出现的游戏专有名词（地名、物品名、技能名等，且术语表中没有的）放入 terms 数组；术语表中已有或没有新名词时传空数组"""

        return prompt

    @staticmethod
    def _parse_tool_response(message, expected: int) -> tuple[Optional[List[str]], List[dict]]:
        """解析 tool calls 响应

        translations 数组长度必须恰好等于 expected，否则返回 (None, [])。
        """
        calls = getattr(message, 'tool_calls', None)
        if not calls:
            return None, []
        try:
            args = json.loads(calls[0].function.arguments)
        except (json.JSONDecodeError, TypeError, AttributeError):
            return None, []

        translations = args.get('translations')
        if not isinstance(translations, list) or len(translations) != expected:
            return None, []

        terms = []
        for t in args.get('terms') or []:
            if isinstance(t, dict) and t.get('en_term') and t.get('cn_term'):
                terms.append({'en_term': str(t['en_term']), 'cn_term': str(t['cn_term'])})

        return [t if isinstance(t, str) else '' for t in translations], terms

    def translate_batch(self, items: List[dict], content_type: str = 'dialogue',
                        glossary_text: str = "", character_profiles: str = "",
                        context_before: List[dict] = None,
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
            system_prompt = self._build_ui_system_prompt(glossary_text)
        else:
            system_prompt = self._build_system_prompt(
                glossary_text=glossary_text,
                character_profile=character_profiles
            )
        user_prompt = self._build_batch_user_prompt(items, context_before)

        if debug:
            print(f'\n{"="*50}')
            print(f'[批次翻译] 共 {len(items)} 句')
            print(f'[批次翻译] 系统提示词:\n{system_prompt[:500]}...')
            print(f'[批次翻译] 用户提示词:\n{user_prompt}')
            print(f'{"="*50}\n')

        # 输出长度按批放大：译文长度通常不超过原文（英→中会缩短），1.2 倍余量确保不被截断
        est_input_tokens = sum(len(it.get('original_text', '')) for it in items) // 3
        max_tokens = max(self.config.max_tokens, int(est_input_tokens * 1.2) + 300)
        if context_window_tokens:
            # 输入 + 输出不得超出模型上下文窗口（deepseek 等服务超限直接返回 400）
            est_total_input = (len(system_prompt) + len(user_prompt)) // 3
            max_tokens = min(max_tokens, max(1000, context_window_tokens - est_total_input))

        # Tool Calls：schema 强制 translations 恰好 n 条，避免编号文本的格式污染
        n = len(items)
        tools = [{
            "type": "function",
            "function": {
                "name": "submit_translations",
                "description": "按输入编号顺序提交全部译文，以及原文中新出现的游戏专有名词术语",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "translations": {
                            "type": "array",
                            "description": f"恰好 {n} 条译文，顺序与输入编号 1 到 {n} 一一对应",
                            "items": {"type": "string"},
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

