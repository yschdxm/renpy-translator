"""人物分析面板"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from nicegui import ui

from database import ProjectDatabase
from translator import AITranslator
from logger import TranslationLogger
from components.log_panel import LogPanel


class AnalysisPanel:
    """人物分析面板"""

    def __init__(self, logger: TranslationLogger):
        self.logger = logger
        self.db: ProjectDatabase = None
        self.translator: AITranslator = None

        self.analysis_table: ui.table = None
        self.stats_label: ui.label = None
        self.analyze_all_btn: ui.button = None
        self.analyze_btn: ui.button = None
        self.log_panel: LogPanel = None
        self._executor = ThreadPoolExecutor(max_workers=2)

    def set_db(self, db: ProjectDatabase):
        self.db = db

    def set_translator(self, translator: AITranslator):
        self.translator = translator

    def create(self, container: ui.column):
        """创建面板"""
        with container:
            with ui.row().classes('w-full items-center gap-2'):
                self.stats_label = ui.label('请先完成人名翻译').classes('text-subtitle1')
                ui.space()
                self.analyze_all_btn = ui.button(
                    '🤖 分析所有角色', color='primary',
                    on_click=self._analyze_all
                )
                self.analyze_btn = ui.button('🔄 刷新', on_click=self.refresh)

            ui.separator()

            self.analysis_table = ui.table(
                columns=[
                    {'name': 'name', 'label': '角色名', 'field': 'name'},
                    {'name': 'lines_count', 'label': '台词数', 'field': 'lines_count'},
                    {'name': 'status', 'label': '状态', 'field': 'status'},
                    {'name': 'action', 'label': '操作', 'field': 'action'},
                ],
                rows=[],
                row_key='name'
            ).classes('w-full')

            self.analysis_table.add_slot('body-cell-status', '''
                <q-td :props="props">
                    <q-chip :color="props.row.status === '已完成' ? 'green' : (props.row.status === '分析中' ? 'orange' : 'grey')"
                        text-color="white" dense size="sm">
                        {{ props.row.status }}
                    </q-chip>
                </q-td>
            ''')
            self.analysis_table.add_slot('body-cell-action', '''
                <q-td :props="props">
                    <q-btn flat dense color="primary" label="分析"
                        :disable="props.row.status === '分析中'"
                        @click="$parent.$emit('analyze_character', props.row)" />
                    <q-btn flat dense color="secondary" label="查看"
                        :disable="props.row.status !== '已完成'"
                        @click="$parent.$emit('view_character', props.row)" />
                </q-td>
            ''')

            self.analysis_table.on('analyze_character', self._on_analyze)
            self.analysis_table.on('view_character', self._on_view)

            ui.separator()
            self.log_panel = LogPanel(height='h-48')
            self.log_panel.build_ui(container, label='分析日志')

            self.logger.bind_ui('analysis', self.log_panel.get_push_callback())

    def refresh(self):
        """同步刷新（仅在同步上下文中使用）"""
        if not self.db:
            return
        self._do_refresh()

    async def async_refresh(self):
        """异步刷新（非阻塞）"""
        if not self.db:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._do_refresh)

    def _do_refresh(self):
        """实际刷新逻辑（同步，在线程池中调用）"""
        characters = self.db.get_characters()
        profiles = self.db.get_all_profiles()
        variable_map = self.db.get_variable_map()
        dialogues = self.db.get_dialogues_page(0, 999999)[0]

        var_line_counts = {}
        for d in dialogues:
            var = d.get('character', '')
            if var:
                var_line_counts[var] = var_line_counts.get(var, 0) + 1

        rows = []
        for char in characters:
            name = char['name']
            var = char['variable']
            lines_count = var_line_counts.get(var, 0)
            status = '已完成' if name in profiles else '未分析'
            rows.append({
                'name': name,
                'lines_count': lines_count,
                'status': status,
                'action': name,
            })

        self.analysis_table.rows = rows
        self.analysis_table.update()
        analyzed = sum(1 for r in rows if r['status'] == '已完成')
        self.stats_label.text = f'📊 共 {len(rows)} 个角色，已分析 {analyzed} 个'

    async def _on_analyze(self, e):
        """分析单个角色"""
        row = e.args
        if not row or not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return
        await self._do_analyze(row['name'])

    async def _on_view(self, e):
        """查看角色分析结果（DB 查询在线程池中）"""
        row = e.args
        if not row or not self.db:
            return

        loop = asyncio.get_event_loop()
        profile = await loop.run_in_executor(None, self.db.get_profile, row['name'])

        if not profile:
            ui.notify('该角色尚未分析', type='warning')
            return

        with ui.dialog() as dialog, ui.card().classes('w-full max-w-2xl'):
            ui.label(f'👤 {row["name"]} - 人物特征').classes('text-h6')
            ui.separator()
            for key, value in profile.items():
                if value:
                    ui.label(f'【{key}】').classes('text-subtitle2 text-primary')
                    ui.label(value).classes('text-body2 pl-4 mb-2')
            ui.button('关闭', on_click=dialog.close).classes('mt-4')
        dialog.props('persistent')
        dialog.open()

    async def _analyze_all(self):
        """分析所有角色"""
        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        loop = asyncio.get_event_loop()

        def _load():
            return self.db.get_characters(), self.db.get_all_profiles()

        characters, profiles = await loop.run_in_executor(None, _load)

        self.analyze_all_btn.disable()
        self.analyze_all_btn.text = '分析中...'

        success = 0
        for i, char in enumerate(characters):
            name = char['name']
            if name in profiles:
                success += 1
                continue

            ok = await self._do_analyze(name)
            if ok:
                success += 1
            await self.async_refresh()

        self.analyze_all_btn.enable()
        self.analyze_all_btn.text = '🤖 分析所有角色'
        self.logger.info(f'分析完成！成功: {success}/{len(characters)}', panel='analysis')

    async def _do_analyze(self, name: str) -> bool:
        """执行角色分析"""
        loop = asyncio.get_event_loop()

        self.logger.info(f'正在分析 {name}...', panel='analysis')

        try:
            # 获取该角色的台词（DB 查询在线程池中）
            def _load_lines():
                variable_map = self.db.get_variable_map()
                var_name = None
                for var, display in variable_map.items():
                    if display == name:
                        var_name = var
                        break
                if var_name:
                    dialogues = self.db.get_dialogues_page(0, 999999,
                        character=var_name)[0]
                    return [d['original_text'] for d in dialogues]
                return []

            char_lines = await loop.run_in_executor(None, _load_lines)

            if not char_lines:
                empty_profile = {'性格特征': '该角色没有台词', '说话风格': '无', '背景': '无'}
                await loop.run_in_executor(None, self.db.save_profile, name, empty_profile)
                self.logger.info(f'{name} 没有台词，已跳过', panel='analysis')
                return True

            # 分析
            lines_text = '\n'.join([f'"{line}"' for line in char_lines[:50]])
            prompt = f"""【任务类型：文本分析，不是翻译】

我需要你分析游戏角色 "{name}" 的以下台词，总结该角色的人物特征。

台词（请勿翻译，只需分析）：
{lines_text}

请严格按照以下格式输出分析结果：

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

            # 解析结果
            profile = self._parse_profile(result)
            if profile:
                await loop.run_in_executor(None, self.db.save_profile, name, profile)
                self.logger.info(f'{name} 分析完成', panel='analysis')
                return True
            else:
                self.logger.error(f'{name} 分析结果解析失败', panel='analysis')
                return False

        except Exception as e:
            self.logger.error(f'{name} 分析失败: {e}', panel='analysis')
            return False

    def _parse_profile(self, text: str) -> dict:
        """解析人物特征文本为字典"""
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
