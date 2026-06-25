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
            # 获取变量名映射
            variable_map = character_dict.get('__variable_map__', {})

            # 人名词典（只包含有翻译的人名）
            dict_text = "\n\n人名翻译词典（翻译时必须使用这些中文名）：\n"
            has_names = False
            for en_name, cn_name in character_dict.items():
                # 跳过所有特殊键
                if en_name.startswith('__'):
                    continue
                # 只显示有翻译的人名（确保是字符串且非空）
                if not isinstance(cn_name, str) or not cn_name.strip():
                    continue
                # 跳过占位符（如 [mc_name] 但不是 [Mika]）
                if en_name.startswith('[') and en_name.endswith(']') and '_name' in en_name:
                    continue
                dict_text += f"- {en_name} → {cn_name}\n"
                has_names = True
            if has_names:
                prompt += dict_text

            # 当前角色的特征
            profiles = character_dict.get('__profiles__', {})
            variable_map = character_dict.get('__variable_map__', {})

            if character and profiles:
                # 尝试多种方式查找角色特征
                profile = None
                matched_key = None

                # 1. 直接匹配
                if character in profiles:
                    profile = profiles[character]
                    matched_key = character
                # 2. 尝试匹配 [character_name] 格式
                elif f'[{character}_name]' in profiles:
                    profile = profiles[f'[{character}_name]']
                    matched_key = f'[{character}_name]'
                # 3. 通过变量名映射查找显示名
                elif character in variable_map:
                    display_name = variable_map[character]
                    if display_name in profiles:
                        profile = profiles[display_name]
                        matched_key = display_name
                # 4. 遍历查找包含character的键
                else:
                    for key in profiles:
                        if character.lower() in key.lower() or key.lower() in character.lower():
                            profile = profiles[key]
                            matched_key = key
                            break

                if profile:
                    prompt += f"\n\n当前说话角色 [{matched_key}] 的人物特征：\n"
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

    def translate_name(self, name: str, debug: bool = False) -> str:
        """翻译人名（简洁提示词）"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not name.strip():
            return ""

        # 检查是否是占位符（如 [mc_name]）
        if name.startswith('[') and name.endswith(']'):
            if debug:
                print(f'\n[人名翻译] 占位符，直接返回: {name}')
            return name  # 占位符直接返回，不翻译

        system_prompt = """你是一个游戏翻译专家。请将游戏人名翻译成中文，只返回中文名，不要解释。

