"""AI翻译器 - 业务门面

LLM 调用封装（client 生命周期、重试、错误分类）见 llm_client.py；
提示词模板见 prompts.py。本模块只保留翻译业务流程与响应解析。
"""

import json
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Callable

from llm_client import LLMClient, FatalAPIError  # noqa: F401  (FatalAPIError 供旧路径 import)
from prompts import (
    ANALYSIS_SYSTEM_PROMPT,
    STYLE_GUIDE_PROMPT,
    build_batch_user_prompt,
    build_name_system_prompt,
    build_name_user_prompt,
    build_system_prompt,
    build_ui_system_prompt,
    build_ui_user_prompt,
    build_user_prompt,
)
from token_budget import TokenBudget, count_tokens


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


def _strip_speaker_prefix(text: str, character: str) -> str:
    """剥掉模型误加/误抄进译文开头的说话人标记（[角色]/【角色】/角色:）"""
    if character and text:
        for prefix in (f'[{character}]', f'【{character}】', f'{character}:', f'{character}：'):
            if text.startswith(prefix):
                return text[len(prefix):].strip()
    return text


def clean_name_result(raw: str) -> str:
    """清洗人名翻译结果：去掉模型误带的引号与句号"""
    return raw.replace('"', '').replace("'", '').replace('。', '')


class AITranslator:
    """AI翻译器"""

    MAX_RETRIES = LLMClient.MAX_RETRIES  # 可重试错误的最大尝试次数

    def __init__(self, config: TranslationConfig):
        self.config = config
        self._llm = LLMClient(config)

    @property
    def client(self):
        """底层 OpenAI client（未配置 API Key 时为 None）"""
        return self._llm.client

    @property
    def api_log_callback(self) -> Optional[Callable[[dict, dict, str], None]]:
        return self._llm.api_log_callback

    @api_log_callback.setter
    def api_log_callback(self, callback):
        self._llm.api_log_callback = callback

    def update_config(self, config: TranslationConfig):
        self.config = config
        self._llm.update_config(config)

    def chat_completion(self, messages: list, temperature: float = None,
                        max_tokens: int = None, tools: list = None,
                        tool_choice=None, return_message: bool = False,
                        task_type: str = ''):
        """公开的底层 LLM 调用接口（temperature/max_tokens 缺省取 config）"""
        return self._llm.chat_completion(
            messages=messages, temperature=temperature, max_tokens=max_tokens,
            tools=tools, tool_choice=tool_choice,
            return_message=return_message, task_type=task_type)

    def _call_api(self, messages: list, temperature: float, max_tokens: int,
                  tools: list = None, tool_choice: dict = None,
                  return_message: bool = False, task_type: str = ''):
        """兼容旧签名，委托给 LLMClient.chat_completion"""
        return self._llm.chat_completion(
            messages=messages, temperature=temperature, max_tokens=max_tokens,
            tools=tools, tool_choice=tool_choice,
            return_message=return_message, task_type=task_type)

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

        system_prompt = build_system_prompt(
            glossary_text=glossary_text,
            character_profile=character_profile,
            style_guide=style_guide,
        )
        user_prompt = build_user_prompt(
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
        translated = _strip_speaker_prefix(translated, character)

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

        system_prompt = build_name_system_prompt(glossary_text)
        user_prompt = build_name_user_prompt(name)

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
        result = clean_name_result(raw)

        if debug:
            print(f'[人名翻译] 翻译结果: {result}')

        return result

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

        system_prompt = build_ui_system_prompt(glossary_text, style_guide=style_guide)
        user_prompt = build_ui_user_prompt(text)

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
            # 译文必须是非空字符串：模型返回 null/数字/空串时若静默填 ''，
            # 能通过下面的全覆盖检查而不触发重试，下游 if not text: continue
            # 又会跳过，导致该条永远漏译；故与其他校验失败一样返回 (None, []) 触发重试
            if not isinstance(text, str) or not text:
                return None, []
            result[idx - 1] = text

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
        解析失败（句数不匹配）会自动重试，重试耗尽仍失败时返回 (None, [])。
        """
        if not self.client:
            raise ValueError("请先配置API Key")
        if not items:
            return [], []

        if content_type == 'ui':
            system_prompt = build_ui_system_prompt(glossary_text, style_guide=style_guide)
        else:
            system_prompt = build_system_prompt(
                glossary_text=glossary_text,
                character_profile=character_profiles,
                style_guide=style_guide,
            )
        user_prompt = build_batch_user_prompt(items, context_before, content_type)

        if debug:
            print(f'\n{"="*50}')
            print(f'[批次翻译] 共 {len(items)} 句')
            print(f'[批次翻译] 系统提示词:\n{system_prompt[:500]}...')
            print(f'[批次翻译] 用户提示词:\n{user_prompt}')
            print(f'{"="*50}\n')

        # 输出长度按批放大：译文长度通常不超过原文（英→中会缩短），1.2 倍余量确保不被截断；
        # 给出上下文窗口时再钳制到 窗口 - 实际输入（deepseek 等服务超限直接返回 400）
        est_input_tokens = sum(count_tokens(it.get('original_text', '')) for it in items)
        actual_input = None
        if context_window_tokens:
            actual_input = count_tokens(system_prompt) + count_tokens(user_prompt)
        max_tokens = TokenBudget(context_window_tokens or 0).output_max_tokens(
            est_input_tokens, self.config.max_tokens, input_tokens=actual_input)

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

        # 解析失败（句数不匹配）也重试：模型偶发漏译/多译，重新请求通常能恢复
        translated_list, terms = None, []
        for attempt in range(1, self.MAX_RETRIES + 1):
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
                break
            if attempt < self.MAX_RETRIES:
                print(f'[批次翻译] 解析失败（句数不匹配），进行第 {attempt + 1}/{self.MAX_RETRIES} 次尝试...')

        if translated_list is not None:
            # 兜底：剥掉模型误抄进译文开头的说话人标记（[角色]/【角色】/角色:）
            translated_list = [
                _strip_speaker_prefix(text, it.get('character', ''))
                for it, text in zip(items, translated_list)
            ]

        if debug:
            if translated_list is None:
                print(f'[批次翻译] 解析失败（句数不匹配），已重试 {self.MAX_RETRIES} 次仍失败')
            else:
                print(f'[批次翻译] 成功解析 {len(translated_list)} 句')
            if terms:
                print(f'[批次翻译] 术语: {terms}')

        return translated_list, terms

    def analyze_text(self, prompt: str, max_tokens: int = None) -> str:
        """分析文本（不使用翻译系统提示词）"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not prompt.strip():
            return ""

        return self._call_api(
            messages=[
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=self.config.temperature,
            max_tokens=max_tokens or self.config.max_tokens,
            task_type='analysis',
        )

    def generate_style_guide(self, sample_text: str) -> str:
        """根据台词抽样生成作品风格指南"""
        if not sample_text.strip():
            return ""
        return self.analyze_text(STYLE_GUIDE_PROMPT.format(sample=sample_text))

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
