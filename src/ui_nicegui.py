"""Ren'Py翻译工具 - NiceGUI前端"""

import os
import sys
import asyncio
from nicegui import ui

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from project_manager import ProjectManager, Project
from config_manager import ConfigManager, ModelConfig
from translator import AITranslator, TranslationConfig
from renpy_parser import RenpyParser


class TranslatorUI:
    """翻译工具UI"""

    def __init__(self):
        # 后端管理器
        self.project_manager = ProjectManager()
        self.config_manager = ConfigManager()
        self.parser = RenpyParser()

        # 当前状态
        self.current_project: Project = None
        self.translator: AITranslator = None
        self.current_index = 0
        self.filter_mode = "all"
        self.search_text = ""

        # 翻译队列
        self.translate_queue = {}  # {table_type: {index: status}}
        self.max_concurrent = 5  # 最大并发数
        self.current_translating = 0  # 当前正在翻译的数量

        # UI组件引用
        self.project_list = None
        self.dialogue_preview = None
        self.translation_input = None
        self.status_label = None
        self.progress_label = None

    def create(self):
        """创建UI"""
        # 页面标题
        ui.markdown('# 🎮 Ren\'Py翻译工具')

        # 顶部状态栏
        with ui.header().classes('bg-primary text-white'):
            with ui.row().classes('w-full items-center'):
                ui.label('🎮 Ren\'Py翻译工具').classes('text-h5')
                ui.space()
                self.status_label = ui.label('未打开项目').classes('text-subtitle1')

        # 主布局：左侧边栏 + 右侧内容
        with ui.row().classes('w-full'):
            # 左侧边栏
            with ui.column().classes('w-64 gap-2'):
                self._create_sidebar()

            # 右侧内容区
            with ui.column().classes('flex-1 gap-2'):
                self._create_main_content()

    def _create_sidebar(self):
        """创建左侧边栏"""
        with ui.card().classes('w-full'):
            ui.label('📁 项目管理').classes('text-h6')

            # 项目列表
            self.project_list = ui.list().classes('w-full')

            # 刷新项目列表
            self._refresh_project_list()

            ui.separator()

            # 新建项目
            ui.label('新建项目').classes('text-subtitle2')
            new_name = ui.input(label='项目名称', placeholder='MyGame')
            new_game_dir = ui.input(label='游戏目录', placeholder='Z:\\game\\MyGame')

            # 模型选择
            model_names = [c.name for c in self.config_manager.load_all_configs()]
            new_model = ui.select(options=model_names, label='AI模型', value=model_names[0] if model_names else None)

            ui.button('➕ 创建项目', on_click=lambda: self._create_project(
                new_name.value, new_game_dir.value, new_model.value
            )).classes('w-full')

    def _create_main_content(self):
        """创建主内容区"""
        # 标签页
        with ui.tabs() as tabs:
            tab_name = ui.tab('人名翻译', icon='person')
            tab_ui = ui.tab('UI翻译', icon='web')
            tab_dialogue = ui.tab('对话翻译', icon='chat')
            tab_config = ui.tab('模型配置', icon='settings')

        with ui.tab_panels(tabs, value='对话翻译').classes('w-full'):
            # 人名翻译
            with ui.tab_panel('人名翻译'):
                self._create_name_panel()

            # UI翻译
            with ui.tab_panel('UI翻译'):
                self._create_ui_panel()

            # 对话翻译
            with ui.tab_panel('对话翻译'):
                self._create_dialogue_panel()

            # 模型配置
            with ui.tab_panel('模型配置'):
                self._create_config_panel()

    def _create_name_panel(self):
        """创建人名翻译面板"""
        # 统计和分页
        with ui.row().classes('w-full items-center gap-2'):
            self.name_stats = ui.label('请先打开项目').classes('text-subtitle1')
            ui.space()
            self.name_page_size = ui.number(label='每页', value=50, min=10).classes('w-24')
            ui.button('⏮', on_click=lambda: self._name_goto_page(0)).props('flat dense')
            ui.button('◀', on_click=lambda: self._name_prev_page()).props('flat dense')
            self.name_page_label = ui.label('第 1 页')
            ui.button('▶', on_click=lambda: self._name_next_page()).props('flat dense')
            ui.button('⏭', on_click=lambda: self._name_goto_page(-1)).props('flat dense')
            ui.button('🔄 刷新', on_click=self._name_refresh)

        # 人名表格
        self.name_table = ui.table(
            columns=[
                {'name': 'index', 'label': '#', 'field': 'index', 'sortable': True},
                {'name': 'original', 'label': '原文人名', 'field': 'original'},
                {'name': 'translated', 'label': '中文名', 'field': 'translated'},
                {'name': 'status', 'label': '状态', 'field': 'status'},
                {'name': 'action', 'label': '操作', 'field': 'action'},
            ],
            rows=[],
            row_key='index'
        ).classes('w-full')

        # 自定义单元格
        self.name_table.add_slot('body-cell-translated', '''
            <q-td :props="props">
                <q-input v-model="props.row.translated" dense @change="$parent.$emit('update:name', props.row)" />
            </q-td>
        ''')
        self.name_table.add_slot('body-cell-status', '''
            <q-td :props="props">
                <q-chip :color="props.row.status === '翻译中' ? 'orange' : (props.row.status === '完成' ? 'green' : 'grey')"
                    text-color="white" dense size="sm">
                    {{ props.row.status }}
                </q-chip>
            </q-td>
        ''')
        self.name_table.add_slot('body-cell-action', '''
            <q-td :props="props">
                <q-btn flat dense color="primary" label="AI翻译"
                    :disable="props.row.status === '翻译中' || props.row.status === '排队中'"
                    @click="$parent.$emit('translate_name', props.row)" />
            </q-td>
        ''')

        # 监听事件
        self.name_table.on('update:name', self._on_name_update)
        self.name_table.on('translate_name', self._on_name_translate)

        # 分页状态
        self.name_current_page = 0

    def _create_ui_panel(self):
        """创建UI翻译面板"""
        # 统计和分页
        with ui.row().classes('w-full items-center gap-2'):
            self.ui_stats = ui.label('请先打开项目').classes('text-subtitle1')
            ui.space()
            self.ui_page_size = ui.number(label='每页', value=50, min=10).classes('w-24')
            ui.button('⏮', on_click=lambda: self._ui_goto_page(0)).props('flat dense')
            ui.button('◀', on_click=lambda: self._ui_prev_page()).props('flat dense')
            self.ui_page_label = ui.label('第 1 页')
            ui.button('▶', on_click=lambda: self._ui_next_page()).props('flat dense')
            ui.button('⏭', on_click=lambda: self._ui_goto_page(-1)).props('flat dense')
            ui.button('🚀 翻译本页', color='primary', on_click=self._ui_translate_page)
            ui.button('🔄 刷新', on_click=self._ui_refresh)

        # UI表格
        self.ui_table = ui.table(
            columns=[
                {'name': 'index', 'label': '#', 'field': 'index', 'sortable': True},
                {'name': 'original', 'label': '原文', 'field': 'original'},
                {'name': 'translated', 'label': '译文', 'field': 'translated'},
                {'name': 'status', 'label': '状态', 'field': 'status'},
                {'name': 'action', 'label': '操作', 'field': 'action'},
            ],
            rows=[],
            row_key='index'
        ).classes('w-full')

        # 自定义单元格
        self.ui_table.add_slot('body-cell-translated', '''
            <q-td :props="props">
                <q-input v-model="props.row.translated" dense @change="$parent.$emit('update:ui', props.row)" />
            </q-td>
        ''')
        self.ui_table.add_slot('body-cell-status', '''
            <q-td :props="props">
                <q-chip :color="props.row.status === '翻译中' ? 'orange' : (props.row.status === '完成' ? 'green' : 'grey')"
                    text-color="white" dense size="sm">
                    {{ props.row.status }}
                </q-chip>
            </q-td>
        ''')
        self.ui_table.add_slot('body-cell-action', '''
            <q-td :props="props">
                <q-btn flat dense color="primary" label="AI翻译"
                    :disable="props.row.status === '翻译中' || props.row.status === '排队中'"
                    @click="$parent.$emit('translate_ui', props.row)" />
            </q-td>
        ''')

        # 监听事件
        self.ui_table.on('update:ui', self._on_ui_update)
        self.ui_table.on('translate_ui', self._on_ui_translate)

        # 日志
        self.ui_log = ui.log().classes('w-full h-32')
        self.ui_current_page = 0

    def _create_dialogue_panel(self):
        """创建对话翻译面板"""
        # 统计和分页
        with ui.row().classes('w-full items-center gap-2'):
            self.dialogue_stats = ui.label('请先打开项目').classes('text-subtitle1')
            ui.space()
            self.dialogue_page_size = ui.number(label='每页', value=30, min=10).classes('w-24')
            ui.button('⏮', on_click=lambda: self._dialogue_goto_page(0)).props('flat dense')
            ui.button('◀', on_click=lambda: self._dialogue_prev_page()).props('flat dense')
            self.dialogue_page_label = ui.label('第 1 页')
            ui.button('▶', on_click=lambda: self._dialogue_next_page()).props('flat dense')
            ui.button('⏭', on_click=lambda: self._dialogue_goto_page(-1)).props('flat dense')
            ui.button('🚀 翻译本页', color='primary', on_click=self._dialogue_translate_page)
            ui.button('🔄 刷新', on_click=self._dialogue_refresh)

        # 筛选
        with ui.row().classes('w-full gap-2'):
            self.dialogue_search = ui.input(label='搜索', placeholder='搜索原文/译文').classes('flex-1')
            self.dialogue_filter = ui.select(
                options={'all': '全部', 'untranslated': '未翻译', 'translated': '已翻译'},
                label='筛选', value='all'
            )
            self.dialogue_char_filter = ui.input(label='角色', placeholder='角色名')
            ui.button('🔍', on_click=self._dialogue_apply_filter)

        # 队列状态
        with ui.row().classes('w-full items-center gap-2'):
            self.queue_status = ui.label('队列状态: 空闲').classes('text-caption')
            self.queue_progress = ui.linear_progress(value=0, show_value=False).classes('flex-1')

        # 对话表格
        self.dialogue_table = ui.table(
            columns=[
                {'name': 'index', 'label': '#', 'field': 'index', 'sortable': True},
                {'name': 'character', 'label': '角色', 'field': 'character', 'sortable': True},
                {'name': 'original', 'label': '原文', 'field': 'original'},
                {'name': 'translated', 'label': '译文', 'field': 'translated'},
                {'name': 'status', 'label': '状态', 'field': 'status'},
                {'name': 'action', 'label': '操作', 'field': 'action'},
            ],
            rows=[],
            row_key='index'
        ).classes('w-full')

        # 自定义单元格
        self.dialogue_table.add_slot('body-cell-translated', '''
            <q-td :props="props">
                <q-input v-model="props.row.translated" dense type="textarea" autogrow
                    @change="$parent.$emit('update:dialogue', props.row)" />
            </q-td>
        ''')
        self.dialogue_table.add_slot('body-cell-status', '''
            <q-td :props="props">
                <q-chip :color="props.row.status === '翻译中' ? 'orange' : (props.row.status === '完成' ? 'green' : 'grey')"
                    text-color="white" dense size="sm">
                    {{ props.row.status }}
                </q-chip>
            </q-td>
        ''')
        self.dialogue_table.add_slot('body-cell-action', '''
            <q-td :props="props">
                <q-btn flat dense color="primary" label="AI翻译"
                    :disable="props.row.status === '翻译中' || props.row.status === '排队中'"
                    @click="$parent.$emit('translate_dialogue', props.row)" />
            </q-td>
        ''')

        # 监听事件
        self.dialogue_table.on('update:dialogue', self._on_dialogue_update)
        self.dialogue_table.on('translate_dialogue', self._on_dialogue_translate)

        # 日志
        self.dialogue_log = ui.log().classes('w-full h-32')
        self.dialogue_current_page = 0
        self.dialogue_filter_mode = 'all'
        self.dialogue_search_text = ''
        self.dialogue_char_text = ''

    def _create_config_panel(self):
        """创建模型配置面板"""
        # 配置表单
        with ui.card().classes('w-full'):
            ui.label('编辑配置').classes('text-h6')
            self.config_name_input = ui.input(label='配置名称', placeholder='GPT-4')
            self.config_api_base = ui.input(label='API地址', value='https://api.openai.com/v1')
            self.config_api_key = ui.input(label='API Key', password=True)
            self.config_model = ui.input(label='模型名称', placeholder='gpt-4')

            with ui.row().classes('gap-2 items-center w-full'):
                ui.label('Temperature:')
                self.config_temp = ui.slider(min=0, max=2, value=0.3, step=0.1).classes('flex-1')
                self.temp_label = ui.label('0.3')

            self.config_temp.on_value_change(lambda e: self.temp_label.set_text(str(e.value)))

            with ui.row().classes('gap-2'):
                self.config_max_tokens = ui.number(label='最大输出Token数', value=4096, min=1)
                self.config_context = ui.number(label='上下文行数', value=3, min=0)

            with ui.row().classes('gap-2'):
                ui.button('💾 保存配置', color='primary', on_click=self._save_config_form)
                ui.button('🔄 清空表单', on_click=self._clear_config_form)

        ui.separator()

        # 配置列表
        ui.label('已有配置').classes('text-subtitle1')
        self.config_list = ui.list().classes('w-full')
        self._refresh_config_list()

    # ========== 项目操作 ==========

    def _refresh_project_list(self):
        """刷新项目列表"""
        self.project_list.clear()
        projects = self.project_manager.list_projects()

        with self.project_list:
            for p in projects:
                with ui.item(on_click=lambda name=p.name: self._open_project(name)):
                    with ui.item_section():
                        ui.item_label(p.name)
                        ui.item_label(p.progress_text).props('caption')

    def _create_project(self, name, game_dir, model):
        """创建项目"""
        if not name or not game_dir:
            ui.notify('请填写完整信息', type='warning')
            return

        try:
            # 创建项目
            project = self.project_manager.create_project(name, game_dir, model or '')

            # 解析游戏
            ui.notify('正在解析游戏...', type='info')
            result = self.parser.parse_directory(game_dir, extract_rpa=True, decompile_rpyc=True)

            # 保存解析结果
            project.dialogues = [self._dialogue_to_dict(d) for d in result['dialogues']]
            project.ui_texts = [self._dialogue_to_dict(d) for d in result['ui_texts']]
            project.characters = [self._character_to_dict(c) for c in result['characters']]

            # 初始化人名词典
            for char in result['characters']:
                if char.name not in project.char_dict:
                    project.char_dict[char.name] = char.name

            self.project_manager.save_project(project)

            ui.notify(f'项目创建成功！共 {len(project.dialogues)} 条对话', type='positive')
            self._refresh_project_list()

        except Exception as e:
            ui.notify(f'创建失败: {str(e)}', type='negative')

    def _open_project(self, name):
        """打开项目"""
        project = self.project_manager.load_project(name)
        if not project:
            ui.notify('项目不存在', type='negative')
            return

        self.current_project = project
        self.name_current_page = 0
        self.ui_current_page = 0
        self.dialogue_current_page = 0

        # 初始化翻译器
        if project.model_config_name:
            self._init_translator(project.model_config_name)

        # 更新所有面板
        self._name_refresh()
        self._ui_refresh()
        self._dialogue_refresh()
        self.status_label.text = f'当前项目: {name}'

        ui.notify(f'已打开项目: {name}', type='positive')

    def _save_project(self):
        """保存项目"""
        if not self.current_project:
            ui.notify('未打开项目', type='warning')
            return

        # 更新位置
        filtered = self._get_filtered_dialogues()
        if filtered and self.current_index < len(filtered):
            dialogue = filtered[self.current_index]
            self.current_project.last_position = {
                'index': self.current_index,
                'file': dialogue.get('file_path', ''),
                'line': dialogue.get('line_number', 0)
            }

        if self.project_manager.save_project(self.current_project):
            ui.notify('项目已保存', type='positive')
        else:
            ui.notify('保存失败', type='negative')

    # ========== 翻译操作 ==========

    def _get_filtered_dialogues(self):
        """获取筛选后的对话"""
        if not self.current_project:
            return []

        dialogues = self.current_project.dialogues

        if self.filter_mode == 'untranslated':
            dialogues = [d for d in dialogues if not d.get('is_translated', False)]
        elif self.filter_mode == 'translated':
            dialogues = [d for d in dialogues if d.get('is_translated', False)]

        if self.search_text:
            search_lower = self.search_text.lower()
            dialogues = [d for d in dialogues
                        if search_lower in d.get('original_text', '').lower()
                        or search_lower in d.get('translated_text', '').lower()]

        return dialogues

    def _update_progress(self):
        """更新进度显示"""
        if not self.current_project:
            self.progress_label.text = '请先打开项目'
            return

        stats = self.current_project.get_stats()
        filtered = self._get_filtered_dialogues()
        self.progress_label.text = (
            f"📊 总进度: {stats['translated_dialogues']}/{stats['total_dialogues']} 对话 | "
            f"当前筛选: {len(filtered)} 条 | 位置: {self.current_index + 1}/{len(filtered)}"
        )

    def _update_preview(self):
        """更新对话预览"""
        filtered = self._get_filtered_dialogues()
        if not filtered or self.current_index >= len(filtered):
            self.dialogue_preview.content = '暂无对话'
            return

        dialogue = filtered[self.current_index]

        # 构建预览文本
        preview = f"""
**📍 位置:** {dialogue.get('file_path', '')}:{dialogue.get('line_number', 0)}

**👤 角色:** {dialogue.get('character', '') or '旁白'}

**📝 原文:**
{dialogue.get('original_text', '')}

**🔄 译文:**
{dialogue.get('translated_text', '') or '*未翻译*'}
"""

        # 上下文
        ctx_before = dialogue.get('context_before', [])
        ctx_after = dialogue.get('context_after', [])

        if ctx_before:
            preview += '\n\n**--- 前文 ---**\n'
            for line in ctx_before[-2:]:
                preview += f'{line}\n'

        if ctx_after:
            preview += '\n**--- 后文 ---**\n'
            for line in ctx_after[:2]:
                preview += f'{line}\n'

        self.dialogue_preview.content = preview

    def _apply_filter(self, search, mode, char):
        """应用筛选"""
        self.search_text = search
        self.filter_mode = mode
        self.current_index = 0
        self._update_progress()
        self._update_preview()

    def _navigate(self, direction):
        """导航对话"""
        filtered = self._get_filtered_dialogues()
        if not filtered:
            return

        if direction == 'first':
            self.current_index = 0
        elif direction == 'prev':
            self.current_index = max(0, self.current_index - 1)
        elif direction == 'next':
            self.current_index = min(len(filtered) - 1, self.current_index + 1)
        elif direction == 'last':
            self.current_index = len(filtered) - 1

        self._update_progress()
        self._update_preview()

    def _ai_translate(self):
        """AI翻译"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return

        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        filtered = self._get_filtered_dialogues()
        if not filtered or self.current_index >= len(filtered):
            return

        dialogue = filtered[self.current_index]

        try:
            translated = self.translator.translate_text(
                text=dialogue['original_text'],
                character=dialogue.get('character', ''),
                context_before=dialogue.get('context_before', []),
                context_after=dialogue.get('context_after', []),
                character_dict=self.current_project.char_dict
            )

            dialogue['translated_text'] = translated
            dialogue['is_translated'] = True

            self._update_progress()
            self._update_preview()
            self._save_project()

            ui.notify('翻译完成', type='positive')

        except Exception as e:
            ui.notify(f'翻译失败: {str(e)}', type='negative')

    def _manual_translate(self):
        """手动翻译"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return

        text = self.translation_input.value
        if not text:
            ui.notify('请输入翻译内容', type='warning')
            return

        filtered = self._get_filtered_dialogues()
        if not filtered or self.current_index >= len(filtered):
            return

        dialogue = filtered[self.current_index]
        dialogue['translated_text'] = text
        dialogue['is_translated'] = True

        self.translation_input.value = ''
        self._update_progress()
        self._update_preview()
        self._save_project()

        ui.notify('翻译已保存', type='positive')

    # ========== 人名翻译 ==========

    def _on_name_update(self, e):
        """人名输入框更新 - 自动保存"""
        row = e.args
        if row and self.current_project:
            self.current_project.char_dict[row['original']] = row['translated']
            self._save_project()

    def _on_name_translate(self, e):
        """AI翻译人名 - 加入队列"""
        row = e.args
        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return
        if row and self.current_project:
            # 生成唯一key
            key = f"name_{row['original']}"
            if key in self.translate_queue:
                ui.notify('该条目已在队列中', type='warning')
                return

            # 加入队列
            self.translate_queue[key] = {
                'type': 'name',
                'row': row,
                'original': row['original'],
                'status': '排队中'
            }
            self._update_queue_status()
            self._process_next_in_queue()

    def _do_name_translate(self, queue_item):
        """执行人名翻译"""
        row = queue_item['row']
        try:
            prompt = f"将以下人名翻译成中文，只返回中文名，不要解释：{row['original']}"
            translated = self.translator.translate_text(text=prompt)
            translated = translated.strip().replace('"', '').replace("'", '')
            self.current_project.char_dict[row['original']] = translated
            self._save_project()
            self._name_refresh()
            return True
        except Exception as e:
            ui.notify(f'翻译失败: {str(e)}', type='negative')
            return False

    def _update_queue_status(self):
        """更新队列状态显示"""
        if hasattr(self, 'queue_status'):
            total = len(self.translate_queue)
            translating = sum(1 for v in self.translate_queue.values() if v['status'] == '翻译中')
            queued = sum(1 for v in self.translate_queue.values() if v['status'] == '排队中')
            self.queue_status.text = f'队列状态: {translating}个翻译中, {queued}个排队'

    def _process_next_in_queue(self):
        """处理队列中的下一个任务"""
        # 检查是否达到并发上限
        if self.current_translating >= self.max_concurrent:
            return

        # 找到下一个排队中的任务
        next_key = None
        for key, item in self.translate_queue.items():
            if item['status'] == '排队中':
                next_key = key
                break

        if not next_key:
            return

        # 开始翻译
        self.current_translating += 1
        self.translate_queue[next_key]['status'] = '翻译中'
        self._update_queue_status()

        # 根据类型执行翻译
        queue_item = self.translate_queue[next_key]
        try:
            if queue_item['type'] == 'name':
                success = self._do_name_translate(queue_item)
            elif queue_item['type'] == 'ui':
                success = self._do_ui_translate(queue_item)
            elif queue_item['type'] == 'dialogue':
                success = self._do_dialogue_translate(queue_item)
            else:
                success = False
        except Exception as e:
            ui.notify(f'翻译失败: {str(e)}', type='negative')
            success = False

        # 移除已完成的任务
        del self.translate_queue[next_key]
        self.current_translating -= 1
        self._update_queue_status()

        # 处理下一个
        self._process_next_in_queue()

    def _name_refresh(self):
        """刷新人名表格"""
        if not self.current_project:
            return

        char_dict = self.current_project.char_dict
        items = list(char_dict.items())
        total = len(items)

        page_size = int(self.name_page_size.value)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        self.name_current_page = min(self.name_current_page, total_pages - 1)

        start = self.name_current_page * page_size
        end = min(start + page_size, total)
        page_items = items[start:end]

        rows = []
        for i, (en, cn) in enumerate(page_items):
            # 检查队列状态
            key = f"name_{en}"
            status = '待翻译'
            if key in self.translate_queue:
                status = self.translate_queue[key]['status']
            elif cn and cn != en:
                status = '完成'

            rows.append({
                'index': start + i + 1,
                'original': en,
                'translated': cn,
                'status': status,
                'action': en  # 存储原始key用于操作
            })

        self.name_table.rows = rows
        self.name_stats.text = f'📊 共 {total} 个人名'
        self.name_page_label.text = f'第 {self.name_current_page + 1} / {total_pages} 页'

    def _name_prev_page(self):
        if self.name_current_page > 0:
            self.name_current_page -= 1
            self._name_refresh()

    def _name_next_page(self):
        if not self.current_project:
            return
        page_size = int(self.name_page_size.value)
        total = len(self.current_project.char_dict)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        if self.name_current_page < total_pages - 1:
            self.name_current_page += 1
            self._name_refresh()

    def _name_goto_page(self, page):
        if not self.current_project:
            return
        if page == -1:
            page_size = int(self.name_page_size.value)
            total = len(self.current_project.char_dict)
            self.name_current_page = (total + page_size - 1) // page_size - 1 if total > 0 else 0
        else:
            self.name_current_page = max(0, page)
        self._name_refresh()

    # ========== UI翻译 ==========

    def _on_ui_update(self, e):
        """UI译文输入框更新 - 自动保存"""
        row = e.args
        if row and self.current_project:
            idx = row['action']
            if 0 <= idx < len(self.current_project.ui_texts):
                self.current_project.ui_texts[idx]['translated_text'] = row['translated']
                self.current_project.ui_texts[idx]['is_translated'] = True
                self._save_project()

    def _on_ui_translate(self, e):
        """AI翻译单条UI - 加入队列"""
        row = e.args
        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return
        if row and self.current_project:
            idx = row['action']
            if 0 <= idx < len(self.current_project.ui_texts):
                # 生成唯一key
                key = f"ui_{idx}"
                if key in self.translate_queue:
                    ui.notify('该条目已在队列中', type='warning')
                    return

                # 加入队列
                self.translate_queue[key] = {
                    'type': 'ui',
                    'idx': idx,
                    'original': self.current_project.ui_texts[idx]['original_text'],
                    'status': '排队中'
                }
                self._update_queue_status()
                self._process_next_in_queue()

    def _do_ui_translate(self, queue_item):
        """执行UI翻译"""
        idx = queue_item['idx']
        if 0 <= idx < len(self.current_project.ui_texts):
            item = self.current_project.ui_texts[idx]
            try:
                translated = self.translator.translate_text(text=item['original_text'])
                item['translated_text'] = translated
                item['is_translated'] = True
                self._save_project()
                self._ui_refresh()
                return True
            except Exception as e:
                ui.notify(f'翻译失败: {str(e)}', type='negative')
                return False
        return False

    def _ui_refresh(self):
        """刷新UI表格"""
        if not self.current_project:
            return

        items = self.current_project.ui_texts
        total = len(items)
        translated = sum(1 for d in items if d.get('is_translated', False))

        page_size = int(self.ui_page_size.value)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        self.ui_current_page = min(self.ui_current_page, total_pages - 1)

        start = self.ui_current_page * page_size
        end = min(start + page_size, total)
        page_items = items[start:end]

        rows = []
        for i, d in enumerate(page_items):
            idx = start + i
            # 检查队列状态
            key = f"ui_{idx}"
            status = '待翻译'
            if key in self.translate_queue:
                status = self.translate_queue[key]['status']
            elif d.get('is_translated', False):
                status = '完成'

            rows.append({
                'index': idx + 1,
                'original': d.get('original_text', ''),
                'translated': d.get('translated_text', ''),
                'status': status,
                'action': idx  # 存储索引用于操作
            })

        self.ui_table.rows = rows
        self.ui_stats.text = f'📊 总计: {total} | ✅ 已翻译: {translated}'
        self.ui_page_label.text = f'第 {self.ui_current_page + 1} / {total_pages} 页'

    def _ui_prev_page(self):
        if self.ui_current_page > 0:
            self.ui_current_page -= 1
            self._ui_refresh()

    def _ui_next_page(self):
        if not self.current_project:
            return
        page_size = int(self.ui_page_size.value)
        total = len(self.current_project.ui_texts)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        if self.ui_current_page < total_pages - 1:
            self.ui_current_page += 1
            self._ui_refresh()

    def _ui_goto_page(self, page):
        if not self.current_project:
            return
        if page == -1:
            page_size = int(self.ui_page_size.value)
            total = len(self.current_project.ui_texts)
            self.ui_current_page = (total + page_size - 1) // page_size - 1 if total > 0 else 0
        else:
            self.ui_current_page = max(0, page)
        self._ui_refresh()

    def _ui_translate_page(self):
        """翻译当前页UI"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return
        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        page_size = int(self.ui_page_size.value)
        start = self.ui_current_page * page_size
        end = min(start + page_size, len(self.current_project.ui_texts))
        page_items = self.current_project.ui_texts[start:end]

        to_translate = [d for d in page_items if not d.get('is_translated', False)]
        if not to_translate:
            ui.notify('当前页已全部翻译', type='info')
            return

        total = len(to_translate)
        success = 0
        stopped = False

        with self.ui_log:
            for i, item in enumerate(to_translate):
                try:
                    translated = self.translator.translate_text(text=item['original_text'])
                    item['translated_text'] = translated
                    item['is_translated'] = True
                    success += 1
                except Exception as e:
                    print(f'❌ 翻译失败: {e}')
                    ui.notify(f'API错误，翻译已停止: {str(e)}', type='negative')
                    stopped = True
                    break

        self._save_project()
        self._ui_refresh()

        if stopped:
            ui.notify(f'翻译已停止！成功: {success}/{total}', type='warning')
        else:
            ui.notify(f'本页翻译完成！成功: {success}/{total}', type='positive')

    # ========== 对话翻译 ==========

    def _on_dialogue_update(self, e):
        """对话译文输入框更新 - 自动保存"""
        row = e.args
        if row and self.current_project:
            idx = row['action']
            if 0 <= idx < len(self.current_project.dialogues):
                self.current_project.dialogues[idx]['translated_text'] = row['translated']
                self.current_project.dialogues[idx]['is_translated'] = True
                self._save_project()

    def _on_dialogue_translate(self, e):
        """AI翻译单条对话 - 加入队列"""
        row = e.args
        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return
        if row and self.current_project:
            idx = row['action']
            if 0 <= idx < len(self.current_project.dialogues):
                # 生成唯一key
                key = f"dialogue_{idx}"
                if key in self.translate_queue:
                    ui.notify('该条目已在队列中', type='warning')
                    return

                # 加入队列
                self.translate_queue[key] = {
                    'type': 'dialogue',
                    'idx': idx,
                    'original': self.current_project.dialogues[idx]['original_text'],
                    'status': '排队中'
                }
                self._update_queue_status()
                self._process_next_in_queue()

    def _do_dialogue_translate(self, queue_item):
        """执行对话翻译"""
        idx = queue_item['idx']
        if 0 <= idx < len(self.current_project.dialogues):
            item = self.current_project.dialogues[idx]
            try:
                translated = self.translator.translate_text(
                    text=item['original_text'],
                    character=item.get('character', ''),
                    context_before=item.get('context_before', []),
                    context_after=item.get('context_after', []),
                    character_dict=self.current_project.char_dict
                )
                item['translated_text'] = translated
                item['is_translated'] = True
                self._save_project()
                self._dialogue_refresh()
                return True
            except Exception as e:
                ui.notify(f'翻译失败: {str(e)}', type='negative')
                return False
        return False

    def _dialogue_refresh(self):
        """刷新对话表格"""
        if not self.current_project:
            return

        dialogues = self.current_project.dialogues

        # 应用筛选
        if self.dialogue_filter_mode == 'untranslated':
            dialogues = [d for d in dialogues if not d.get('is_translated', False)]
        elif self.dialogue_filter_mode == 'translated':
            dialogues = [d for d in dialogues if d.get('is_translated', False)]

        if self.dialogue_search_text:
            s = self.dialogue_search_text.lower()
            dialogues = [d for d in dialogues if s in d.get('original_text', '').lower() or s in d.get('translated_text', '').lower()]

        if self.dialogue_char_text:
            dialogues = [d for d in dialogues if d.get('character', '') == self.dialogue_char_text]

        total = len(dialogues)
        translated = sum(1 for d in dialogues if d.get('is_translated', False))

        page_size = int(self.dialogue_page_size.value)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        self.dialogue_current_page = min(self.dialogue_current_page, total_pages - 1)

        start = self.dialogue_current_page * page_size
        end = min(start + page_size, total)
        page_items = dialogues[start:end]

        rows = []
        for i, d in enumerate(page_items):
            idx = start + i
            # 检查队列状态
            key = f"dialogue_{idx}"
            status = '待翻译'
            if key in self.translate_queue:
                status = self.translate_queue[key]['status']
            elif d.get('is_translated', False):
                status = '完成'

            rows.append({
                'index': idx + 1,
                'character': d.get('character', '') or '旁白',
                'original': d.get('original_text', ''),
                'translated': d.get('translated_text', ''),
                'status': status,
                'action': idx
            })

        self.dialogue_table.rows = rows
        self.dialogue_stats.text = f'📊 总计: {total} | ✅ 已翻译: {translated}'
        self.dialogue_page_label.text = f'第 {self.dialogue_current_page + 1} / {total_pages} 页'

    def _dialogue_apply_filter(self):
        self.dialogue_search_text = self.dialogue_search.value
        self.dialogue_filter_mode = self.dialogue_filter.value
        self.dialogue_char_text = self.dialogue_char_filter.value
        self.dialogue_current_page = 0
        self._dialogue_refresh()

    def _dialogue_prev_page(self):
        if self.dialogue_current_page > 0:
            self.dialogue_current_page -= 1
            self._dialogue_refresh()

    def _dialogue_next_page(self):
        if not self.current_project:
            return
        page_size = int(self.dialogue_page_size.value)
        total = len(self.current_project.dialogues)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        if self.dialogue_current_page < total_pages - 1:
            self.dialogue_current_page += 1
            self._dialogue_refresh()

    def _dialogue_goto_page(self, page):
        if not self.current_project:
            return
        if page == -1:
            page_size = int(self.dialogue_page_size.value)
            total = len(self.current_project.dialogues)
            self.dialogue_current_page = (total + page_size - 1) // page_size - 1 if total > 0 else 0
        else:
            self.dialogue_current_page = max(0, page)
        self._dialogue_refresh()

    def _dialogue_translate_page(self):
        """翻译当前页对话"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return
        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        page_size = int(self.dialogue_page_size.value)
        start = self.dialogue_current_page * page_size
        end = min(start + page_size, len(self.current_project.dialogues))
        page_items = self.current_project.dialogues[start:end]

        to_translate = [d for d in page_items if not d.get('is_translated', False)]
        if not to_translate:
            ui.notify('当前页已全部翻译', type='info')
            return

        total = len(to_translate)
        success = 0
        stopped = False

        with self.dialogue_log:
            for i, item in enumerate(to_translate):
                try:
                    translated = self.translator.translate_text(
                        text=item['original_text'],
                        character=item.get('character', ''),
                        context_before=item.get('context_before', []),
                        context_after=item.get('context_after', []),
                        character_dict=self.current_project.char_dict
                    )
                    item['translated_text'] = translated
                    item['is_translated'] = True
                    success += 1
                    print(f'✅ [{success}/{total}] {item["original_text"][:30]}...')
                except Exception as e:
                    print(f'❌ 翻译失败: {e}')
                    ui.notify(f'API错误，翻译已停止: {str(e)}', type='negative')
                    stopped = True
                    break

        self._save_project()
        self._dialogue_refresh()

        if stopped:
            ui.notify(f'翻译已停止！成功: {success}/{total}', type='warning')
        else:
            ui.notify(f'本页翻译完成！成功: {success}/{total}', type='positive')

    # ========== 模型配置 ==========

    def _refresh_config_list(self):
        """刷新配置列表"""
        self.config_list.clear()
        configs = self.config_manager.load_all_configs()

        with self.config_list:
            for c in configs:
                with ui.item():
                    with ui.item_section():
                        ui.item_label(c.name)
                        ui.item_label(f'{c.model} | {c.api_base}').props('caption')
                    with ui.item_section().props('side'):
                        ui.button(icon='edit', on_click=lambda name=c.name: self._edit_config(name)).props('flat dense')
                        ui.button(icon='delete', color='red', on_click=lambda name=c.name: self._delete_config(name)).props('flat dense')

    def _save_config_form(self):
        """从表单保存配置"""
        name = self.config_name_input.value
        if not name:
            ui.notify('请输入配置名称', type='warning')
            return

        config = ModelConfig(
            name=name,
            api_base=self.config_api_base.value,
            api_key=self.config_api_key.value,
            model=self.config_model.value,
            temperature=float(self.config_temp.value),
            max_tokens=int(self.config_max_tokens.value),
            context_lines=int(self.config_context.value)
        )

        # 检查是否已存在
        existing = self.config_manager.get_config_by_name(name)
        if existing:
            self.config_manager.update_config(name, config)
            ui.notify(f'配置已更新: {name}', type='positive')
        else:
            self.config_manager.add_config(config)
            ui.notify(f'配置已保存: {name}', type='positive')

        self._refresh_config_list()

    def _edit_config(self, name):
        """编辑配置 - 填充表单"""
        config = self.config_manager.get_config_by_name(name)
        if not config:
            ui.notify('配置不存在', type='negative')
            return

        # 填充表单
        self.config_name_input.value = config.name
        self.config_api_base.value = config.api_base
        self.config_api_key.value = config.api_key
        self.config_model.value = config.model
        self.config_temp.value = config.temperature
        self.config_max_tokens.value = config.max_tokens
        self.config_context.value = config.context_lines

        ui.notify(f'已加载配置: {name}', type='info')

    def _delete_config(self, name):
        """删除配置"""
        # 使用对话框确认
        with ui.dialog() as dialog:
            with ui.card():
                ui.label(f'确定要删除配置 "{name}" 吗？')
                with ui.row().classes('gap-2'):
                    ui.button('取消', on_click=dialog.close)
                    ui.button('删除', color='red', on_click=lambda: (
                        self.config_manager.delete_config(name),
                        self._refresh_config_list(),
                        ui.notify(f'已删除配置: {name}', type='positive'),
                        dialog.close()
                    ))
        dialog.open()

    def _clear_config_form(self):
        """清空配置表单"""
        self.config_name_input.value = ''
        self.config_api_base.value = 'https://api.openai.com/v1'
        self.config_api_key.value = ''
        self.config_model.value = ''
        self.config_temp.value = 0.3
        self.config_max_tokens.value = 4096
        self.config_context.value = 3

    def _init_translator(self, config_name):
        """初始化翻译器"""
        config = self.config_manager.get_config_by_name(config_name)
        if not config:
            return

        trans_config = TranslationConfig(
            api_base=config.api_base,
            api_key=config.api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            context_lines=config.context_lines
        )
        self.translator = AITranslator(trans_config)

    # ========== 辅助方法 ==========

    def _dialogue_to_dict(self, d):
        """将DialogueLine转为字典"""
        return {
            'file_path': d.file_path,
            'line_number': d.line_number,
            'character': d.character,
            'original_text': d.original_text,
            'translated_text': d.translated_text,
            'is_translated': d.is_translated,
            'context_before': d.context_before or [],
            'context_after': d.context_after or []
        }

    def _character_to_dict(self, c):
        """将CharacterInfo转为字典"""
        return {
            'variable': c.variable,
            'name': c.name,
            'chinese_name': c.chinese_name
        }


def create_app():
    """创建应用"""
    app = TranslatorUI()
    app.create()
    return app
