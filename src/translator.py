"""AI翻译器 - 使用OpenAI兼容接口进行翻译"""

import re
from typing import List, Optional, Dict, Any
from openai import OpenAI
from dataclasses import dataclass


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

    def __init__(self, config: TranslationConfig):
        self.config = config
        self.client: Optional[OpenAI] = None
        self.prompt_callback: Optional[Callable[[str, str, str], None]] = None
        # prompt_callback(system_prompt, user_prompt, task_type)
        self._init_client()

    def _init_client(self):
        if self.config.api_key:
            self.client = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.api_base
            )

    def update_config(self, config: TranslationConfig):
        self.config = config
        self._init_client()

    def _build_system_prompt(self, character: str = "",
                              glossary_text: str = "",
                              character_profile: str = "") -> str:
        """构建系统提示词"""
        prompt = """你是一位资深的游戏本地化翻译家。请将以下文本翻译成简体中文。

核心规则：
- 只翻译【请翻译以下文本】中的内容，【前文参考】和【后文参考】不翻译
- 如果原文只有标点符号或空白，直接原样返回，不要翻译

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

        if self.prompt_callback:
            self.prompt_callback(system_prompt, user_prompt, 'dialogue')

        if debug:
            print(f'\n{"="*50}')
            print(f'[翻译] 角色: {character or "旁白"}')
            print(f'[翻译] 原文: {text}')
            print(f'[翻译] 系统提示词:\n{system_prompt[:500]}...')
            print(f'[翻译] 用户提示词:\n{user_prompt}')
            print(f'{"="*50}\n')

        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                timeout=self.config.timeout
            )

            raw = response.choices[0].message.content.strip()
            translated, terms = self._parse_translation_response(raw)

            if debug:
                print(f'[翻译] 翻译结果: {translated}')
                if terms:
                    print(f'[翻译] 术语: {terms}')

            return translated, terms

        except Exception as e:
            raise Exception(f"翻译失败: {str(e)}")

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

        if self.prompt_callback:
            self.prompt_callback(system_prompt, user_prompt, 'name')

        if debug:
            print(f'\n{"="*50}')
            print(f'[人名翻译] 原文: {name}')
            print(f'{"="*50}\n')

        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=self.config.max_tokens,
                timeout=self.config.timeout
            )

            result = response.choices[0].message.content
            if result:
                result = result.strip().replace('"', '').replace("'", '').replace('。', '')
            else:
                result = ''

            if debug:
                print(f'[人名翻译] 翻译结果: {result}')

            return result

        except Exception as e:
            raise Exception(f"人名翻译失败: {str(e)}")

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

        system_prompt = """你是一位游戏本地化翻译家。请将以下文本翻译成简体中文。

核心规则：
- 如果原文只有标点符号或空白，直接原样返回
- 按钮和菜单文字要简洁，符合中文游戏用语习惯

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

        user_prompt = f"""请翻译：\n{text}

【输出格式】
第一行：翻译结果（只输出译文）
如果原文中有专有名词，空一行输出：
【术语】
原文1 → 译文1
没有专有名词则不输出【术语】部分。"""

        if self.prompt_callback:
            self.prompt_callback(system_prompt, user_prompt, 'ui')

        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=self.config.max_tokens,
                timeout=self.config.timeout
            )

            raw = response.choices[0].message.content.strip()
            translated, terms = self._parse_translation_response(raw)
            return translated, terms

        except Exception as e:
            raise Exception(f"UI翻译失败: {str(e)}")

    def analyze_text(self, prompt: str) -> str:
        """分析文本（不使用翻译系统提示词）"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not prompt.strip():
            return ""

        system_prompt = "你是一个专业的文本分析师。请按照用户的要求进行分析，直接输出分析结果，不要添加额外的解释。"

        if self.prompt_callback:
            self.prompt_callback(system_prompt, prompt, 'analysis')

        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                timeout=self.config.timeout
            )

            return response.choices[0].message.content.strip()

        except Exception as e:
            raise Exception(f"分析失败: {str(e)}")

    def test_connection(self) -> Dict[str, Any]:
        """测试API连接"""
        if not self.client:
            return {'success': False, 'error': '请先配置API Key'}

        try:
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=[{"role": "user", "content": "Hello, this is a test."}],
                max_tokens=10
            )

            return {
                'success': True,
                'model': self.config.model,
                'response': response.choices[0].message.content
            }

        except Exception as e:
            return {'success': False, 'error': str(e)}

