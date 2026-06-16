"""AI翻译器 - 使用OpenAI兼容接口进行翻译"""

import json
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
    context_lines: int = 3  # 上下文行数
    timeout: int = 30


class AITranslator:
    """AI翻译器"""

    def __init__(self, config: TranslationConfig):
        self.config = config
        self.client: Optional[OpenAI] = None
        self._init_client()

    def _init_client(self):
        """初始化OpenAI客户端"""
        if self.config.api_key:
            self.client = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.api_base
            )

    def update_config(self, config: TranslationConfig):
        """更新配置"""
        self.config = config
        self._init_client()

    def _build_system_prompt(self, character_dict: Dict[str, str] = None, character: str = "") -> str:
        """构建系统提示词"""
        prompt = """你是一位资深的文学翻译家，专注于视觉小说和游戏的中文本地化。你的翻译风格追求"信达雅"——忠实原意、表达通顺、文辞优美。

重要规则：
- 只翻译【请只翻译以下文本】中的内容
- 【前文参考】和【后文参考】仅供理解上下文，绝对不要翻译它们
- 只返回翻译结果，不要返回上下文内容

翻译原则：

【信 - 忠实原意】
- 准确传达原文的含义，不遗漏、不添加
- 保留原文的情感色彩和语气强度
- 保留Ren'Py的格式标记（如%s、%d、{w=2}{nw}等）

【达 - 表达通顺】
- 使用地道的中文表达，而非英文直译
- 根据语境调整句式结构，使其符合中文习惯
- 对话要口语化、自然，像真人说话
- 旁白可以书面化，但要有文学性

【雅 - 文辞优美】
- 适当使用成语、俗语、网络用语，增添文采
- 根据角色性格调整语言风格（活泼/冷酷/温柔/痞气等）
- 情感表达要细腻，善用语气词（呢、啊、呀、嘛等）
- 避免生硬的翻译腔，如"的"字滥用、被动句式

人名处理：
- 使用用户提供的翻译词典中的人名
- 如果词典中没有，保留原文人名

返回格式：
- 只返回翻译后的文本，不要添加任何解释或标注
- 如果原文包含换行符\\n，翻译后也保留
- 保留原文的引号格式"""

        # 添加人名词典和角色特征
        if character_dict:
            # 人名词典
            dict_text = "\n\n人名翻译词典：\n"
            for en_name, cn_name in character_dict.items():
                if en_name != '__profiles__':  # 跳过特殊键
                    dict_text += f"- {en_name} → {cn_name}\n"
            prompt += dict_text

            # 当前角色的特征
            profiles = character_dict.get('__profiles__', {})
            if character and character in profiles:
                profile = profiles[character]
                prompt += f"\n\n当前说话角色 [{character}] 的人物特征：\n"
                for key, value in profile.items():
                    if value:
                        prompt += f"- {key}：{value}\n"
                prompt += "\n请根据该角色的特点进行翻译，保持其说话风格和性格特征。"

        return prompt

    def _build_user_prompt(self, text: str, character: str = "",
                          context_before: List[str] = None,
                          context_after: List[str] = None) -> str:
        """构建用户提示词"""
        prompt = ""

        # 添加前文上下文（仅供理解上下文，不需要翻译）
        if context_before:
            prompt += "【前文参考 - 用于理解场景和角色情感】\n"
            for line in context_before[-self.config.context_lines:]:
                prompt += f"{line}\n"
            prompt += "\n"

        # 添加需要翻译的文本
        prompt += f"【请翻译以下文本】\n{text}"

        # 添加角色信息和翻译指引
        if character:
            prompt += f"\n\n【角色信息】\n说话角色：{character}"
            prompt += "\n请根据该角色的身份和性格，使用符合其特点的语言风格翻译。"

        # 添加后文上下文（仅供理解上下文，不需要翻译）
        if context_after:
            prompt += "\n\n【后文参考 - 用于理解剧情走向】\n"
            for line in context_after[:self.config.context_lines]:
                prompt += f"{line}\n"

        prompt += "\n\n请翻译【请翻译以下文本】中的内容，追求信达雅的翻译质量："
        return prompt

    def analyze_text(self, prompt: str) -> str:
        """分析文本（不使用翻译系统提示词）"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not prompt.strip():
            return ""

        # 分析任务使用简单的系统提示词
        system_prompt = "你是一个专业的文本分析师。请按照用户的要求进行分析，直接输出分析结果，不要添加额外的解释。"

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

    def translate_text(self, text: str, character: str = "",
                      context_before: List[str] = None,
                      context_after: List[str] = None,
                      character_dict: Dict[str, str] = None) -> str:
        """翻译单行文本"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not text.strip():
            return ""

        system_prompt = self._build_system_prompt(character_dict, character)
        user_prompt = self._build_user_prompt(
            text, character, context_before, context_after
        )

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

            translated = response.choices[0].message.content.strip()
            return translated

        except Exception as e:
            raise Exception(f"翻译失败: {str(e)}")

    def translate_batch(self, texts: List[Dict[str, Any]],
                       character_dict: Dict[str, str] = None,
                       progress_callback=None) -> List[Dict[str, Any]]:
        """批量翻译文本"""
        results = []
        total = len(texts)

        for i, item in enumerate(texts):
            try:
                translated = self.translate_text(
                    text=item['original_text'],
                    character=item.get('character', ''),
                    context_before=item.get('context_before'),
                    context_after=item.get('context_after'),
                    character_dict=character_dict
                )

                result = {
                    **item,
                    'translated_text': translated,
                    'is_translated': True,
                    'error': None
                }
                results.append(result)

            except Exception as e:
                result = {
                    **item,
                    'translated_text': '',
                    'is_translated': False,
                    'error': str(e)
                }
                results.append(result)

            # 回调进度
            if progress_callback:
                progress_callback(i + 1, total)

        return results

    def test_connection(self) -> Dict[str, Any]:
        """测试API连接"""
        if not self.client:
            return {
                'success': False,
                'error': '请先配置API Key'
            }

        try:
            # 发送一个简单的测试请求
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "user", "content": "Hello, this is a test."}
                ],
                max_tokens=10
            )

            return {
                'success': True,
                'model': self.config.model,
                'response': response.choices[0].message.content
            }

        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }


