"""人名翻译 + 人物分析服务（从 name_panel 抽取，无 UI 依赖）

UI 层通过 hooks 接收行状态/进度回调：
    service.on_row_busy = lambda en_name: ...      # 开始处理（同步）
    service.on_row_done = async def(en_name): ...  # 处理结束刷新行（协程）
    service.on_progress = lambda i, total, text: ...
"""
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor

from database import ProjectDatabase
from logger import TranslationLogger
from translation_service import TranslationService
from translator import AITranslator, FatalAPIError

PROFILE_KEYS = ['性格特点', '外貌特征', '说话风格', '行为习惯',
                '人物关系', '背景故事', '角色定位', '翻译建议']


def calc_batch_size(total_lines: int, max_context_k: int) -> int:
    """根据模型上下文大小动态计算每段台词数

    - 可用 token = 模型上下文 - 提示词开销 - 输出预留
    - 每条台词约 20 token（英文平均）
    - 为安全起见，使用 60% 的可用空间
    """
    total_tokens = max_context_k * 1024
    # 提示词开销约 800 token，输出预留 2000 token
    prompt_overhead = 800
    output_reserve = 2000
    available = total_tokens - prompt_overhead - output_reserve
    tokens_per_line = 20
    batch_size = max(10, int(available * 0.6 / tokens_per_line))
    return min(batch_size, total_lines)


def extract_name(text: str) -> str:
    """从 AI 返回中提取人名翻译"""
    match = re.search(r'【人名翻译】\s*\n\s*中文名[：:]\s*(.+)', text)
    if match:
        name = match.group(1).strip()
        name = name.replace('"', '').replace("'", '').replace('。', '')
        return name
    return ''


def parse_profile(text: str) -> dict:
    """把 AI 结构化输出解析为 profile dict；解析不到任何键返回 None"""
    profile = {}
    current_key = None
    current_value = []

    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        found_key = False
        for key in PROFILE_KEYS:
            if line.startswith(key + '：') or line.startswith(key + ':'):
                if current_key:
                    profile[current_key] = '\n'.join(current_value).strip()
                current_key = key
                value_part = line.split('：', 1)[-1].split(':', 1)[-1].strip()
                current_value = [value_part] if value_part else []
                found_key = True
                break
        if not found_key and current_key:
            current_value.append(line)

    if current_key:
        profile[current_key] = '\n'.join(current_value).strip()
    return profile if profile else None


