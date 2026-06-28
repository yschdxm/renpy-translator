"""人名翻译 + 人物分析面板（融合为一个工作流）"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from nicegui import ui


def _safe(fn, *args, **kwargs):
    """安全执行 UI 操作"""
    try:
        return fn(*args, **kwargs)
    except (RuntimeError, AttributeError):
        return None

from database import ProjectDatabase
from translation_service import TranslationService
from translator import AITranslator
from logger import TranslationLogger
from components.paginated_table import PaginatedTable
from components.progress_panel import ProgressPanel
from components.log_panel import LogPanel


class NamePanel:
    """人名翻译 + 人物分析面板

    翻译人名后自动分析该角色，两个任务融合在一个流程中。
    """

    def __init__(self, logger: TranslationLogger):
        self.logger = logger
        self.db: ProjectDatabase = None
        self.translation_service: TranslationService = None
        self.translator: AITranslator = None
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._max_context_k: int = 8
        self._on_task_state_change: callable = None  # (running: bool) -> None

        self.table: PaginatedTable = None
        self.progress: ProgressPanel = None
        self.log_panel: LogPanel = None

        self.translate_all_btn: ui.button = None
        self.stop_btn: ui.button = None
        self._cancel = False
        self._processing_names: set = set()  # 正在处理的人名

    def set_db(self, db: ProjectDatabase):
        self.db = db

    def set_translation_service(self, service: TranslationService):
        self.translation_service = service

    def set_translator(self, translator: AITranslator):
        self.translator = translator

    def set_max_context(self, max_context_k: int):
        """设置模型最大上下文（单位K）"""
        self._max_context_k = max_context_k

    def _calc_batch_size(self, total_lines: int) -> int:
        """根据模型上下文大小动态计算每段台词数

        计算逻辑：
        - 可用 token = 模型上下文 - 提示词开销 - 输出预留
        - 每条台词约 20 token（英文平均）
        - 为安全起见，使用 60% 的可用空间
        """
        total_tokens = self._max_context_k * 1024
        # 提示词开销约 800 token，输出预留 2000 token
        prompt_overhead = 800
        output_reserve = 2000
        available = total_tokens - prompt_overhead - output_reserve
        # 使用 60% 安全系数，每条约 20 token
        tokens_per_line = 20
        batch_size = max(10, int(available * 0.6 / tokens_per_line))
        # 不超过总台词数
        return min(batch_size, total_lines)

    def create(self, container: ui.column):
        with container:
            with ui.row().classes('w-full items-center gap-2'):
                self.stats_label = ui.label('请先打开项目').classes('text-subtitle1')
                ui.space()
                self.translate_all_btn = ui.button(
                    '🌐 全部翻译+分析', color='primary',
                    on_click=self._translate_all
                )
                self.stop_btn = ui.button(
                    '⏹ 停止', color='red',
                    on_click=self._stop
                )
                _safe(self.stop_btn.set_visibility, False)
                ui.button('🔄 刷新', on_click=self.async_refresh).props('flat dense')

            self.table = PaginatedTable(
                columns=[
                    {'name': 'index', 'label': '#', 'field': 'index', 'sortable': True},
                    {'name': 'variable', 'label': '变量名', 'field': 'variable'},
                    {'name': 'original', 'label': '原文人名', 'field': 'original'},
                    {'name': 'translated', 'label': '中文名', 'field': 'translated'},
                    {'name': 'lines', 'label': '台词数', 'field': 'lines', 'sortable': True},
                    {'name': 'name_status', 'label': '翻译', 'field': 'name_status'},
                    {'name': 'analysis_status', 'label': '分析', 'field': 'analysis_status'},
                    {'name': 'action', 'label': '操作', 'field': 'action'},
                ],
                page_size=50,
                row_key='index'
            )

            self.table.add_slot('body-cell-translated', '''
                <q-td :props="props">
                    <q-input v-model="props.row.translated" dense
                        @change="$parent.$emit('update:name', props.row)" />
                </q-td>
            ''')
            self.table.add_slot('body-cell-name_status', '''
                <q-td :props="props">
                    <q-chip :color="props.row.name_status === '完成' ? 'green' : (props.row.name_status === '处理中' ? 'orange' : 'grey')"
                        text-color="white" dense size="sm">
                        {{ props.row.name_status }}
                    </q-chip>
                </q-td>
            ''')
            self.table.add_slot('body-cell-analysis_status', '''
                <q-td :props="props">
                    <q-chip :color="props.row.analysis_status === '已完成' ? 'green' : (props.row.analysis_status === '处理中' ? 'orange' : 'grey')"
                        text-color="white" dense size="sm">
                        {{ props.row.analysis_status }}
                    </q-chip>
                </q-td>
            ''')
            self.table.add_slot('body-cell-action', '''
                <q-td :props="props">
                    <q-btn flat dense color="primary" label="翻译+分析"
                        @click="$parent.$emit('translate_analyze', props.row)" />
                    <q-btn flat dense color="secondary" label="查看"
                        :disable="props.row.analysis_status !== '已完成'"
                        @click="$parent.$emit('view_profile', props.row)" />
                </q-td>
            ''')

            self.table.on('update:name', self._on_name_update)
            self.table.on('translate_analyze', self._on_translate_analyze)
            self.table.on('view_profile', self._on_view_profile)

            self.table.build_ui(container, show_search=False, show_filter=False)

            self.progress = ProgressPanel()
            self.progress.build_ui(container)

            self.log_panel = LogPanel(height='h-32')
            self.log_panel.build_ui(container, label='翻译日志')
            self.logger.bind_ui('names', self.log_panel.get_push_callback())

    def refresh(self):
        if not self.db:
            return
        self.table.set_query(self._query_names)
        self.table.refresh()
        self._update_stats()

    async def async_refresh(self):
        if not self.db:
            return
        loop = asyncio.get_event_loop()
        counts = await loop.run_in_executor(None, self.db.get_char_dict_count)
        profiles = await loop.run_in_executor(None, self.db.get_all_profiles)
        self.table.set_query(self._query_names)
        _safe(self.table.refresh)
        analyzed = len(profiles)
        _safe(setattr, self.stats_label, 'text', f'📊 {counts["total"]} 人名，翻译 {counts["translated"]}，分析 {analyzed}')

    def _update_stats(self):
        if not self.db:
            return
        counts = self.db.get_char_dict_count()
        profiles = self.db.get_all_profiles()
        analyzed = len(profiles)
        _safe(setattr, self.stats_label, 'text', f'📊 {counts["total"]} 人名，翻译 {counts["translated"]}，分析 {analyzed}')

    def _query_names(self, page: int, page_size: int, **filters):
        characters = self.db.get_characters()

        # 过滤掉占位符（单独显示或跳过）
        chars = [c for c in characters if not c['is_placeholder']]

        total = len(chars)
        start = page * page_size
        end = min(start + page_size, total)
        page_items = chars[start:end]

        rows = []
        for i, c in enumerate(page_items):
            name = c['display_name']
            cn = c['cn_name']
            name_ok = bool(cn and cn.strip())
            analysis_ok = bool(c['profile_json'])
            is_processing = name in self._processing_names

            if is_processing:
                name_status = '处理中'
            elif name_ok:
                name_status = '完成'
            else:
                name_status = '待翻译'

            if is_processing:
                analysis_status = '处理中'
            elif analysis_ok:
                analysis_status = '已完成'
            else:
                analysis_status = '未分析'

            rows.append({
                'index': start + i + 1,
                'variable': c['variable'] or '',
                'original': name,
                'translated': cn or '',
                'lines': c['lines_count'],
                'name_status': name_status,
                'analysis_status': analysis_status,
                'action': name,
            })

        return rows, total

    def _on_name_update(self, e):
        row = e.args
        if row and self.db:
            self.db.update_character_cn_name(row['original'], row['translated'])
            self.logger.info(f"人名已保存: {row['original']} -> {row['translated']}", panel='names')

    async def _on_translate_analyze(self, e):
        row = e.args
        if not self.translation_service:
            _safe(ui.notify, '请先配置翻译器', type='warning')
            return
        if row:
            await self._do_translate_and_analyze(row['original'])

    async def _on_view_profile(self, e):
        row = e.args
        if not row or not self.db:
            return
        loop = asyncio.get_event_loop()
        profile = await loop.run_in_executor(None, self.db.get_profile, row['original'])
        if not profile:
            _safe(ui.notify, '该角色尚未分析', type='warning')
            return

        with ui.dialog() as dialog, ui.card().classes('w-full max-w-2xl'):
            ui.label(f'👤 {row["original"]} - 人物特征').classes('text-h6')
            ui.separator()
            for key, value in profile.items():
                if value:
                    ui.label(f'【{key}】').classes('text-subtitle2 text-primary')
                    ui.label(value).classes('text-body2 pl-4 mb-2')
            ui.button('关闭', on_click=dialog.close).classes('mt-4')
        dialog.props('persistent')
        dialog.open()

    async def _do_translate_and_analyze(self, en_name: str):
        """融合流程：一次 AI 调用同时完成人名翻译和人物分析

        台词超长时自动分段处理：
        - 第1段：翻译人名 + 分析角色（含人名翻译结果）
        - 后续段：补充分析（合并到已有 profile）
        - 最终合并所有分析结果
        """
        loop = asyncio.get_event_loop()

        # 占位符（如 [mc_name]、[hero]）不需要翻译人名，但仍需分析角色
        is_placeholder = en_name.startswith('[') and en_name.endswith(']')
        if is_placeholder:
            self.logger.info(f'占位符 {en_name}，跳过人名翻译，仅分析角色', panel='names')
            # 占位符直接填入原值作为"翻译"
            self.db.update_character_cn_name(en_name, en_name)

        # 标记为处理中
        self._processing_names.add(en_name)
        await self.async_refresh()

        try:
            # 加载该角色的台词
            def _load_lines():
                variable_map = self.db.get_variable_map()
                var_name = None
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
                await loop.run_in_executor(None, self.db.save_profile, en_name, empty_profile)
                self._processing_names.discard(en_name)
                await self.async_refresh()
                return

            # 获取人名词典用于参考
            glossary_text = self.db.get_glossary_for_prompt()
            char_prompt = self.db.get_characters_for_prompt()
            dict_text = ""
            if char_prompt:
                dict_text += char_prompt + "\n"
            if glossary_text:
                dict_text += glossary_text

            # 根据模型上下文动态计算每段台词数
            batch_size = self._calc_batch_size(len(char_lines))
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
                        f'（{len(char_lines)}条台词，每段{batch_size}条，上下文{self._max_context_k}K）',
                        panel='names'
                    )

                    if is_placeholder:
                        # 占位符：只分析角色，不翻译人名
                        prompt = f"""【任务类型：文本分析，不是翻译】