class CharacterDictionary:
    """人名词典管理"""

    def __init__(self, dict_file: str = "character_dict.json"):
        self.dict_file = dict_file
        self.dictionary: Dict[str, str] = {}
        self.load()

    def load(self):
        """加载词典"""
        try:
            with open(self.dict_file, 'r', encoding='utf-8') as f:
                self.dictionary = json.load(f)
        except FileNotFoundError:
            self.dictionary = {}

    def save(self):
        """保存词典"""
        with open(self.dict_file, 'w', encoding='utf-8') as f:
            json.dump(self.dictionary, f, ensure_ascii=False, indent=2)

    def add(self, english_name: str, chinese_name: str):
        """添加人名翻译"""
        self.dictionary[english_name] = chinese_name
        self.save()

    def remove(self, english_name: str):
        """删除人名翻译"""
        if english_name in self.dictionary:
            del self.dictionary[english_name]
            self.save()

    def update_from_characters(self, characters: List[Any]):
        """从角色列表更新词典"""
        for char in characters:
            if char.name not in self.dictionary:
                # 默认使用原名
                self.dictionary[char.name] = char.name
        self.save()

    def get_dict(self) -> Dict[str, str]:
        """获取词典"""
        return self.dictionary.copy()

    def get_formatted(self) -> str:
        """获取格式化的词典文本"""
        if not self.dictionary:
            return "暂无人名词典"

        lines = ["人名词典："]
        for en, cn in sorted(self.dictionary.items()):
            lines.append(f"  {en} → {cn}")
        return "\n".join(lines)