class NameTranslationService:
    """融合流程：一次 AI 调用同时完成人名翻译和人物分析（顺序处理）"""

    def __init__(self, db: ProjectDatabase, translator: AITranslator,
                 translation_service: TranslationService,
                 logger: TranslationLogger, max_context_k: int = 8):
        self.db = db
        self.translator = translator
        self.translation_service = translation_service
        self.logger = logger
        self.max_context_k = max_context_k
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._cancel = False

        # UI hooks（皆可 None）
        self.on_row_busy = None    # (en_name) -> None
        self.on_row_done = None    # (en_name) -> coroutine
        self.on_progress = None    # (i, total, text) -> None

    def stop(self):
        self._cancel = True

    def close(self):
        """关闭内部线程池。

        names.py 以单例缓存本服务，项目切换/配置变化重建实例时
        必须先 close 旧实例，否则 ThreadPoolExecutor 线程持续累积。
        """
        self._executor.shutdown(wait=False)

    @property
    def cancelled(self) -> bool:
        return self._cancel

    async def _emit_busy(self, en_name: str):
        if self.on_row_busy:
            self.on_row_busy(en_name)

    async def _emit_done(self, en_name: str):
        if self.on_row_done:
            await self.on_row_done(en_name)

    async def translate_and_analyze(self, en_name: str, variable: str = None):
        """翻译人名 + 分析角色。台词超长自动分段：
        第1段翻译人名+分析，后续段补充分析，最终合并。

        variable：角色变量名（同名显示名的角色靠它精确定位）
        FatalAPIError 向上抛出以中止批量任务；其他错误记日志后返回。
        """
        loop = asyncio.get_event_loop()

        # 占位符（如 [mc_name]、[hero]）不需要翻译人名，但仍需分析角色
        is_placeholder = en_name.startswith('[') and en_name.endswith(']')
        if is_placeholder:
            self.logger.info(f'占位符 {en_name}，跳过人名翻译，仅分析角色', panel='names')
            await loop.run_in_executor(
                None, lambda: self.db.update_character_cn_name(en_name, en_name, variable=variable)
            )

        await self._emit_busy(en_name)

        try:
            # 加载该角色的台词（有变量名直接查，同名显示名不会串）
            def _load_lines():
                var_name = variable
                if not var_name:
                    variable_map = self.db.get_variable_map()
                    for var, display in variable_map.items():
                        if display == en_name:
                            var_name = var
                            break
                if var_name:
                    dialogues = self.db.get_dialogues_page(0, 999999, character=var_name)[0]
                    return [d['original_text'] for d in dialogues]
                return []

            char_lines = await loop.run_in_executor(None, _load_lines)

            if not char_lines:
                self.logger.info(f'{en_name} 没有台词，仅翻译人名', panel='names')
                await self.translation_service.translate_single(
                    item_id=0, content_type='name', original_text=en_name
                )
                empty_profile = {'性格特征': '该角色没有台词', '说话风格': '无', '背景': '无'}
                await loop.run_in_executor(
                    None, lambda: self.db.save_profile(en_name, empty_profile, variable=variable)
                )
                await self._emit_done(en_name)
                return

            # 获取人名词典用于参考
            def _load_dict_text():
                glossary_text = self.db.get_glossary_for_prompt()
                char_prompt = self.db.get_characters_for_prompt()
                text = ""
                if char_prompt:
                    text += char_prompt + "\n"
                if glossary_text:
                    text += glossary_text
                return text

            dict_text = await loop.run_in_executor(None, _load_dict_text)

            batch_size = calc_batch_size(len(char_lines), self.max_context_k)
            batches = [char_lines[i:i+batch_size] for i in range(0, len(char_lines), batch_size)]
            total_batches = len(batches)

            cn_name = ''
            summaries = []

            for batch_idx, batch_lines in enumerate(batches):
                if self._cancel:
                    break

                lines_text = '\n'.join([f'"{line}"' for line in batch_lines])

                if batch_idx == 0:
                    self.logger.info(
                        f'[{batch_idx+1}/{total_batches}] {"分析" if is_placeholder else "翻译+分析"} {en_name}'
                        f'（{len(char_lines)}条台词，每段{batch_size}条，上下文{self.max_context_k}K）',
                        panel='names'
                    )

                    if is_placeholder:
                        prompt = self._build_analyze_only_prompt(
                            en_name, lines_text, batch_idx, total_batches)
                    else:
                        prompt = self._build_translate_analyze_prompt(
                            en_name, lines_text, batch_idx, total_batches, dict_text)
                else:
                    self.logger.info(f'[{batch_idx+1}/{total_batches}] 补充分析 {en_name}', panel='names')
                    prompt = self._build_continue_prompt(
                        en_name, lines_text, batch_idx, total_batches, summaries)

                result = await loop.run_in_executor(
                    self._executor,
                    lambda p=prompt: self.translator.analyze_text(prompt=p)
                )

                if batch_idx == 0:
                    cn_name = extract_name(result)
                summaries.append(result)

            # 保存人名翻译（占位符不翻译）
            if cn_name and not is_placeholder:
                await loop.run_in_executor(
                    None, lambda: self.db.update_character_cn_name(en_name, cn_name, variable=variable)
                )
                self.logger.info(f'人名: {en_name} -> {cn_name}', panel='names')

            # 合并所有分析结果
            if summaries:
                if len(summaries) == 1:
                    profile = parse_profile(summaries[0])
                else:
                    profile = await self._merge_summaries(en_name, summaries)

                if profile:
                    await loop.run_in_executor(
                        None, lambda: self.db.save_profile(en_name, profile, variable=variable)
                    )
                    self.logger.info(f'{en_name} 分析完成', panel='names')

            await self._emit_done(en_name)

        except FatalAPIError:
            await self._emit_done(en_name)
            raise
        except Exception as e:
            self.logger.error(f'{en_name} 翻译+分析失败: {e}', panel='names')
            await self._emit_done(en_name)

    async def translate_all(self) -> dict:
        """翻译全部未翻译人名 + 补充分析全部未分析角色（顺序处理）

        Returns: {'completed': int, 'total': int, 'stopped': bool, 'nothing': bool}
        """
        self._cancel = False
        loop = asyncio.get_event_loop()
        completed_count = 0

        def _get_todo():
            chars = self.db.get_untranslated_characters()
            profiles = self.db.get_all_profiles()
            return chars, profiles

        chars_todo, profiles = await loop.run_in_executor(None, _get_todo)
        total = len(chars_todo)

        if total == 0:
            all_chars = await loop.run_in_executor(None, self.db.get_characters)
            unanalyzed = [(c['display_name'], c['variable'] or None) for c in all_chars
                          if c['display_name'] not in profiles and not c['is_placeholder']]
            if not unanalyzed:
                return {'completed': 0, 'total': 0, 'stopped': False, 'nothing': True}
            total = len(unanalyzed)
            self.logger.info(f'人名已全部翻译，补充分析 {total} 个角色', panel='names')
            todo = unanalyzed
        else:
            self.logger.info(f'开始翻译+分析 {total} 个角色', panel='names')
            todo = [(c['display_name'], c['variable'] or None) for c in chars_todo]

        for i, (name, var) in enumerate(todo):
            if self._cancel:
                break
            if self.on_progress:
                self.on_progress(i, total, f'翻译+分析: {i+1}/{total} {name}')
            try:
                await self.translate_and_analyze(name, variable=var)
                completed_count += 1
            except FatalAPIError as e:
                self.logger.error(f'API 致命错误，批量任务中止: {e}', panel='names')
                self._cancel = True
                raise
            except Exception as e:
                self.logger.error(f'{name} 翻译+分析失败: {e}', panel='names')

        self.logger.info(f'翻译+分析完成: {completed_count}/{total}', panel='names')
        return {'completed': completed_count, 'total': total,
                'stopped': self._cancel, 'nothing': False}

    async def _merge_summaries(self, name: str, summaries: list) -> dict:
        """合并多段分析结果为最终人物特征"""
        loop = asyncio.get_event_loop()
        summaries_text = '\n\n'.join([f'第{i+1}批分析：\n{s}' for i, s in enumerate(summaries)])

        prompt = f"""根据以下对角色 "{name}" 的多段分析，合并为一个完整的人物特征报告。

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

        result = await loop.run_in_executor(
            self._executor,
            lambda: self.translator.analyze_text(prompt=prompt)
        )
        return parse_profile(result)

    # ---- 提示词构建 ----

    @staticmethod
    def _build_analyze_only_prompt(en_name, lines_text, batch_idx, total_batches):
        return f"""【任务类型：文本分析，不是翻译】