分析游戏角色 "{en_name}" 的台词，总结人物特征。

重要规则：
- 方括号包裹的内容（如 [cleo_name]、[mc_name]）是 Ren'Py 变量占位符，绝对不要翻译，直接保留原样
- 花括号包裹的内容（如 {{i}}、{{/i}}、{{size=-5}}）是 Ren'Py 标记标签，不是文本内容，忽略它们
- $ 开头的内容是代码变量，不要翻译

以下是该角色的台词（第{batch_idx+1}批，共{total_batches}批）：
{lines_text}

请按以下格式输出分析结果：

性格特点：
外貌特征：
说话风格：
行为习惯：
人物关系：
背景故事：
角色定位：
翻译建议：<仅针对该角色 "{en_name}" 的台词翻译给出建议，如语气、用词风格、特殊表达的处理方式，不要给其他角色的翻译建议>"""
                    else:
                        # 普通人名：翻译人名 + 分析角色
                        prompt = f"""你是一位资深的游戏本地化专家。请同时完成以下两个任务：

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
外貌特征：<分析>
说话风格：<分析>
行为习惯：<分析>
人物关系：<分析>
背景故事：<分析>
角色定位：<分析>
翻译建议：<仅针对该角色 "{en_name}" 的台词翻译给出建议，如语气、用词风格、特殊表达的处理方式，不要给其他角色的翻译建议>"""
                else:
                    # 后续段：补充分析
                    self.logger.info(f'[{batch_idx+1}/{total_batches}] 补充分析 {en_name}', panel='names')

                    prompt = f"""【任务类型：文本分析，不是翻译】