注意：
- 如果输入是占位符（如 [xxx_name]），直接返回原文
- 如果输入是变量名或代码，直接返回原文
- 只翻译真正的人名"""

        user_prompt = f"请将以下人名翻译成中文，只返回中文名：\n{name}"

        # 打印提示词到日志
        if debug:
            print(f'\n{"="*50}')
            print(f'[人名翻译] 原文: {name}')
            print(f'[人名翻译] 系统提示词:\n{system_prompt}')
            print(f'[人名翻译] 用户提示词:\n{user_prompt}')
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

            if debug:
                print(f'[人名翻译] 响应: {response}')

            result = response.choices[0].message.content
            if result:
                result = result.strip()
                # 清理结果，只保留名字
                result = result.replace('"', '').replace("'", '').replace('。', '')
            else:
                result = ''

            if debug:
                print(f'[人名翻译] 翻译结果: {result}')

            return result

        except Exception as e:
            if debug:
                print(f'[人名翻译] 异常: {e}')
            raise Exception(f"人名翻译失败: {str(e)}")

    def translate_ui(self, text: str, character_dict: Dict[str, str] = None, debug: bool = False) -> str:
        """翻译UI文字（简洁提示词）"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not text.strip():
            return ""

        system_prompt = """你是一个游戏字符串翻译专家。请将以下内容翻译成简体中文。

这些字符串可能包含：
- 菜单选项（如 "Start Game"、"Load Game"）
- 按钮文字（如 "OK"、"Cancel"、"Back"）
- 提示文字（如 "Are you sure?"、"Saving..."）
- 任务描述（如 "Go to the market"、"Talk to the guard"）
- 物品名称（如 "Health Potion"、"Sword"）

翻译要求：
- 简洁明了，符合中文游戏用语习惯
- 按钮和菜单文字要简短
- 保持专业术语的一致性
- 保留原意但可以适当本地化
- 只返回翻译结果，不要解释"""

        # 添加人名词典
        if character_dict:
            # 获取变量名映射
            variable_map = character_dict.get('__variable_map__', {})

            # 人名词典（只包含有翻译的人名）
            dict_text = "\n\n人名翻译词典（翻译时必须使用这些中文名）：\n"
            has_names = False
            for en_name, cn_name in character_dict.items():
                # 跳过所有特殊键
                if en_name.startswith('__'):
                    continue
                # 只显示有翻译的人名（确保是字符串且非空）
                if not isinstance(cn_name, str) or not cn_name.strip():
                    continue
                # 跳过占位符（如 [mc_name] 但不是 [Mika]）
                if en_name.startswith('[') and en_name.endswith(']') and '_name' in en_name:
                    continue
                dict_text += f"- {en_name} → {cn_name}\n"
                has_names = True
            if has_names:
                system_prompt += dict_text

        user_prompt = f"请翻译：\n{text}"

        # 打印提示词到日志
        if debug:
            print(f'\n{"="*50}')
            print(f'[UI翻译] 原文: {text}')
            print(f'[UI翻译] 系统提示词:\n{system_prompt}')
            print(f'[UI翻译] 用户提示词:\n{user_prompt}')
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

            result = response.choices[0].message.content.strip()

            if debug:
                print(f'[UI翻译] 翻译结果: {result}')

            return result

        except Exception as e:
            raise Exception(f"UI翻译失败: {str(e)}")

    def analyze_text(self, prompt: str) -> str:
        """分析文本（不使用翻译系统提示词）"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not prompt.strip():
            return ""

        # 分析任务使用简单的系统提示词
        system_prompt = "你是一个专业的文本分析师。请按照用户的要求进行分析，直接输出分析结果，不要添加额外的解释。"

        # 打印提示词到日志
        print(f'\n{"="*50}')
        print(f'[分析] 系统提示词:\n{system_prompt}')
        print(f'\n[分析] 用户提示词:\n{prompt[:500]}...' if len(prompt) > 500 else f'\n[分析] 用户提示词:\n{prompt}')
        print(f'{"="*50}\n')

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

            result = response.choices[0].message.content.strip()
            print(f'[分析] 返回结果:\n{result[:500]}...' if len(result) > 500 else f'[分析] 返回结果:\n{result}')
            return result

        except Exception as e:
            raise Exception(f"分析失败: {str(e)}")

    def translate_text(self, text: str, character: str = "",
                      context_before: List[str] = None,
                      context_after: List[str] = None,
                      character_dict: Dict[str, str] = None,
                      debug: bool = False) -> str:
        """翻译单行文本"""
        if not self.client:
            raise ValueError("请先配置API Key")

        if not text.strip():
            return ""

        system_prompt = self._build_system_prompt(character_dict, character)
        user_prompt = self._build_user_prompt(
            text, character, context_before, context_after
        )

        # 打印提示词到日志（便于调试）
        if debug:
            print(f'\n{"="*50}')
            print(f'[翻译] 角色: {character or "旁白"}')
            print(f'[翻译] 原文: {text}')

            # 显示人名词典部分
            dict_start = system_prompt.find('人名翻译词典')
            if dict_start >= 0:
                dict_end = system_prompt.find('\n\n', dict_start + 1)
                if dict_end < 0:
                    dict_end = len(system_prompt)
                print(f'\n[翻译] 人名词典:\n{system_prompt[dict_start:dict_end]}')

            # 检查是否有人物特征
            if '人物特征' in system_prompt:
                # 提取人物特征部分
                profile_start = system_prompt.find('当前说话角色')
                if profile_start >= 0:
                    print(f'\n[翻译] 人物特征:\n{system_prompt[profile_start:]}')

            print(f'\n[翻译] 用户提示词:\n{user_prompt}')
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

            translated = response.choices[0].message.content.strip()

            if debug:
                print(f'[翻译] 翻译结果: {translated}')

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