分析游戏角色 "{en_name}" 的台词，总结人物特征。

重要规则：
- 方括号包裹的内容（如 [cleo_name]、[mc_name]）是 Ren'Py 变量占位符，绝对不要翻译，直接保留原样
- 花括号包裹的内容（如 {{i}}、{{/i}}、{{size=-5}}）是 Ren'Py 标记标签，不是文本内容，忽略它们
- $ 开头的内容是代码变量，不要翻译

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

    @staticmethod
    def _build_translate_analyze_prompt(en_name, lines_text, batch_idx, total_batches, dict_text):
        return f"""你是一位资深的游戏本地化专家。请同时完成以下两个任务：

## 任务1：翻译人名
将角色名 "{en_name}" 翻译成中文。

翻译时请考虑：
- 从台词中推断角色的性格、背景、文化特征
- 音译、意译还是混合？选择最符合角色气质的方式
- 是否有双关、谐音、文化梗需要保留？
- 与已有的人名翻译保持风格一致

重要规则：
- 方括号包裹的内容（如 [cleo_name]、[mc_name]）是 Ren'Py 变量占位符，绝对不要翻译，直接保留原样
- 花括号包裹的内容（如 {{i}}、{{/i}}、{{size=-5}}）是 Ren'Py 标记标签，不是文本内容，忽略它们
- $ 开头的内容是代码变量，不要翻译

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

    @staticmethod
    def _build_continue_prompt(en_name, lines_text, batch_idx, total_batches, summaries):
        return f"""【任务类型：文本分析，不是翻译】

继续分析游戏角色 "{en_name}" 的更多台词（第{batch_idx+1}批，共{total_batches}批）。

之前已有的分析摘要：
{chr(10).join(summaries[-2:]) if summaries else "无"}

新的台词：
{lines_text}

请简要总结这批台词中展现的新特征（性格、说话风格、关系变化等），补充到已有分析中。不要重复已有内容。"""