继续分析游戏角色 "{en_name}" 的更多台词（第{batch_idx+1}批，共{total_batches}批）。

之前已有的分析摘要：
{chr(10).join(summaries[-2:]) if summaries else "无"}

新的台词：
{lines_text}

请简要总结这批台词中展现的新特征（性格、说话风格、关系变化等），补充到已有分析中。不要重复已有内容。"""

                result = await loop.run_in_executor(
                    self._executor,
                    lambda p=prompt: self.translator.analyze_text(prompt=p)
                )

                if batch_idx == 0:
                    cn_name = self._extract_name(result)
                summaries.append(result)

            # 保存人名翻译（占位符不翻译）
            if cn_name and not is_placeholder:
                self.db.update_character_cn_name(en_name, cn_name)
                self.logger.info(f'人名: {en_name} -> {cn_name}', panel='names')

            # 合并所有分析结果
            if summaries:
                if len(summaries) == 1:
                    profile = self._parse_profile(summaries[0])
                else:
                    profile = await self._merge_summaries(en_name, summaries)

                if profile:
                    self.db.save_profile(en_name, profile)
                    self.logger.info(f'{en_name} 分析完成', panel='names')

            self._processing_names.discard(en_name)
            await self.async_refresh()

        except Exception as e:
            self.logger.error(f'{en_name} 翻译+分析失败: {e}', panel='names')
            self._processing_names.discard(en_name)
            await self.async_refresh()

    async def _merge_summaries(self, name: str, summaries: list[str]) -> dict:
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
        return self._parse_profile(result)

    def _extract_name(self, text: str) -> str:
        """从 AI 返回中提取人名翻译"""
        import re
        match = re.search(r'【人名翻译】\s*\n\s*中文名[：:]\s*(.+)', text)
        if match:
            name = match.group(1).strip()
            name = name.replace('"', '').replace("'", '').replace('。', '')
            return name
        return ''

    def _parse_profile(self, text: str) -> dict:
        profile = {}
        current_key = None
        current_value = []

        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue
            found_key = False
            for key in ['性格特点', '外貌特征', '说话风格', '行为习惯',
                        '人物关系', '背景故事', '角色定位', '翻译建议']:
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

    async def _translate_all(self):
        """翻译全部人名 + 分析全部角色（顺序处理，保证上下文连贯）"""
        if not self.translation_service:
            _safe(ui.notify, '请先配置翻译器', type='warning')
            return

        self._cancel = False
        _safe(self.translate_all_btn.set_visibility, False)
        _safe(self.stop_btn.set_visibility, True)
        if self._on_task_state_change:
            self._on_task_state_change(True)

        loop = asyncio.get_event_loop()
        completed_count = 0

        # 获取未翻译的人名
        def _get_todo():
            chars = self.db.get_untranslated_characters()
            profiles = self.db.get_all_profiles()
            return chars, profiles

        chars_todo, profiles = await loop.run_in_executor(None, _get_todo)

        total = len(chars_todo)

        try:
            if total == 0:
                # 没有未翻译的人名，检查是否需要重新分析
                all_chars = self.db.get_characters()
                unanalyzed = [c['display_name'] for c in all_chars
                              if c['display_name'] not in profiles and not c['is_placeholder']]
                if unanalyzed:
                    total = len(unanalyzed)
                    self.logger.info(f'人名已全部翻译，补充分析 {total} 个角色', panel='names')

                    for i, name in enumerate(unanalyzed):
                        if self._cancel:
                            break
                        self.progress.update(i, total, f'分析中: {i+1}/{total} {name}')
                        try:
                            await self._do_translate_and_analyze(name)
                            completed_count += 1
                        except Exception as e:
                            self.logger.error(f'{name} 分析失败: {e}', panel='names')
                else:
                    _safe(ui.notify, '所有人名已翻译并分析', type='info')
            else:
                # 融合流程：顺序处理，同时翻译人名+分析角色
                self.logger.info(f'开始翻译+分析 {total} 个角色', panel='names')

                for i, c in enumerate(chars_todo):
                    if self._cancel:
                        break
                    name = c['display_name']
                    self.progress.update(i, total, f'翻译+分析: {i+1}/{total} {name}')
                    try:
                        await self._do_translate_and_analyze(name)
                        completed_count += 1
                    except Exception as e:
                        self.logger.error(f'{name} 翻译+分析失败: {e}', panel='names')

                    try:
                        await self.async_refresh()
                    except Exception as e:
                        self.logger.warning(f'刷新表格失败: {e}')

                self.logger.info(f'翻译+分析完成: {completed_count}/{total}', panel='names')

        except Exception as e:
            self.logger.error(f'批量翻译异常中断: {e}', panel='names')

        finally:
            _safe(self.translate_all_btn.set_visibility, True)
            _safe(self.stop_btn.set_visibility, False)
            self.progress.reset()
            if self._on_task_state_change:
                self._on_task_state_change(False)
            try:
                await self.async_refresh()
            except Exception:
                pass

            if self._cancel:
                _safe(ui.notify, '已停止', type='warning')
            else:
                _safe(ui.notify, '全部完成', type='positive')

    async def _stop(self):
        self._cancel = True
        if self.translation_service:
            await self.translation_service.stop()
