"""AI翻译器 - 业务门面

LLM 调用封装（client 生命周期、重试、错误分类）见 llm_client.py；
提示词模板见 prompts.py。本模块只保留翻译业务流程与响应解析。
"""

import json
import difflib
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

    # ---- 内容级校验阈值 ----
    _MISALIGN_SIM = 0.85    # 译文与其他条目原文相似度达到该值才怀疑贴错行
    _MISALIGN_MARGIN = 0.1  # 且需明显高于与本条原文的相似度（两条原文本身相似时不误报）
    _SHORT_ORIG = 15        # 原文达到该长度才做长度悬殊检查（短句伸缩空间大）
    _MIN_LEN_RATIO = 0.12   # 译文长度 < 原文 × 0.12 视为悬殊（英译中通常 0.5~0.8）
    _MAX_LEN_RATIO = 4.0    # 译文长度 > 原文 × 4 且绝对超出 40 字符视为悬殊

    @staticmethod
    def _parse_tool_response(message, items: List[dict]
                             ) -> tuple[Dict[int, str], List[dict], Dict[int, str]]:
        """解析 tool calls 响应

        translations 是 [{id, translation}, ...]，按 id 放回位置（id 从 1 开始）。
        返回 (已放置译文 {0基索引: 译文}, 术语, 存疑原因 {0基索引: reason})。

        不再整批判废：结构非法（无 tool_calls / JSON 损坏 / translations 非数组）
        时返回 ({}, [], {})；id 越界/重复/译文为空的条目单独跳过（成为未匹配）。
        已放置译文再做内容级校验，存疑的移出结果并记入原因，由上层暂存核验。
        """
        expected = len(items)
        calls = getattr(message, 'tool_calls', None)
        if not calls:
            return {}, [], {}
        try:
            args = json.loads(calls[0].function.arguments)
        except (json.JSONDecodeError, TypeError, AttributeError):
            return {}, [], {}

        raw = args.get('translations')
        if not isinstance(raw, list):
            return {}, [], {}

        # 按 id 对齐（兼容模型乱序返回）；无法落位的条目跳过而非整批判废
        placed: Dict[int, str] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            idx = entry.get('id')
            text = entry.get('translation')
            if not isinstance(idx, int) or not (1 <= idx <= expected) \
                    or (idx - 1) in placed:
                continue
            # 译文必须是非空字符串，否则该条静默漏译
            if not isinstance(text, str) or not text:
                continue
            placed[idx - 1] = text

        suspicious = AITranslator._content_sanity_check(items, placed)
        for idx in suspicious:
            placed.pop(idx, None)

        terms = []
        for t in args.get('terms') or []:
            if isinstance(t, dict) and t.get('en_term') and t.get('cn_term'):
                terms.append({'en_term': str(t['en_term']), 'cn_term': str(t['cn_term'])})

        return placed, terms, suspicious

    @classmethod
    def _content_sanity_check(cls, items: List[dict],
                              placed: Dict[int, str]) -> Dict[int, str]:
        """内容级校验：贴错行与长度悬殊，返回 {0基索引: 原因}"""
        suspicious: Dict[int, str] = {}
        originals = [it.get('original_text', '') for it in items]
        for idx, text in placed.items():
            own = originals[idx]
            # 贴错行：译文与另一条原文高度相似，且明显高于与本条原文的相似度
            # （若两条原文本身相似，本条相似度同样高，不会误报）
            own_sim = difflib.SequenceMatcher(None, text, own).ratio()
            for j, other in enumerate(originals):
                if j == idx or not other:
                    continue
                sim = difflib.SequenceMatcher(None, text, other).ratio()
                if sim >= cls._MISALIGN_SIM and sim > own_sim + cls._MISALIGN_MARGIN:
                    suspicious[idx] = f'疑似贴错行：与第 {j + 1} 条原文高度相似'
                    break
            if idx in suspicious:
                continue
            # 长度悬殊：漏译大半或多句合并进一条的典型信号
            lo, lt = len(own), len(text)
            if lo >= cls._SHORT_ORIG and (
                    lt < lo * cls._MIN_LEN_RATIO
                    or (lt > lo * cls._MAX_LEN_RATIO and lt - lo > 40)):
                suspicious[idx] = f'译文长度与原文悬殊（{lt} vs {lo} 字符）'
        return suspicious

    def translate_batch(self, items: List[dict], content_type: str = 'dialogue',
                        glossary_text: str = "", character_profiles: str = "",
                        context_before: List[dict] = None,
                        style_guide: str = "",
                        context_window_tokens: Optional[int] = None,
                        debug: bool = False) -> tuple[Optional[List[str]], List[dict]]:
        """批次翻译多句文本

        items: [{'original_text': ..., 'character': ...}]
        返回 (已匹配译文 {0基索引: 译文}, 术语列表, 未译出原因 {0基索引: reason})。
        解析不完整（句数不匹配）会自动重试并按 id 合并多次尝试的结果；
        重试耗尽仍缺失或内容校验存疑的条目不进 merged，原因进 fail_reasons。
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

        # 解析失败（句数不匹配）也重试：模型偶发漏译/多译，重新请求通常能恢复；
        # 多次尝试按 id 合并（同一句以最近一次返回为准，与旧版"成功的整批结果生效"
        # 语义一致），最后仍缺/存疑的条目随原因一并返回
        merged: Dict[int, str] = {}
        terms_all: List[dict] = []
        seen_terms: set = set()
        suspicious_all: Dict[int, str] = {}
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
            placed, terms, suspicious = self._parse_tool_response(message, items)
            merged.update(placed)
            for idx in placed:
                suspicious_all.pop(idx, None)  # 后续尝试正常译出的不再算存疑
            suspicious_all.update(suspicious)
            for t in terms:
                if t['en_term'] not in seen_terms:
                    seen_terms.add(t['en_term'])
                    terms_all.append(t)
            if len(merged) == n:
                break
            if attempt < self.MAX_RETRIES:
                print(f'[批次翻译] {n - len(merged)}/{n} 句未匹配，'
                      f'进行第 {attempt + 1}/{self.MAX_RETRIES} 次尝试...')

        # 兜底：剥掉模型误抄进译文开头的说话人标记（[角色]/【角色】/角色:）
        for idx, text in merged.items():
            merged[idx] = _strip_speaker_prefix(text, items[idx].get('character', ''))

        fail_reasons = {i: suspicious_all.get(i, '模型未返回该句译文')
                        for i in range(n) if i not in merged}

        if debug:
            if fail_reasons:
                print(f'[批次翻译] {len(merged)}/{n} 句成功，'
                      f'{len(fail_reasons)} 句未译出（已重试 {self.MAX_RETRIES} 次）')
            else:
                print(f'[批次翻译] 成功解析 {n} 句')
            if terms_all:
                print(f'[批次翻译] 术语: {terms_all}')

        return merged, terms_all, fail_reasons

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
