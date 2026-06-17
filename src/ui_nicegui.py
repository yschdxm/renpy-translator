"""Ren'Py翻译工具 - NiceGUI前端"""

import os
import sys
import asyncio
from pathlib import Path
from queue import Queue
from concurrent.futures import ThreadPoolExecutor
from nicegui import ui

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from project_manager import ProjectManager, Project
from config_manager import ConfigManager, ModelConfig
from translator import AITranslator, TranslationConfig
from renpy_parser import RenpyParser
from sdk_manager import SDKManager


class TranslatorUI:
    """翻译工具UI"""

    def __init__(self):
        # 后端管理器
        self.project_manager = ProjectManager()
        self.config_manager = ConfigManager()
        self.parser = RenpyParser()
        self.sdk_manager = SDKManager()

        # 当前状态
        self.current_project: Project = None
        self.is_translating = False  # 是否正在翻译
        self.stop_translation = False  # 是否停止翻译
        self.translator: AITranslator = None
        self.current_index = 0
        self.filter_mode = "all"
        self.search_text = ""

        # 翻译队列
        self.translate_queue = {}  # {table_type: {index: status}}
        self.max_concurrent = 5  # 最大并发数
        self.current_translating = 0  # 当前正在翻译的数量
        self.executor = ThreadPoolExecutor(max_workers=5)  # 线程池

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
            new_game_dir = ui.input(label='游戏目录', placeholder='/path/to/game')

            # 模型选择
            model_names = [c.name for c in self.config_manager.load_all_configs()]
            new_model = ui.select(options=model_names, label='AI模型', value=model_names[0] if model_names else None)

            # 创建按钮（添加引用以便禁用）
            self.create_btn = ui.button('➕ 创建项目', on_click=lambda: self._create_project(
                new_name.value, new_game_dir.value, new_model.value
            )).classes('w-full')

    def _create_main_content(self):
        """创建主内容区"""
        # 标签页
        with ui.tabs() as tabs:
            tab_name = ui.tab('人名翻译', icon='person')
            tab_analysis = ui.tab('人物分析', icon='psychology')
            tab_strings = ui.tab('字符串翻译', icon='translate')
            tab_dialogue = ui.tab('对话翻译', icon='chat')
            tab_export = ui.tab('导出游戏', icon='folder_zip')
            tab_config = ui.tab('模型配置', icon='settings')

        with ui.tab_panels(tabs, value='人名翻译').classes('w-full'):
            # 人名翻译
            with ui.tab_panel('人名翻译'):
                self._create_name_panel()

            # 人物分析
            with ui.tab_panel('人物分析'):
                self._create_analysis_panel()

            # 字符串翻译（菜单选项等）
            with ui.tab_panel('字符串翻译'):
                self._create_strings_panel()

            # 对话翻译
            with ui.tab_panel('对话翻译'):
                self._create_dialogue_panel()

            # 导出游戏
            with ui.tab_panel('导出游戏'):
                self._create_export_panel()

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
            self.translate_all_names_btn = ui.button('🌐 翻译全部人名', color='primary', on_click=self._translate_all_names)
            self.stop_names_btn = ui.button('⏹ 停止', color='red', on_click=self._stop_translation)
            self.stop_names_btn.set_visibility(False)
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

    def _create_analysis_panel(self):
        """创建人物分析面板"""
        # 状态栏
        with ui.row().classes('w-full items-center gap-2'):
            self.analysis_stats = ui.label('请先完成人名翻译').classes('text-subtitle1')
            ui.space()
            self.analyze_all_btn = ui.button('🤖 分析所有角色', color='primary',
                on_click=self._analyze_all_characters)
            self.analyze_btn = ui.button('🔄 刷新', on_click=self._refresh_analysis)

        ui.separator()

        # 角色分析表格
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

        # 自定义状态单元格
        self.analysis_table.add_slot('body-cell-status', '''
            <q-td :props="props">
                <q-chip :color="props.row.status === '已完成' ? 'green' : (props.row.status === '分析中' ? 'orange' : 'grey')"
                    text-color="white" dense size="sm">
                    {{ props.row.status }}
                </q-chip>
            </q-td>
        ''')

        # 自定义操作单元格
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

        # 监听事件
        self.analysis_table.on('analyze_character', self._on_analyze_character)
        self.analysis_table.on('view_character', self._on_view_character)

        # 日志
        ui.separator()
        ui.label('分析日志').classes('text-subtitle1')
        self.analysis_log = ui.log().classes('w-full h-48')

    def _create_strings_panel(self):
        """创建字符串翻译面板（菜单选项等）"""
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
            self.ui_translate_page_btn = ui.button('🚀 翻译本页', color='primary', on_click=self._ui_translate_page)
            self.ui_translate_all_btn = ui.button('⚡ 全部翻译', color='secondary', on_click=self._ui_translate_all)
            self.ui_stop_btn = ui.button('⏹ 停止', color='red', on_click=self._stop_translation)
            self.ui_stop_btn.set_visibility(False)
            ui.button('🔄 刷新', on_click=self._ui_refresh)

        # 搜索栏
        with ui.row().classes('w-full gap-2'):
            self.ui_search = ui.input(label='搜索', placeholder='搜索原文/译文').classes('flex-1')
            self.ui_filter = ui.select(
                options={'all': '全部', 'untranslated': '未翻译', 'translated': '已翻译'},
                label='筛选', value='all'
            )
            ui.button('🔍', on_click=self._ui_apply_filter)

        # 字符串表格
        self.ui_table = ui.table(
            columns=[
                {'name': 'index', 'label': '#', 'field': 'index', 'sortable': True, 'style': 'width: 50px'},
                {'name': 'original', 'label': '原文', 'field': 'original', 'style': 'white-space: normal; max-width: 400px'},
                {'name': 'translated', 'label': '译文', 'field': 'translated', 'style': 'white-space: normal; max-width: 400px'},
                {'name': 'status', 'label': '状态', 'field': 'status', 'style': 'width: 80px'},
                {'name': 'action', 'label': '操作', 'field': 'action', 'style': 'width: 100px'},
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
            self.translate_page_btn = ui.button('🚀 翻译本页', color='primary', on_click=self._dialogue_translate_page)
            self.translate_all_btn = ui.button('⚡ 全部翻译', color='secondary', on_click=self._dialogue_translate_all)
            self.stop_translate_btn = ui.button('⏹ 停止', color='red', on_click=self._stop_translation)
            self.stop_translate_btn.set_visibility(False)
            ui.button('🔄 刷新', on_click=self._dialogue_refresh)

        # 筛选
        with ui.row().classes('w-full gap-2'):
            self.dialogue_search = ui.input(label='搜索', placeholder='搜索原文/译文').classes('flex-1')
            self.dialogue_filter = ui.select(
                options={'all': '全部', 'untranslated': '未翻译', 'translated': '已翻译'},
                label='筛选', value='all'
            )
            self.dialogue_char_filter = ui.select(
                options=['全部'],
                label='角色', value='全部'
            )
            ui.button('🔍', on_click=self._dialogue_apply_filter)

        # 队列状态
        with ui.row().classes('w-full items-center gap-2'):
            self.queue_status = ui.label('队列状态: 空闲').classes('text-caption')
            self.queue_progress = ui.linear_progress(value=0, show_value=False).classes('flex-1')

        # 对话表格
        self.dialogue_table = ui.table(
            columns=[
                {'name': 'index', 'label': '#', 'field': 'index', 'sortable': True, 'style': 'width: 50px'},
                {'name': 'character', 'label': '角色', 'field': 'character', 'sortable': True, 'style': 'width: 80px'},
                {'name': 'original', 'label': '原文', 'field': 'original', 'style': 'white-space: normal; max-width: 400px'},
                {'name': 'translated', 'label': '译文', 'field': 'translated', 'style': 'white-space: normal; max-width: 400px'},
                {'name': 'status', 'label': '状态', 'field': 'status', 'style': 'width: 80px'},
                {'name': 'action', 'label': '操作', 'field': 'action', 'style': 'width: 100px'},
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
                <q-btn flat dense color="secondary" label="上下文"
                    @click="$parent.$emit('show_context', props.row)" />
            </q-td>
        ''')

        # 监听事件
        self.dialogue_table.on('update:dialogue', self._on_dialogue_update)
        self.dialogue_table.on('translate_dialogue', self._on_dialogue_translate)
        self.dialogue_table.on('show_context', self._on_show_context)

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
                self.config_context = ui.number(label='翻译上下文行数', value=3, min=0)
                self.config_max_context = ui.number(label='模型最大上下文(K)', value=8, min=1,
                    placeholder='如8=8K, 32=32K, 128=128K')

            with ui.row().classes('gap-2'):
                ui.button('💾 保存配置', color='primary', on_click=self._save_config_form)
                ui.button('🔄 清空表单', on_click=self._clear_config_form)

        ui.separator()

        # SDK 配置
        with ui.card().classes('w-full'):
            ui.label('Ren\'Py SDK 配置').classes('text-h6')
            ui.label('用于生成正确的翻译文件格式').classes('text-caption text-grey')

            with ui.row().classes('gap-2 items-center w-full'):
                self.sdk_path_input = ui.input(
                    label='SDK 路径',
                    placeholder='/path/to/renpy-sdk',
                    value=self._find_sdk_path()
                ).classes('flex-1')
                ui.button('🔍 自动查找', on_click=self._auto_find_sdk)
                ui.button('✅ 测试', on_click=self._test_sdk)

            self.sdk_status = ui.label('').classes('text-caption')

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

    # ========== SDK 管理 ==========

    def _find_sdk_path(self) -> str:
        """查找 SDK 路径"""
        # 1. 从配置中获取
        configs = self.config_manager.load_all_configs()
        for c in configs:
            if hasattr(c, 'sdk_path') and c.sdk_path:
                return c.sdk_path

        # 2. 自动查找
        sdk_path = self.sdk_manager.find_sdk(str(Path(__file__).parent.parent))
        return str(sdk_path) if sdk_path else ""

    def _auto_find_sdk(self):
        """自动查找 SDK"""
        sdk_path = self.sdk_manager.find_sdk(str(Path(__file__).parent.parent))
        if sdk_path:
            self.sdk_path_input.value = str(sdk_path)
            self.sdk_status.text = f'✅ 找到 SDK: {sdk_path}'
            self.sdk_status.classes(replace='text-caption text-positive')
        else:
            self.sdk_status.text = '❌ 未找到 Ren\'Py SDK'
            self.sdk_status.classes(replace='text-caption text-negative')

    def _test_sdk(self):
        """测试 SDK"""
        sdk_path = self.sdk_path_input.value
        if not sdk_path:
            self.sdk_status.text = '❌ 请输入 SDK 路径'
            self.sdk_status.classes(replace='text-caption text-negative')
            return

        self.sdk_manager.sdk_path = Path(sdk_path)
        if self.sdk_manager._is_valid_sdk(Path(sdk_path)):
            self.sdk_status.text = '✅ SDK 有效'
            self.sdk_status.classes(replace='text-caption text-positive')
        else:
            self.sdk_status.text = '❌ 无效的 SDK 路径'
            self.sdk_status.classes(replace='text-caption text-negative')

    def _create_project(self, name, game_dir, model):
        """创建项目"""
        if not name or not game_dir:
            ui.notify('请填写完整信息', type='warning')
            return

        # 禁用创建按钮
        self.create_btn.disable()
        self.create_btn.text = '创建中...'
        ui.notify('正在创建项目，请稍候...', type='info', timeout=2000)

        # 存储创建状态
        self._creating_project = {
            'done': False,
            'success': False,
            'message': '',
            'name': name,
            'game_dir': game_dir,
            'model': model
        }

        # 在线程池中执行
        self.executor.submit(self._create_project_thread, name, game_dir, model)

        # 启动定时器检查状态
        ui.timer(0.5, self._check_project_creation, once=True)

    def _create_project_thread(self, name, game_dir, model):
        """在线程池中创建项目"""
        import shutil

        try:
            project = self.project_manager.create_project(name, game_dir, model or '')

            # 创建工作目录（直接使用解包后的游戏）
            project_dir = self.project_manager._get_project_dir(name)
            game_work_dir = project_dir / 'game'

            # 清理旧的工作目录
            if game_work_dir.exists():
                shutil.rmtree(game_work_dir)

            # 1. 复制游戏到项目目录
            print(f'[创建项目] 复制游戏文件...')
            shutil.copytree(game_dir, game_work_dir)

            # 2. 解包 rpa 文件
            print(f'[创建项目] 解包 rpa 文件...')
            self.parser.parse_directory(
                str(game_work_dir),
                extract_rpa=True,
                decompile_rpyc=True,
                work_dir=str(game_work_dir)
            )

            # 3. 清理冲突文件
            print(f'[创建项目] 清理冲突文件...')
            self._cleanup_conflicts(game_work_dir)

            # 4. 使用 SDK 生成翻译文件
            sdk_path = self.sdk_path_input.value if hasattr(self, 'sdk_path_input') else ''
            if not sdk_path:
                raise Exception('请先配置 Ren\'Py SDK 路径')

            print(f'[创建项目] 使用 SDK 生成翻译文件...')
            self.sdk_manager.sdk_path = Path(sdk_path)
            sdk_result = self.sdk_manager.generate_translations(str(game_work_dir), 'chinese')

            if not sdk_result['success']:
                raise Exception(f'SDK 生成翻译文件失败: {sdk_result["message"]}')

            print(f'[创建项目] SDK 生成成功')

            # 5. 解析角色信息
            print(f'[创建项目] 解析角色信息...')
            fresh_parser = RenpyParser()
            result = fresh_parser.parse_directory(
                str(game_work_dir),
                extract_rpa=False,
                decompile_rpyc=False
            )
            project.characters = [self._character_to_dict(c) for c in result['characters']]

            # 初始化人名词典
            for char in result['characters']:
                if char.name and char.name not in project.char_dict:
                    project.char_dict[char.name] = ''

            # 存储变量名到显示名的映射
            if '__variable_map__' not in project.char_dict:
                project.char_dict['__variable_map__'] = {}
            for char in result['characters']:
                if char.variable and char.name:
                    project.char_dict['__variable_map__'][char.variable] = char.name

            # 6. 解析翻译文件
            tl_dir = game_work_dir / 'game' / 'tl' / 'chinese'
            if not tl_dir.exists():
                raise Exception('翻译文件目录不存在')

            print(f'[创建项目] 解析翻译文件...')
            tl_result = self._parse_translation_files(tl_dir, str(game_work_dir), project.char_dict)
            project.dialogues = tl_result.get('dialogues', [])
            project.ui_texts = tl_result.get('ui_texts', [])

            # 保存项目
            self.project_manager.save_project(project)

            self._creating_project['success'] = True
            self._creating_project['message'] = f'项目创建成功！共 {len(project.dialogues)} 条对话'

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._creating_project['success'] = False
            self._creating_project['message'] = f'创建失败: {str(e)}'

        finally:
            self._creating_project['done'] = True

    def _cleanup_conflicts(self, game_dir: Path):
        """清理冲突文件，避免 SDK 报错"""
        game_path = Path(game_dir)
        game_sub = game_path / 'game'

        # 1. 删除 .rpa 文件（已经解包，不再需要）
        for rpa_file in game_sub.glob('*.rpa'):
            print(f'[清理] 删除 rpa 文件: {rpa_file.name}')
            rpa_file.unlink()

        # 2. 清理 game/scripts/ 下的垃圾文件
        scripts_dir = game_sub / 'scripts'
        if scripts_dir.exists():
            for rpy_file in scripts_dir.glob('*.rpy'):
                # 检查是否有对应的 .py 文件冲突
                py_file = rpy_file.with_suffix('.py')
                if py_file.exists():
                    # 检查 .rpy 文件是否是垃圾文件
                    try:
                        with open(rpy_file, 'r', encoding='utf-8') as f:
                            content = f.read(100)
                        if '从.rpyc文件自动提取' in content or '\x00' in content:
                            print(f'[清理] 删除垃圾文件: {rpy_file.name}')
                            rpy_file.unlink()
                    except:
                        print(f'[清理] 删除无法读取的文件: {rpy_file.name}')
                        rpy_file.unlink()

    def _parse_translation_files(self, tl_dir, game_dir, name_dict=None) -> dict:
        """解析 SDK 生成的翻译文件，提取需要翻译的内容"""
        import re

        dialogues = []
        ui_texts = []
        tl_path = Path(tl_dir)
        game_path = Path(game_dir)

        for tl_file in tl_path.rglob('*.rpy'):
            try:
                with open(tl_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                # 分离对话块和字符串块
                dialogue_lines = []
                strings_lines = []
                in_strings = False

                for line in lines:
                    if 'translate chinese strings:' in line:
                        in_strings = True

                    if in_strings:
                        strings_lines.append(line)
                    else:
                        dialogue_lines.append(line)

                # 解析对话块
                if dialogue_lines:
                    self._parse_dialogue_blocks(dialogue_lines, tl_file, game_path, dialogues, ui_texts)

                # 解析字符串块
                if strings_lines:
                    self._parse_strings_block(strings_lines, tl_file, game_path, ui_texts, name_dict)

            except Exception as e:
                print(f'[解析翻译文件失败] {tl_file}: {e}')

        return {
            'dialogues': dialogues,
            'ui_texts': ui_texts
        }

    def _parse_dialogue_blocks(self, lines, tl_file, game_path, dialogues, ui_texts):
        """解析对话格式的翻译块"""
        import re

        # 格式:
        # translate chinese label_hash:
        #     # mc "对话内容"   <- 注释行（包含角色信息）
        #     mc "对话内容"    <- 内容行（实际显示的文本）
        # 或
        #     # "旁白内容"     <- 注释行（无角色）
        #     "旁白内容"      <- 内容行

        current_file = str(tl_file.relative_to(tl_file.parent.parent.parent))
        if current_file.startswith('tl/chinese/'):
            current_file = current_file[len('tl/chinese/'):]

        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            i += 1

            # 跳过空行和 translate 行
            if not line or line.startswith('translate '):
                continue

            # 跳过文件路径注释
            if re.match(r'^\s+#\s+game/', line):
                continue

            # 检查注释行（包含角色信息）
            comment_match = re.match(r'^\s+#\s*(.*)', line)
            if comment_match:
                comment_text = comment_match.group(1).strip()

                # 跳过空注释
                if not comment_text:
                    continue

                # 从注释行解析角色和文本
                # 格式: mc "text" 或 "text"
                char_match = re.match(r'^(\w+)\s+"(.*)"', comment_text)
                narration_match = re.match(r'^"(.*)"', comment_text)

                if char_match:
                    character = char_match.group(1)
                    text = char_match.group(2).replace('\\"', '"')
                elif narration_match:
                    character = ''
                    text = narration_match.group(1).replace('\\"', '"')
                else:
                    # 不是对话格式，跳过
                    continue

                # 跳过空内容
                if not text or text == '""':
                    # 跳过内容行
                    if i < len(lines):
                        i += 1
                    continue

                # 跳过内容行（已经从注释行提取了）
                if i < len(lines):
                    i += 1

                full_path = str(game_path / 'game' / current_file)

                entry = {
                    'file_path': full_path,
                    'line_number': 0,
                    'character': character,
                    'original_text': text,
                    'translated_text': '',  # 等待翻译
                    'is_translated': False,
                    'context_before': [],
                    'context_after': []
                }

                # 判断是对话还是字符串
                if any(f in current_file for f in ['screens', 'gui', 'options', 'common']):
                    ui_texts.append(entry)
                else:
                    dialogues.append(entry)

    def _parse_strings_block(self, lines, tl_file, game_path, ui_texts, name_dict=None):
        """解析 strings 格式的翻译块（菜单选项等）"""
        import re

        # 格式:
        # translate chinese strings:
        #     # game/file.rpy:line
        #     old "原文"
        #     new "译文"

        current_file = None
        current_line = None
        current_old = None

        # 人名词典（用于判断是否是人名）
        if name_dict is None:
            name_dict = {}

        for line in lines:
            line = line.rstrip()

            # 检查文件路径注释
            file_match = re.match(r'^\s+#\s+(.+):(\d+)', line)
            if file_match:
                current_file = file_match.group(1)
                current_line = int(file_match.group(2))
                continue

            # 检查 old 行
            old_match = re.match(r'^\s+old\s+"(.*)"', line)
            if old_match:
                current_old = old_match.group(1).replace('\\"', '"')
                continue

            # 检查 new 行
            new_match = re.match(r'^\s+new\s+"(.*)"', line)
            if new_match and current_old is not None:
                new_text = new_match.group(1).replace('\\"', '"')

                # 如果是人名词典中的词，跳过（会在人名翻译中处理）
                if current_old in name_dict:
                    current_old = None
                    continue

                full_path = str(game_path / 'game' / current_file) if current_file else ''

                entry = {
                    'file_path': full_path,
                    'line_number': current_line or 0,
                    'character': '',
                    'original_text': current_old,
                    'translated_text': '',  # 等待翻译
                    'is_translated': False,
                    'context_before': [],
                    'context_after': []
                }

                ui_texts.append(entry)
                current_old = None

    def _check_project_creation(self):
        """检查项目创建状态"""
        if not hasattr(self, '_creating_project') or not self._creating_project['done']:
            # 还没完成，继续检查
            if hasattr(self, '_creating_project') and not self._creating_project['done']:
                ui.timer(0.5, self._check_project_creation, once=True)
            return

        # 完成，更新UI
        if self._creating_project['success']:
            ui.notify(self._creating_project['message'], type='positive')
            self._refresh_project_list()
        else:
            ui.notify(self._creating_project['message'], type='negative')

        # 恢复按钮
        self.create_btn.enable()
        self.create_btn.text = '➕ 创建项目'

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
        self._refresh_analysis()
        self._ui_refresh()
        self._dialogue_refresh()
        self._refresh_export_stats()
        self.status_label.text = f'当前项目: {name}'

        ui.notify(f'已打开项目: {name}', type='positive')

    def _save_project(self, show_notify=True):
        """保存项目"""
        if not self.current_project:
            if show_notify:
                ui.notify('未打开项目', type='warning')
            return False

        # 更新位置
        filtered = self._get_filtered_dialogues()
        if filtered and self.current_index < len(filtered):
            dialogue = filtered[self.current_index]
            self.current_project.last_position = {
                'index': self.current_index,
                'file': dialogue.get('file_path', ''),
                'line': dialogue.get('line_number', 0)
            }

        success = self.project_manager.save_project(self.current_project)

        # 只在主线程且需要时显示通知
        if show_notify:
            if success:
                ui.notify('项目已保存', type='positive')
            else:
                ui.notify('保存失败', type='negative')

        return success

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
                character_dict=self.current_project.char_dict,
                debug=True
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

    async def _translate_all_names(self):
        """翻译全部人名"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return
        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        # 获取未翻译的人名
        char_dict = self.current_project.char_dict
        to_translate = [(k, v) for k, v in char_dict.items()
                       if not k.startswith('__') and (not v or not v.strip())]

        if not to_translate:
            ui.notify('所有人名已翻译', type='info')
            return

        total = len(to_translate)
        success = 0

        # 切换按钮状态
        self.is_translating = True
        self.stop_translation = False
        self.translate_all_names_btn.set_visibility(False)
        self.stop_names_btn.set_visibility(True)

        loop = asyncio.get_event_loop()

        for en_name, _ in to_translate:
            if self.stop_translation:
                break

            try:
                print(f'[人名翻译] 正在翻译: {en_name}')
                translated = await loop.run_in_executor(
                    self.executor,
                    lambda n=en_name: self.translator.translate_name(n, debug=True)
                )
                print(f'[人名翻译] 结果: {en_name} -> {translated}')

                if translated and translated.strip():
                    self.current_project.char_dict[en_name] = translated
                    success += 1
                else:
                    print(f'[人名翻译] 结果为空，跳过')

                self._name_refresh()
                self.name_stats.text = f'翻译进度: {success}/{total}'
            except Exception as e:
                print(f'[人名翻译] 失败: {en_name} - {e}')

        # 保存项目
        self._save_project(show_notify=False)
        self._name_refresh()

        # 恢复按钮
        self.is_translating = False
        self.stop_translation = False
        self.translate_all_names_btn.set_visibility(True)
        self.stop_names_btn.set_visibility(False)

        if self.stop_translation:
            ui.notify(f'翻译已停止: {success}/{total}', type='warning')
        else:
            ui.notify(f'翻译完成: {success}/{total}', type='positive')

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
        """执行人名翻译（线程池中执行）"""
        row = queue_item['row']
        try:
            translated = self.translator.translate_name(row['original'], debug=True)
            self.current_project.char_dict[row['original']] = translated
            return True
        except Exception as e:
            print(f'❌ 人名翻译失败: {e}')
            return False

    def _update_queue_status(self):
        """更新队列状态显示"""
        if hasattr(self, 'queue_status'):
            total = len(self.translate_queue)
            translating = sum(1 for v in self.translate_queue.values() if v['status'] == '翻译中')
            queued = sum(1 for v in self.translate_queue.values() if v['status'] == '排队中')
            self.queue_status.text = f'队列状态: {translating}个翻译中, {queued}个排队'

    def _process_next_in_queue(self):
        """处理队列中的下一个任务（异步）"""
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
        self._refresh_current_table()  # 立即刷新UI

        # 异步执行翻译
        asyncio.create_task(self._async_translate(next_key))

    def _refresh_current_table(self):
        """刷新当前显示的表格"""
        # 根据队列中的任务类型刷新对应的表格
        types_in_queue = set(item['type'] for item in self.translate_queue.values())
        if 'name' in types_in_queue:
            self._name_refresh()
        if 'ui' in types_in_queue:
            self._ui_refresh()
        if 'dialogue' in types_in_queue:
            self._dialogue_refresh()
        # 如果队列为空，刷新所有表格
        if not self.translate_queue:
            self._name_refresh()
            self._ui_refresh()
            self._dialogue_refresh()

    async def _async_translate(self, key):
        """异步执行翻译任务"""
        queue_item = self.translate_queue.get(key)
        if not queue_item:
            self.current_translating -= 1
            return

        loop = asyncio.get_event_loop()
        try:
            # 在线程池中执行翻译（避免阻塞事件循环）
            if queue_item['type'] == 'name':
                success = await loop.run_in_executor(
                    self.executor, self._do_name_translate, queue_item
                )
            elif queue_item['type'] == 'ui':
                success = await loop.run_in_executor(
                    self.executor, self._do_ui_translate, queue_item
                )
            elif queue_item['type'] == 'dialogue':
                success = await loop.run_in_executor(
                    self.executor, self._do_dialogue_translate, queue_item
                )
            else:
                success = False

            # 翻译成功后保存项目（不显示通知，避免异步UI问题）
            if success:
                self._save_project(show_notify=False)

        except Exception as e:
            print(f'翻译失败: {str(e)}')
            success = False

        # 标记为完成并刷新UI
        if key in self.translate_queue:
            self.translate_queue[key]['status'] = '完成'
            self._refresh_current_table()

        # 短暂延迟后移除（让用户看到完成状态）
        await asyncio.sleep(0.5)

        # 移除已完成的任务
        if key in self.translate_queue:
            del self.translate_queue[key]
        self.current_translating -= 1
        self._update_queue_status()
        self._refresh_current_table()

        # 处理下一个
        self._process_next_in_queue()

    async def _async_batch_translate(self, translate_type, items, translate_all=False):
        """异步批量翻译"""
        total = len(items)
        success = 0
        stopped = False

        # 切换按钮状态
        self.is_translating = True
        self.stop_translation = False

        if translate_type == 'ui':
            self.ui_translate_page_btn.set_visibility(False)
            self.ui_translate_all_btn.set_visibility(False)
            self.ui_stop_btn.set_visibility(True)
        elif translate_type == 'dialogue':
            self.translate_page_btn.set_visibility(False)
            self.translate_all_btn.set_visibility(False)
            self.stop_translate_btn.set_visibility(True)

        loop = asyncio.get_event_loop()

        for i, item in enumerate(items):
            # 检查是否停止
            if self.stop_translation:
                stopped = True
                break

            try:
                # 在线程池中执行翻译
                if translate_type == 'ui':
                    translated = await loop.run_in_executor(
                        self.executor,
                        lambda t=item['original_text']: self.translator.translate_text(text=t, debug=True)
                    )
                elif translate_type == 'dialogue':
                    translated = await loop.run_in_executor(
                        self.executor,
                        lambda t=item['original_text'], c=item.get('character', ''),
                               cb=item.get('context_before', []), ca=item.get('context_after', []):
                            self.translator.translate_text(
                                text=t, character=c,
                                context_before=cb, context_after=ca,
                                character_dict=self.current_project.char_dict,
                                debug=True
                            )
                    )
                else:
                    continue

                item['translated_text'] = translated
                item['is_translated'] = True
                success += 1

                # 立即保存
                self._save_project(show_notify=False)

                # 刷新UI
                if translate_type == 'ui':
                    self._ui_refresh()
                elif translate_type == 'dialogue':
                    self._dialogue_refresh()

                # 更新进度
                if hasattr(self, 'queue_status'):
                    self.queue_status.text = f'翻译进度: {success}/{total}'

            except Exception as e:
                stopped = True
                print(f'❌ 翻译失败: {e}')
                break

        # 刷新UI
        if translate_type == 'ui':
            self._ui_refresh()
        elif translate_type == 'dialogue':
            self._dialogue_refresh()

        # 恢复按钮状态
        self.is_translating = False
        self.stop_translation = False

        if translate_type == 'ui':
            self.ui_translate_page_btn.set_visibility(True)
            self.ui_translate_all_btn.set_visibility(True)
            self.ui_stop_btn.set_visibility(False)
        elif translate_type == 'dialogue':
            self.translate_page_btn.set_visibility(True)
            self.translate_all_btn.set_visibility(True)
            self.stop_translate_btn.set_visibility(False)

        if stopped:
            print(f'翻译已停止: {success}/{total}')
        else:
            print(f'翻译完成: {success}/{total}')

    def _name_refresh(self):
        """刷新人名表格"""
        if not self.current_project:
            return

        char_dict = self.current_project.char_dict
        # 过滤掉 __profiles__ 等特殊键
        items = [(k, v) for k, v in char_dict.items() if not k.startswith('__')]
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
            elif cn and cn.strip():  # 只判断是否有文字，不判断是否与原文相同
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

    # ========== 字符串翻译 ==========

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
        """执行字符串翻译（线程池中执行）"""
        idx = queue_item['idx']
        if 0 <= idx < len(self.current_project.ui_texts):
            item = self.current_project.ui_texts[idx]
            try:
                translated = self.translator.translate_ui(item['original_text'], debug=True)
                item['translated_text'] = translated
                item['is_translated'] = True
                return True
            except Exception as e:
                print(f'❌ 字符串翻译失败: {e}')
                return False
        return False

    def _ui_apply_filter(self):
        """应用搜索筛选"""
        self.ui_current_page = 0
        self._ui_refresh()

    def _ui_refresh(self):
        """刷新字符串表格"""
        if not self.current_project:
            return

        all_items = self.current_project.ui_texts

        # 创建带全局索引的列表
        indexed_items = [(i, d) for i, d in enumerate(all_items)]

        # 应用筛选
        filter_mode = self.ui_filter.value if hasattr(self, 'ui_filter') else 'all'
        if filter_mode == 'untranslated':
            indexed_items = [(i, d) for i, d in indexed_items if not d.get('is_translated', False)]
        elif filter_mode == 'translated':
            indexed_items = [(i, d) for i, d in indexed_items if d.get('is_translated', False)]

        # 应用搜索
        search_text = self.ui_search.value if hasattr(self, 'ui_search') else ''
        if search_text:
            s = search_text.lower()
            indexed_items = [(i, d) for i, d in indexed_items
                           if s in d.get('original_text', '').lower() or s in d.get('translated_text', '').lower()]

        total = len(indexed_items)
        translated = sum(1 for _, d in indexed_items if d.get('is_translated', False))

        page_size = int(self.ui_page_size.value)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        self.ui_current_page = min(self.ui_current_page, total_pages - 1)

        start = self.ui_current_page * page_size
        end = min(start + page_size, total)
        page_items = indexed_items[start:end]

        rows = []
        for i, (global_idx, d) in enumerate(page_items):
            # 检查队列状态
            key = f"ui_{global_idx}"
            status = '待翻译'
            if key in self.translate_queue:
                status = self.translate_queue[key]['status']
            elif d.get('is_translated', False):
                status = '完成'

            rows.append({
                'index': global_idx + 1,
                'original': d.get('original_text', ''),
                'translated': d.get('translated_text', ''),
                'status': status,
                'action': global_idx  # 使用全局索引
            })

        self.ui_table.rows = rows
        self.ui_stats.text = f'📊 总计: {total} | ✅ 已翻译: {translated}'
        self.ui_page_label.text = f'第 {self.ui_current_page + 1} / {total_pages} 页'
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
        """翻译当前页字符串（异步）"""
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

        # 异步执行批量翻译
        asyncio.create_task(self._async_batch_translate('ui', to_translate))

    def _ui_translate_all(self):
        """翻译所有字符串（异步）"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return
        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        to_translate = [d for d in self.current_project.ui_texts if not d.get('is_translated', False)]
        if not to_translate:
            ui.notify('所有字符串已翻译', type='info')
            return

        asyncio.create_task(self._async_batch_translate('ui', to_translate, translate_all=True))

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

    def _on_show_context(self, e):
        """显示上下文对话框（从对话列表动态计算）"""
        row = e.args
        if not row or not self.current_project:
            return

        idx = row['action']
        dialogues = self.current_project.dialogues

        if 0 <= idx < len(dialogues):
            current_item = dialogues[idx]
            current_text = current_item.get('original_text', '')
            current_char = current_item.get('character', '') or '旁白'

            # 从当前项目的模型配置获取上下文行数
            context_lines = 5  # 默认值
            if self.current_project.model_config_name:
                config = self.config_manager.get_config_by_name(self.current_project.model_config_name)
                if config:
                    context_lines = config.context_lines

            # 从对话列表动态计算上下文
            context_before = dialogues[max(0, idx - context_lines):idx]
            context_after = dialogues[idx + 1:idx + 1 + context_lines]

            # 创建对话框
            with ui.dialog() as dialog, ui.card().classes('w-full max-w-3xl'):
                ui.label(f'📖 上下文（前后各{context_lines}句）').classes('text-h6')

                # 前文
                if context_before:
                    ui.label('前文:').classes('text-subtitle2 text-grey')
                    for item in context_before:
                        char = item.get('character', '') or '旁白'
                        text = item.get('original_text', '')
                        ui.label(f'  【{char}】{text}').classes('text-body2')

                # 当前行（高亮）
                ui.separator()
                ui.label(f'>>> 【{current_char}】{current_text} <<<').classes('text-body1 text-primary font-bold text-h6')

                # 后文
                if context_after:
                    ui.separator()
                    ui.label('后文:').classes('text-subtitle2 text-grey')
                    for item in context_after:
                        char = item.get('character', '') or '旁白'
                        text = item.get('original_text', '')
                        ui.label(f'  【{char}】{text}').classes('text-body2')

                # 关闭按钮
                ui.button('关闭', on_click=dialog.close).classes('mt-4')

            dialog.open()

    def _do_dialogue_translate(self, queue_item):
        """执行对话翻译（线程池中执行）"""
        idx = queue_item['idx']
        if 0 <= idx < len(self.current_project.dialogues):
            item = self.current_project.dialogues[idx]
            prompt_text = item['original_text']
            character = item.get('character', '')
            try:
                translated = self.translator.translate_text(
                    text=prompt_text,
                    character=character,
                    context_before=item.get('context_before', []),
                    context_after=item.get('context_after', []),
                    character_dict=self.current_project.char_dict,
                    debug=True
                )
                item['translated_text'] = translated
                item['is_translated'] = True
                return True
            except Exception as e:
                print(f'❌ 对话翻译失败: {e}')
                print(f'📝 角色: {character}')
                print(f'📝 原文: {prompt_text}')
                return False
        return False

    def _dialogue_refresh(self):
        """刷新对话表格"""
        if not self.current_project:
            return

        # 更新角色下拉菜单（显示显示名而不是变量名）
        all_dialogues = self.current_project.dialogues
        variable_map = self.current_project.char_dict.get('__variable_map__', {})

        # 获取所有出现的变量名
        var_names = sorted(set(d.get('character', '') for d in all_dialogues if d.get('character', '')))

        # 转换为显示名
        char_options = ['全部']
        self._char_display_map = {'全部': ''}  # 显示名 -> 变量名
        for var in var_names:
            display = variable_map.get(var, var)
            char_options.append(display)
            self._char_display_map[display] = var

        if hasattr(self, 'dialogue_char_filter'):
            self.dialogue_char_filter.set_options(char_options)
            # 保持当前选择（如果还在列表中）
            current_display = self.dialogue_char_filter.value
            if current_display not in char_options:
                self.dialogue_char_filter.set_value('全部')
                self.dialogue_char_text = ''

        # 创建带全局索引的对话列表
        indexed_dialogues = [(i, d) for i, d in enumerate(all_dialogues)]

        # 应用筛选
        if self.dialogue_filter_mode == 'untranslated':
            indexed_dialogues = [(i, d) for i, d in indexed_dialogues if not d.get('is_translated', False)]
        elif self.dialogue_filter_mode == 'translated':
            indexed_dialogues = [(i, d) for i, d in indexed_dialogues if d.get('is_translated', False)]

        if self.dialogue_search_text:
            s = self.dialogue_search_text.lower()
            indexed_dialogues = [(i, d) for i, d in indexed_dialogues
                               if s in d.get('original_text', '').lower() or s in d.get('translated_text', '').lower()]

        if self.dialogue_char_text:
            indexed_dialogues = [(i, d) for i, d in indexed_dialogues
                               if d.get('character', '') == self.dialogue_char_text]

        total = len(indexed_dialogues)
        translated = sum(1 for _, d in indexed_dialogues if d.get('is_translated', False))

        page_size = int(self.dialogue_page_size.value)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        self.dialogue_current_page = min(self.dialogue_current_page, total_pages - 1)

        start = self.dialogue_current_page * page_size
        end = min(start + page_size, total)
        page_items = indexed_dialogues[start:end]

        rows = []
        for i, (global_idx, d) in enumerate(page_items):
            # 检查队列状态
            key = f"dialogue_{global_idx}"
            status = '待翻译'
            if key in self.translate_queue:
                status = self.translate_queue[key]['status']
            elif d.get('is_translated', False):
                status = '完成'

            rows.append({
                'index': global_idx + 1,
                'character': d.get('character', '') or '旁白',
                'original': d.get('original_text', ''),
                'translated': d.get('translated_text', ''),
                'status': status,
                'action': global_idx  # 使用全局索引
            })

        self.dialogue_table.rows = rows
        self.dialogue_stats.text = f'📊 总计: {total} | ✅ 已翻译: {translated}'
        self.dialogue_page_label.text = f'第 {self.dialogue_current_page + 1} / {total_pages} 页'

    def _dialogue_apply_filter(self):
        self.dialogue_search_text = self.dialogue_search.value
        self.dialogue_filter_mode = self.dialogue_filter.value

        # 将显示名转换为变量名
        selected_char = self.dialogue_char_filter.value
        if selected_char == '全部':
            self.dialogue_char_text = ''
        elif hasattr(self, '_char_display_map'):
            self.dialogue_char_text = self._char_display_map.get(selected_char, selected_char)
        else:
            self.dialogue_char_text = selected_char

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

    def _check_dialogue_prerequisites(self):
        """检查对话翻译的前置条件"""
        if not self.current_project:
            return False, '请先打开项目'

        # 检查人名翻译（只判断是否有文字，不判断是否与原文相同）
        char_dict = self.current_project.char_dict
        untranslated_names = [k for k, v in char_dict.items()
                            if not k.startswith('__') and isinstance(v, str) and (not v or not v.strip())]
        if untranslated_names:
            return False, f'请先完成人名翻译（还有 {len(untranslated_names)} 个未翻译）'

        # 检查人物分析
        profiles = char_dict.get('__profiles__', {})
        characters = self.current_project.characters
        unanalyzed = [c['name'] for c in characters if c.get('name') and c['name'] not in profiles]
        if unanalyzed:
            return False, f'请先完成人物分析（还有 {len(unanalyzed)} 个未分析）'

        return True, ''

    def _dialogue_translate_page(self):
        """翻译当前页对话（异步）"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return
        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        # 检查前置条件
        ok, msg = self._check_dialogue_prerequisites()
        if not ok:
            ui.notify(msg, type='warning')
            return

        page_size = int(self.dialogue_page_size.value)
        start = self.dialogue_current_page * page_size
        end = min(start + page_size, len(self.current_project.dialogues))
        page_items = self.current_project.dialogues[start:end]

        to_translate = [d for d in page_items if not d.get('is_translated', False)]
        if not to_translate:
            ui.notify('当前页已全部翻译', type='info')
            return

        # 异步执行批量翻译
        asyncio.create_task(self._async_batch_translate('dialogue', to_translate))

    def _dialogue_translate_all(self):
        """翻译所有对话（异步）"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return
        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        # 检查前置条件
        ok, msg = self._check_dialogue_prerequisites()
        if not ok:
            ui.notify(msg, type='warning')
            return

        # 获取所有未翻译的对话
        to_translate = [d for d in self.current_project.dialogues if not d.get('is_translated', False)]
        if not to_translate:
            ui.notify('所有对话已翻译', type='info')
            return

        # 异步执行全部翻译
        asyncio.create_task(self._async_batch_translate('dialogue', to_translate, translate_all=True))

    def _stop_translation(self):
        """停止翻译"""
        self.stop_translation = True
        ui.notify('正在停止翻译...', type='warning')

    # ========== 导出游戏 ==========

    def _create_export_panel(self):
        """创建导出游戏面板"""
        ui.label('📦 导出翻译后的游戏').classes('text-h5')
        ui.label('将翻译后的游戏导出为独立目录，可直接运行，不影响原版游戏').classes('text-body1 text-grey')

        ui.separator()

        # 统计信息
        with ui.card().classes('w-full'):
            self.export_stats = ui.label('请先打开项目').classes('text-subtitle1')

            # 翻译进度统计
            with ui.row().classes('gap-8'):
                self.export_dialogue_stats = ui.label('对话翻译: -').classes('text-body1')
                self.export_ui_stats = ui.label('字符串翻译: -').classes('text-body1')
                self.export_name_stats = ui.label('人名翻译: -').classes('text-body1')

        ui.separator()

        # 导出选项
        with ui.card().classes('w-full'):
            ui.label('导出选项').classes('text-h6')

            self.export_include_ui = ui.checkbox('包含字符串翻译', value=True)
            self.export_include_dialogue = ui.checkbox('包含对话翻译', value=True)

        ui.separator()

        # 导出按钮
        with ui.row().classes('gap-2'):
            self.export_btn = ui.button('📦 开始导出', color='positive',
                on_click=self._export_game).classes('px-8')
            ui.button('🔄 刷新统计', on_click=self._refresh_export_stats)

        # 导出日志
        ui.separator()
        ui.label('导出日志').classes('text-subtitle1')
        self.export_log = ui.log().classes('w-full h-64')

    def _refresh_export_stats(self):
        """刷新导出统计"""
        if not self.current_project:
            return

        dialogues = self.current_project.dialogues
        ui_texts = self.current_project.ui_texts
        char_dict = self.current_project.char_dict

        # 对话翻译统计
        dialogue_total = len(dialogues)
        dialogue_translated = sum(1 for d in dialogues if d.get('is_translated', False))
        self.export_dialogue_stats.text = f'对话翻译: {dialogue_translated}/{dialogue_total}'

        # 字符串翻译统计
        ui_total = len(ui_texts)
        ui_translated = sum(1 for u in ui_texts if u.get('is_translated', False))
        self.export_ui_stats.text = f'字符串翻译: {ui_translated}/{ui_total}'

        # 人名翻译统计
        name_total = len([k for k in char_dict.keys() if not k.startswith('__')])
        name_translated = len([k for k, v in char_dict.items()
                              if not k.startswith('__') and v and v.strip()])
        self.export_name_stats.text = f'人名翻译: {name_translated}/{name_total}'

        # 总体状态
        total = dialogue_total + ui_total + name_total
        translated = dialogue_translated + ui_translated + name_translated
        percent = (translated / total * 100) if total > 0 else 0

        self.export_stats.text = f'📊 总体进度: {translated}/{total} ({percent:.1f}%)'

    def _export_game(self):
        """导出翻译后的游戏"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return

        # 检查是否有翻译内容
        translated_count = sum(1 for d in self.current_project.dialogues if d.get('is_translated', False))
        if translated_count == 0:
            ui.notify('没有已翻译的内容可导出', type='warning')
            return

        # 禁用按钮
        self.export_btn.disable()
        self.export_btn.text = '导出中...'

        # 异步执行导出
        asyncio.create_task(self._async_export_game())

    async def _async_export_game(self):
        """异步导出游戏"""
        loop = asyncio.get_event_loop()
        log_queue = Queue()  # 线程安全队列

        try:
            # 清空日志
            self.export_log.clear()
            self.export_log.push('开始导出游戏...')

            # 启动定时器处理日志队列
            async def process_log_queue():
                while True:
                    # 非阻塞地获取日志
                    try:
                        msg = log_queue.get_nowait()
                        if msg == '__DONE__':
                            break
                        self.export_log.push(msg)
                    except:
                        pass
                    await asyncio.sleep(0.1)  # 每100ms检查一次

            # 启动日志处理任务
            log_task = asyncio.create_task(process_log_queue())

            # 在线程池中执行导出
            result = await loop.run_in_executor(
                self.executor,
                lambda: self._export_game_thread(log_queue)
            )

            # 等待日志处理完成
            await log_task

            if result['success']:
                self.export_log.push('')
                self.export_log.push('✅ 导出完成！')
            else:
                self.export_log.push(f'❌ 导出失败: {result["message"]}')

        except Exception as e:
            self.export_log.push(f'❌ 导出异常: {str(e)}')

        finally:
            # 恢复按钮
            self.export_btn.enable()
            self.export_btn.text = '📦 开始导出'

    def _export_game_thread(self, log_queue=None):
        """在线程池中执行导出"""
        import shutil

        def log(msg):
            """输出日志到队列"""
            if log_queue:
                log_queue.put(msg)

        try:
            project_dir = self.project_manager._get_project_dir(self.current_project.name)
            game_work_dir = project_dir / 'game'
            export_dir = project_dir / 'output'

            # 统计翻译数量
            total_dialogues = len(self.current_project.dialogues)
            translated_dialogues = sum(1 for d in self.current_project.dialogues if d.get('is_translated', False))
            total_strings = len(self.current_project.ui_texts)
            translated_strings = sum(1 for u in self.current_project.ui_texts if u.get('is_translated', False))

            log(f'📊 对话翻译: {translated_dialogues}/{total_dialogues}')
            log(f'📊 字符串翻译: {translated_strings}/{total_strings}')

            # 清理旧的输出目录
            if export_dir.exists():
                log('⏳ 清理旧的输出目录...')
                shutil.rmtree(export_dir)

            # 从工作目录复制到输出目录
            log('⏳ 复制游戏文件...')
            shutil.copytree(game_work_dir, export_dir)
            log('✅ 游戏文件复制完成')

            # 填充对话翻译
            log('📝 填充对话翻译...')
            dialogue_count = self._fill_dialogue_translations(export_dir, log)

            # 填充字符串翻译
            log('📝 填充字符串翻译...')
            string_count = self._fill_string_translations(export_dir, log)

            # 添加语言选择界面
            log('📝 添加语言选择界面...')
            self._add_language_selector(export_dir, log)

            # 添加中文字体支持
            log('📝 添加中文字体支持...')
            self._add_chinese_font_support(export_dir, log)

            # 配置中文字体
            log('📝 配置中文字体...')
            self._configure_chinese_font(export_dir, log)

            log('')
            log('========== 导出完成 ==========')
            log(f'📁 导出目录: {export_dir}')
            log(f'💬 对话翻译: {dialogue_count} 条')
            log(f'📝 字符串翻译: {string_count} 条')

            # 发送完成信号
            if log_queue:
                log_queue.put('__DONE__')

            return {
                'success': True,
                'message': f'导出目录: {export_dir}'
            }

        except Exception as e:
            log(f'❌ 导出异常: {str(e)}')
            if log_queue:
                log_queue.put('__DONE__')
            return {
                'success': False,
                'message': str(e)
            }

    def _fill_dialogue_translations(self, export_dir, log) -> int:
        """填充对话翻译到翻译文件"""
        import re

        # 构建翻译字典: 原文 -> 翻译
        translation_dict = {}
        for d in self.current_project.dialogues:
            if d.get('is_translated', False) and d.get('translated_text'):
                original = d.get('original_text', '')
                if original:
                    translation_dict[original] = d.get('translated_text', '')

        # 查找翻译目录
        tl_dir = export_dir / "game" / "tl" / "chinese"
        if not tl_dir.exists():
            log('⚠️ 翻译目录不存在')
            return 0

        filled_count = 0

        # 遍历翻译文件
        for tl_file in tl_dir.rglob('*.rpy'):
            try:
                with open(tl_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # 替换对话格式: # "原文" \n "原文" -> # "原文" \n "译文"
                lines = content.split('\n')
                new_lines = []
                i = 0
                while i < len(lines):
                    line = lines[i]

                    # 检查是否是注释行（包含对话）
                    comment_match = re.match(r'^\s+#\s+(.*)', line)
                    if comment_match:
                        comment_text = comment_match.group(1).strip()
                        new_lines.append(line)

                        # 检查下一行是否是内容行
                        if i + 1 < len(lines):
                            content_match = re.match(r'^\s+"(.*)"', lines[i + 1])
                            if content_match:
                                content_text = content_match.group(1).replace('\\"', '"')
                                # 查找翻译
                                if content_text in translation_dict:
                                    translated = translation_dict[content_text]
                                    escaped = translated.replace('"', '\\"')
                                    new_lines.append(f'    "{escaped}"')
                                    filled_count += 1
                                else:
                                    new_lines.append(lines[i + 1])
                                i += 2
                                continue

                    new_lines.append(line)
                    i += 1

                # 写回文件
                with open(tl_file, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(new_lines))

            except Exception as e:
                log(f'❌ 填充失败 {tl_file.name}: {e}')

        return filled_count

    def _fill_string_translations(self, export_dir, log) -> int:
        """填充字符串翻译到翻译文件"""
        import re

        # 构建翻译字典: 原文 -> 翻译
        translation_dict = {}
        for u in self.current_project.ui_texts:
            if u.get('is_translated', False) and u.get('translated_text'):
                original = u.get('original_text', '')
                if original:
                    translation_dict[original] = u.get('translated_text', '')

        # 查找翻译目录
        tl_dir = export_dir / "game" / "tl" / "chinese"
        if not tl_dir.exists():
            log('⚠️ 翻译目录不存在')
            return 0

        filled_count = 0

        # 遍历翻译文件
        for tl_file in tl_dir.rglob('*.rpy'):
            try:
                with open(tl_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # 替换 old/new 格式
                lines = content.split('\n')
                new_lines = []
                i = 0
                while i < len(lines):
                    line = lines[i]

                    # 检查是否是 old 行
                    old_match = re.match(r'^\s+old\s+"(.*)"\s*$', line)
                    if old_match:
                        old_text = old_match.group(1).replace('\\"', '"')
                        new_lines.append(line)

                        # 查找翻译
                        translated = translation_dict.get(old_text)

                        # 检查下一行是否是 new
                        if i + 1 < len(lines):
                            new_line = lines[i + 1]
                            new_match = re.match(r'^\s+new\s+"(.*)"\s*$', new_line)
                            if new_match:
                                if translated:
                                    escaped = translated.replace('"', '\\"')
                                    new_lines.append(f'    new "{escaped}"')
                                    filled_count += 1
                                else:
                                    new_lines.append(new_line)
                                i += 2
                                continue

                    new_lines.append(line)
                    i += 1

                # 写回文件
                with open(tl_file, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(new_lines))

            except Exception as e:
                log(f'❌ 填充失败 {tl_file.name}: {e}')

        return filled_count

    def _add_language_selector(self, export_dir, log):
        """添加语言选择界面（在preferences屏幕中）"""
        from pathlib import Path
        import shutil

        # 从项目的游戏目录或导出目录查找screens.rpy
        project_dir = self.project_manager._get_project_dir(self.current_project.name)
        game_work_dir = project_dir / 'game'

        source_file = None
        possible_sources = [
            export_dir / 'game' / 'scripts' / 'screens.rpy',
            export_dir / 'game' / 'screens.rpy',
            game_work_dir / 'game' / 'scripts' / 'screens.rpy',
            game_work_dir / 'game' / 'screens.rpy',
        ]

        for path in possible_sources:
            if path.exists():
                source_file = path
                break

        if not source_file:
            log('  ❌ 找不到原始screens.rpy，无法添加语言选择')
            return

        log(f'  找到原始screens.rpy: {source_file}')

        # 2. 复制到导出目录（覆盖已有的，确保是原始版本）
        target_file = export_dir / 'game' / 'scripts' / 'screens.rpy'
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
        log(f'  已复制到导出目录: {target_file}')

        # 3. 读取导出目录中的文件
        with open(target_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # 4. 检查是否已有语言选择
        if 'Language("chinese")' in content:
            log('  ✅ 语言选择已存在')
            return

        # 5. 找到 "null height (4 * gui.pref_spacing)" 这一行
        # 这是第一个hbox结束的标志，在它之前插入语言选择
        target_line = '            null height (4 * gui.pref_spacing)'

        if target_line not in content:
            log('  ⚠️ 未找到合适的插入位置')
            return

        # 6. 构建语言选择代码
        language_block = '''            vbox:
                style_prefix "radio"
                label _("Language")
                textbutton "English" action Language(None)
                textbutton "中文" action Language("chinese")

'''

        # 7. 在目标行之前插入语言选择
        content = content.replace(target_line, language_block + target_line)

        # 8. 写入修改后的文件
        with open(target_file, 'w', encoding='utf-8') as f:
            f.write(content)

        log('  ✅ 已在preferences中添加语言选择')

        # 检查是否已有语言选择
        if 'Language("chinese")' in content:
            log('  ✅ 语言选择界面已存在')
            return

        # 在preferences屏幕中添加语言选择
        if 'screen preferences():' in content:
            # 在文件末尾添加语言选择屏幕
            language_screen = """

# 语言选择屏幕（由翻译工具添加）
screen language_selector():
    tag menu
    use preferences
    vbox:
        style_prefix "radio"
        xalign 0.5
        yalign 0.5
        spacing 20
        label _("Language")
        textbutton "English" action Language(None)
        textbutton "中文" action Language("chinese")
"""
            content += language_screen

            # 在preferences屏幕中添加语言按钮
            # 查找 "Game Menu" 或类似的位置
            if 'textbutton _("Preferences")' in content:
                content = content.replace(
                    'textbutton _("Preferences")',
                    'textbutton _("Preferences")\n                    textbutton _("Language") action Show("language_selector")'
                )
                log('  ✅ 已添加语言选择按钮到主菜单')
            elif 'textbutton _("Preferences") action Show("preferences")' in content:
                content = content.replace(
                    'textbutton _("Preferences") action Show("preferences")',
                    'textbutton _("Preferences") action Show("preferences")\n                    textbutton _("Language") action Show("language_selector")'
                )
                log('  ✅ 已添加语言选择按钮到主菜单')
            else:
                log('  ⚠️ 未找到主菜单按钮位置，但已添加语言选择屏幕')

            # 写入修改后的文件
            with open(target_file, 'w', encoding='utf-8') as f:
                f.write(content)
            log('  ✅ 已添加语言选择界面')
        else:
            log('  ⚠️ 未找到preferences屏幕')

    def _add_chinese_font_support(self, export_dir, log):
        """添加中文字体支持"""
        from pathlib import Path
        import shutil

        # 检查是否有中文字体
        fonts_dir = export_dir / 'game' / 'fonts'
        fonts_dir.mkdir(exist_ok=True)

        # 常见中文字体文件名
        chinese_fonts = [
            'NotoSansCJK-Regular.ttc',
            'NotoSansSC-Regular.otf',
            'SourceHanSansCN-Regular.otf',
            'WenQuanYiMicroHei.ttf',
            'msyh.ttc',
            'simsun.ttc',
        ]

        # 检查是否已有中文字体
        existing_fonts = []
        for font in chinese_fonts:
            if (fonts_dir / font).exists():
                existing_fonts.append(font)

        if existing_fonts:
            log(f'  ✅ 已有中文字体: {", ".join(existing_fonts)}')
            return

        # 从项目内置字体目录复制
        project_dir = Path(__file__).parent.parent
        builtin_fonts_dir = project_dir / 'fonts'

        if builtin_fonts_dir.exists():
            for font_file in builtin_fonts_dir.iterdir():
                if font_file.suffix.lower() in ['.ttf', '.ttc', '.otf']:
                    try:
                        shutil.copy2(font_file, fonts_dir / font_file.name)
                        log(f'  ✅ 已复制内置字体: {font_file.name}')
                        return
                    except Exception:
                        continue

        log('  ⚠️ 未找到内置字体，请将中文字体放入 fonts/ 目录')

    def _configure_chinese_font(self, export_dir, log):
        """配置gui.rpy使用中文字体"""
        import re

        # 在导出目录中查找gui.rpy
        possible_paths = [
            export_dir / 'game' / 'scripts' / 'gui.rpy',
            export_dir / 'game' / 'gui.rpy',
        ]

        gui_file = None
        for path in possible_paths:
            if path.exists():
                gui_file = path
                break

        if not gui_file:
            log('  ⚠️ 找不到gui.rpy，跳过字体配置')
            return

        log(f'  找到gui.rpy: {gui_file}')

        # 读取文件
        with open(gui_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # 查找导出目录中的字体文件
        fonts_dir = export_dir / 'game' / 'fonts'
        font_name = None
        if fonts_dir.exists():
            for f in fonts_dir.iterdir():
                if f.suffix.lower() in ['.ttf', '.ttc', '.otf']:
                    font_name = f.name
                    break

        if not font_name:
            log('  ⚠️ 未找到字体文件，跳过字体配置')
            return

        # 检查是否已配置该字体
        if f'fonts/{font_name}' in content:
            log(f'  ✅ 字体已配置: {font_name}')
            return

        # 替换字体配置
        font_path = f'fonts/{font_name}'
        content = re.sub(
            r'(define gui\.text_font\s*=\s*)"[^"]*"',
            f'\\1"{font_path}"',
            content
        )
        content = re.sub(
            r'(define gui\.name_text_font\s*=\s*)"[^"]*"',
            f'\\1"{font_path}"',
            content
        )
        content = re.sub(
            r'(define gui\.interface_text_font\s*=\s*)"[^"]*"',
            f'\\1"{font_path}"',
            content
        )

        log(f'  配置字体: {font_path}')

        # 6. 写入修改后的文件
        with open(gui_file, 'w', encoding='utf-8') as f:
            f.write(content)

        log('  ✅ 已配置中文字体')

    def _add_ui_translations(self, export_dir, log):
        """添加字符串翻译"""
        from pathlib import Path

        # 字符串翻译字典
        ui_translations = {
            "Start": "开始",
            "Load": "读取",
            "Save": "保存",
            "Preferences": "设置",
            "About": "关于",
            "Help": "帮助",
            "Quit": "退出",
            "Main Menu": "主菜单",
            "Return": "返回",
            "Yes": "是",
            "No": "否",
            "OK": "确定",
            "Cancel": "取消",
            "Back": "返回",
            "Window": "窗口",
            "Fullscreen": "全屏",
            "Display": "显示",
            "Text Speed": "文字速度",
            "Auto-Forward Time": "自动前进时间",
            "Music Volume": "音乐音量",
            "Sound Volume": "音效音量",
            "Voice Volume": "语音音量",
            "Language": "语言",
            "Save Slot": "存档位",
            "Auto": "自动",
            "Quick": "快速",
            "History": "历史",
            "Skip": "跳过",
            "Unseen Text": "未读文本",
            "After Choices": "选项后",
            "Transitions": "转场效果",
            "Rollback Side": "回滚方向",
            "Disable": "禁用",
            "Left": "左",
            "Right": "右",
            "Mute All": "全部静音",
            "Save Page": "存档页",
            "Load Page": "读取页",
            "Save your game?": "保存游戏？",
            "Load your game?": "读取游戏？",
            "Are you sure?": "确定吗？",
            "Deleting a save slot cannot be undone.": "删除存档无法撤销。",
            "No saves found.": "未找到存档。",
            "File page": "文件页",
            "Previous": "上一页",
            "Next": "下一页",
        }

        # 创建翻译文件
        tl_file = export_dir / 'game' / 'tl' / 'chinese' / 'common.rpy'
        tl_file.parent.mkdir(parents=True, exist_ok=True)

        # 生成翻译内容
        content = []
        content.append('# 字符串翻译\n')
        content.append('# Generated by Ren\'Py Translator\n\n')
        content.append('translate chinese strings:\n')

        for en, cn in ui_translations.items():
            content.append(f'    old "{en}"\n')
            content.append(f'    new "{cn}"\n\n')

        # 写入文件
        with open(tl_file, 'w', encoding='utf-8') as f:
            f.writelines(content)

        log(f'  ✅ 已添加字符串翻译 ({len(ui_translations)}条)')

    # ========== 人物分析 ==========

    def _refresh_analysis(self):
        """刷新人物分析表格"""
        if not self.current_project:
            return

        # 获取角色列表（使用字典的键去重）
        char_dict = self.current_project.char_dict
        dialogues = self.current_project.dialogues
        char_profiles = char_dict.get('__profiles__', {})

        # 获取变量名映射: 变量名 -> 显示名
        variable_map = char_dict.get('__variable_map__', {})

        # 构建变量名到台词数的统计
        var_line_counts = {}
        for d in dialogues:
            var = d.get('character', '')
            if var:
                var_line_counts[var] = var_line_counts.get(var, 0) + 1

        # 从字典获取显示名，排除特殊键
        unique_names = [k for k in char_dict.keys() if not k.startswith('__')]

        rows = []
        for display_name in unique_names:
            if not display_name:
                continue

            # 查找该显示名对应的变量名
            var_name = None
            for var, display in variable_map.items():
                if display == display_name:
                    var_name = var
                    break

            # 统计台词数（通过变量名查找）
            lines_count = var_line_counts.get(var_name, 0) if var_name else 0

            # 检查分析状态
            status = '未分析'
            if display_name in char_profiles:
                status = '已完成'

            rows.append({
                'name': display_name,
                'lines_count': lines_count,
                'status': status,
                'action': display_name
            })

        self.analysis_table.rows = rows
        analyzed = sum(1 for r in rows if r['status'] == '已完成')
        self.analysis_stats.text = f'📊 共 {len(rows)} 个角色，已分析 {analyzed} 个'

    def _on_analyze_character(self, e):
        """分析单个角色"""
        row = e.args
        if not row or not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        name = row['name']
        asyncio.create_task(self._async_analyze_character(name))

    def _on_view_character(self, e):
        """查看角色分析结果"""
        row = e.args
        if not row:
            return

        name = row['name']
        char_profiles = self.current_project.char_dict.get('__profiles__', {})

        if name not in char_profiles:
            ui.notify('该角色尚未分析', type='warning')
            return

        profile = char_profiles[name]

        # 创建对话框显示角色特征
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-2xl'):
            ui.label(f'👤 {name} - 人物特征').classes('text-h6')
            ui.separator()

            # 显示各个维度
            for key, value in profile.items():
                if value:
                    ui.label(f'【{key}】').classes('text-subtitle2 text-primary')
                    ui.label(value).classes('text-body2 pl-4 mb-2')

            ui.button('关闭', on_click=dialog.close).classes('mt-4')

        dialog.open()

    def _analyze_all_characters(self):
        """分析所有角色"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return
        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        asyncio.create_task(self._async_analyze_all_characters())

    async def _async_analyze_all_characters(self):
        """异步分析所有角色"""
        characters = self.current_project.characters
        total = len(characters)
        success = 0

        self.analyze_all_btn.disable()
        self.analyze_all_btn.text = '分析中...'

        with self.analysis_log:
            for i, char in enumerate(characters):
                name = char.get('name', '')
                if not name:
                    continue

                # 跳过已分析的
                char_profiles = self.current_project.char_dict.get('__profiles__', {})
                if name in char_profiles:
                    print(f'⏭️ {name} 已分析，跳过')
                    success += 1
                    continue

                print(f'🤖 [{i+1}/{total}] 正在分析 {name}...')
                success_flag, result_or_error = await self._do_analyze_character(name)
                if success_flag:
                    success += 1
                    print(f'✅ {name} 分析完成')
                else:
                    print(f'❌ {name} 分析失败: {result_or_error}')

                self._refresh_analysis()

        self.analyze_all_btn.enable()
        self.analyze_all_btn.text = '🤖 分析所有角色'
        print(f'分析完成！成功: {success}/{total}')

    async def _async_analyze_character(self, name):
        """异步分析单个角色"""
        self.analyze_btn.disable()

        with self.analysis_log:
            print(f'🤖 正在分析 {name}...')
            success_flag, result_or_error = await self._do_analyze_character(name)
            if success_flag:
                print(f'✅ {name} 分析完成')
            else:
                print(f'❌ {name} 分析失败: {result_or_error}')

            self._refresh_analysis()

        self.analyze_btn.enable()

    async def _do_analyze_character(self, name):
        """执行角色分析，返回 (success, result_or_error)"""
        loop = asyncio.get_event_loop()

        try:
            # name 是显示名（如 "Mika"），需要查找对应的变量名（如 "mk"）
            dialogues = self.current_project.dialogues
            variable_map = self.current_project.char_dict.get('__variable_map__', {})

            # 查找该显示名对应的变量名
            var_name = None
            for var, display in variable_map.items():
                if display == name:
                    var_name = var
                    break

            # 获取该角色的所有台词（通过变量名匹配）
            if var_name:
                char_lines = [d['original_text'] for d in dialogues
                             if d.get('character', '') == var_name]
            else:
                char_lines = []

            if not char_lines:
                return False, f'该角色没有台词 (变量名: {var_name or "未找到"})'

            # 获取模型配置
            max_context = 8  # 默认8K
            if self.current_project.model_config_name:
                config = self.config_manager.get_config_by_name(self.current_project.model_config_name)
                if config:
                    max_context = getattr(config, 'max_context', 8)

            # 计算每批处理的台词数（大约每条台词50 token）
            tokens_per_line = 50
            available_tokens = (max_context * 1024) - 2000  # 预留2000给系统提示
            batch_size = max(10, available_tokens // tokens_per_line // 3)  # 分3批处理

            # 分批处理台词
            batches = [char_lines[i:i+batch_size] for i in range(0, len(char_lines), batch_size)]

            # 如果只有一批，直接分析
            if len(batches) == 1:
                profile = await loop.run_in_executor(
                    self.executor,
                    lambda: self._analyze_single_batch(name, batches[0], is_final=True)
                )
                if profile is None:
                    return False, 'AI分析返回空结果'
            else:
                # 多批处理：先分批总结，再合并
                summaries = []
                for i, batch in enumerate(batches):
                    summary = await loop.run_in_executor(
                        self.executor,
                        lambda b=batch, idx=i: self._analyze_single_batch(
                            name, b, is_final=False, batch_num=idx+1
                        )
                    )
                    if summary:
                        summaries.append(summary)
                    else:
                        return False, f'第{i+1}批分析失败'

                # 合并总结
                if summaries:
                    profile = await loop.run_in_executor(
                        self.executor,
                        lambda: self._merge_summaries(name, summaries)
                    )
                else:
                    return False, '所有批次分析都失败'

            # 保存分析结果
            if profile and isinstance(profile, dict):
                if '__profiles__' not in self.current_project.char_dict:
                    self.current_project.char_dict['__profiles__'] = {}
                self.current_project.char_dict['__profiles__'][name] = profile
                self._save_project(show_notify=False)
                return True, profile

            if not profile:
                return False, 'AI返回的结果为空，可能是提示词被误解为翻译任务'
            return False, f'解析结果失败，期望dict，实际返回: {type(profile)}'

        except Exception as e:
            import traceback
            error_msg = f'{str(e)}\n{traceback.format_exc()}'
            print(f'分析失败: {error_msg}')
            return False, str(e)

    def _parse_profile_text(self, text):
        """解析人物特征文本为字典"""
        profile = {}
        current_key = None
        current_value = []

        print(f'[解析] 原始文本:\n{text[:500]}...')  # 调试日志

        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue

            # 检查是否是新的维度
            found_key = False
            for key in ['性格特点', '外貌特征', '说话风格', '行为习惯', '人物关系', '背景故事', '角色定位', '翻译建议']:
                if line.startswith(key + '：') or line.startswith(key + ':'):
                    if current_key:
                        profile[current_key] = '\n'.join(current_value).strip()
                    current_key = key
                    value_part = line.split('：', 1)[-1].split(':', 1)[-1].strip()
                    current_value = [value_part] if value_part else []
                    found_key = True
                    print(f'[解析] 找到维度: {key}, 值: {value_part}')  # 调试日志
                    break

            if not found_key and current_key:
                current_value.append(line)

        # 保存最后一个维度
        if current_key:
            profile[current_key] = '\n'.join(current_value).strip()

        print(f'[解析] 最终结果: {profile}')  # 调试日志
        return profile

    def _analyze_single_batch(self, name, lines, is_final=False, batch_num=0):
        """分析单批台词"""
        lines_text = '\n'.join([f'"{line}"' for line in lines[:50]])  # 最多50条，加引号强调是台词

        if is_final:
            prompt = f"""【任务类型：文本分析，不是翻译】

我需要你分析以下游戏角色 "{name}" 的台词，总结该角色的人物特征。

以下是该角色的台词（请勿翻译，只需分析）：
{lines_text}

请严格按照以下格式输出分析结果（每个维度一段话，没有信息的维度写"未知"）：

性格特点：（该角色的性格是什么）
外貌特征：（从台词推断的外貌信息）
说话风格：（该角色说话有什么特点）
行为习惯：（该角色的行为模式）
人物关系：（该角色与其他角色的关系）
背景故事：（从台词推断的背景）
角色定位：（该角色在故事中的作用）
翻译建议：（翻译该角色台词时应注意什么）"""
        else:
            prompt = f"""【任务类型：文本分析，不是翻译】

我需要你分析游戏角色 "{name}" 的以下台词（第{batch_num}批），总结该角色的特点。

台词（请勿翻译，只需分析）：
{lines_text}

请简要总结该角色在这批台词中展现的特点（性格、说话风格、关系等）。"""

        try:
            result = self.translator.analyze_text(prompt=prompt)

            # 如果是最终分析，解析为字典
            if is_final:
                return self._parse_profile_text(result)
            else:
                return result  # 返回原始文本用于后续合并

        except Exception as e:
            print(f'❌ 角色分析失败: {e}')
            print(f'📝 提示词:\n{prompt[:500]}...')
            return None

    def _merge_summaries(self, name, summaries):
        """合并多个总结为最终人物特征"""
        summaries_text = '\n\n'.join([f'总结{i+1}：\n{s}' for i, s in enumerate(summaries)])

        prompt = f"""根据以下对角色 "{name}" 的多段分析总结，生成一个完整的人物特征报告。

分段总结：
{summaries_text}

请按以下格式输出完整的人物特征：

性格特点：
外貌特征：
说话风格：
行为习惯：
人物关系：
背景故事：
角色定位：
翻译建议："""

        try:
            result = self.translator.analyze_text(prompt=prompt)
            # 解析结果为字典
            profile = {}
            current_key = None
            current_value = []

            for line in result.split('\n'):
                line = line.strip()
                if not line:
                    continue

                # 检查是否是新的维度
                for key in ['性格特点', '外貌特征', '说话风格', '行为习惯', '人物关系', '背景故事', '角色定位', '翻译建议']:
                    if line.startswith(key + '：') or line.startswith(key + ':'):
                        if current_key:
                            profile[current_key] = '\n'.join(current_value).strip()
                        current_key = key
                        current_value = [line.split('：', 1)[-1].split(':', 1)[-1].strip()]
                        break
                else:
                    if current_key:
                        current_value.append(line)

            if current_key:
                profile[current_key] = '\n'.join(current_value).strip()

            return profile

        except Exception as e:
            print(f'❌ 合并总结失败: {e}')
            print(f'📝 提示词:\n{prompt[:500]}...')
            return {}

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
            context_lines=int(self.config_context.value),
            max_context=int(self.config_max_context.value or 8)
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
        self.config_max_context.value = getattr(config, 'max_context', 8)

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
            'is_translated': d.is_translated
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
