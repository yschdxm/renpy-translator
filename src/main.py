"""Ren'Py Translator - NiceGUI 主入口

模块化架构，所有翻译逻辑通过 TranslationService 统一调度，
数据存储使用 SQLite（单条翻译后立即写入，毫秒级）。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nicegui import ui, app

from project_manager import ProjectManager
from config_manager import ConfigManager
from translator import AITranslator, TranslationConfig
from renpy_parser import RenpyParser
from sdk_manager import SDKManager
from logger import TranslationLogger
from database import ProjectDatabase
from translation_service import TranslationService

from panels.project_panel import ProjectPanel
from panels.name_panel import NamePanel
from panels.text_panel import TextTranslationPanel
from panels.export_panel import ExportPanel
from panels.config_panel import ConfigPanel


def safe_ui(fn, *args, **kwargs):
    """安全执行 UI 操作，客户端断开时跳过

    用法：
        safe_ui(setattr, self.label, 'text', '...')
        safe_ui(ui.notify, '完成', type='positive')
        safe_ui(self.table.refresh)
    """
    try:
        return fn(*args, **kwargs)
    except (RuntimeError, AttributeError):
        return None


class App:
    """Ren'Py Translator 主应用"""

    def __init__(self):
        self.project_manager = ProjectManager()
        self.config_manager = ConfigManager()
        self.parser = RenpyParser()
        self.sdk_manager = SDKManager()
        self.logger = TranslationLogger()

        self.db: ProjectDatabase = None
        self.translator: AITranslator = None
        self.translation_service: TranslationService = None

        self.project_panel = ProjectPanel(
            project_manager=self.project_manager,
            logger=self.logger,
            on_project_open=self._open_project,
            get_sdk_path=lambda: self.config_panel.get_sdk_path() if hasattr(self, 'config_panel') else '',
            get_model_names=lambda: self.config_manager.get_config_names(),
        )
        self.name_panel = NamePanel(logger=self.logger)
        self.strings_panel = TextTranslationPanel(
            content_type='ui', title='字符串翻译', logger=self.logger
        )
        self.dialogue_panel = TextTranslationPanel(
            content_type='dialogue', title='对话翻译',
            show_character=True, logger=self.logger
        )
        self.export_panel = ExportPanel(
            project_manager=self.project_manager, logger=self.logger
        )
        self.config_panel = ConfigPanel(
            config_manager=self.config_manager,
            sdk_manager=self.sdk_manager,
            logger=self.logger
        )

        self.header_project_select: ui.select = None
        self.header_progress: ui.label = None
        self._panels: dict = {}
        self._active_panel = 'projects'
        self._ui_created = False
        # 任务运行状态（跨页面重建保留，每个面板独立）
        self._task_states: dict = {'name': False, 'ui': False, 'dialogue': False}

    def create(self):
        """创建 UI（每个客户端独立的 UI 元素）"""
        self._active_panel = 'projects'

        ui.add_head_html('<style>.q-splitter__separator{background:#e0e0e0!important;width:1px!important;}</style>')

        with ui.header().classes('items-center').style('background: #1a1a2e;'):
            ui.label("🎮 Ren'Py Translator").classes('text-h6 text-white q-ml-md').style('font-weight: 600;')
            ui.separator().props('vertical').classes('q-mx-sm')
            header_select = ui.select(
                options={}, label='切换项目', value=None
            ).classes('w-48').props('dark dense outlined hide-details')

            async def _on_select(e):
                if e.value and isinstance(e.value, str):
                    await self._open_project(e.value, switch_to_names=False)
            header_select.on_value_change(_on_select)

            ui.space()
            header_progress = ui.label('').classes('text-caption text-grey-5')

        # 本客户端的面板引用
        panels = {}

        with ui.column().classes('w-full').style('height: calc(100vh - 50px); padding: 0;'):
            with ui.splitter(horizontal=False).classes('w-full h-full') as splitter:
                with splitter.before:
                    with ui.column().classes('full-width').style('overflow-x: hidden; overflow-y: auto;'):
                        with ui.list().classes('full-width'):
                            nav_items = [
                                ('projects', 'folder', '项目管理'),
                                ('names', 'person', '人名翻译'),
                                ('strings', 'translate', '字符串翻译'),
                                ('dialogue', 'chat', '对话翻译'),
                                ('export', 'folder_zip', '导出游戏'),
                                ('config', 'settings', '模型配置'),
                            ]
                            for key, icon, label in nav_items:
                                with ui.item(on_click=lambda k=key: self._switch_panel(k)).classes('q-py-xs'):
                                    with ui.item_section().props('avatar'):
                                        ui.icon(icon).props('size=sm')
                                    with ui.item_section():
                                        ui.item_label(label).classes('text-body2')

                        ui.separator()
                        with ui.expansion('📜 当前提示词', value=False).classes('full-width'):
                            prompt_log = ui.log().classes('w-full').style('max-height: 40vh; font-size: 11px;')
                            ui.button('清空', icon='delete', on_click=lambda: safe_ui(prompt_log.clear)).props('flat dense size=sm')

                with splitter.after:
                    splitter.props(':model-value=15')
                    with ui.column().classes('full-width').style('overflow-y: auto; height: 100%;'):
                        with ui.card().classes('w-full flat bordered').style('display: block;') as panel:
                            self.project_panel.create(panel)
                        panels['projects'] = panel

                        with ui.card().classes('w-full flat bordered').style('display: none;') as panel:
                            self.name_panel.create(panel)
                        panels['names'] = panel

                        with ui.card().classes('w-full flat bordered').style('display: none;') as panel:
                            self.strings_panel.create(panel)
                        panels['strings'] = panel

                        with ui.card().classes('w-full flat bordered').style('display: none;') as panel:
                            self.dialogue_panel.create(panel)
                        panels['dialogue'] = panel

                        with ui.card().classes('w-full flat bordered').style('display: none;') as panel:
                            self.export_panel.create(panel)
                        panels['export'] = panel

                        with ui.card().classes('w-full flat bordered').style('display: none;') as panel:
                            self.config_panel.create(panel)
                        panels['config'] = panel

        # 存储本客户端的引用
        self._panels = panels
        self.header_project_select = header_select
        self.header_progress = header_progress
        self.prompt_display = prompt_log

        self._refresh_header_options()
        self.config_panel.refresh()

        # 页面重建时恢复之前打开的项目
        saved_project = app.storage.user.get('current_project')
        if saved_project and not self.db:
            asyncio.create_task(self._restore_project(saved_project))

    async def _restore_project(self, name: str):
        """恢复之前打开的项目"""
        if self.project_manager.project_exists(name):
            await self._open_project(name)

    def _switch_panel(self, panel_key: str):
        """切换面板（供 _open_project 等内部方法调用）"""
        for key, panel in self._panels.items():
            safe_ui(panel.style, 'display: block;' if key == panel_key else 'display: none;')
        self._active_panel = panel_key

    def _refresh_header_options(self):
        if not self.header_project_select:
            return
        projects = self.project_manager.list_projects()
        options = {p.name: p.name for p in projects}
        self.header_project_select.options = options
        safe_ui(self.header_project_select.update)

    async def _open_project(self, name: str, switch_to_names: bool = True):
        loop = asyncio.get_event_loop()

        if self.db:
            await loop.run_in_executor(None, self.db.close)

        db = await loop.run_in_executor(None, self.project_manager.open_project, name)
        if not db:
            safe_ui(ui.notify, '项目不存在', type='negative')
            return

        self.db = db

        # 持久化当前项目
        app.storage.user['current_project'] = name

        model_name = await loop.run_in_executor(None, db.get_meta, 'model_config_name')
        if model_name:
            self.translator = await loop.run_in_executor(
                None, self.config_panel.create_translator, model_name
            )

        if self.translator and hasattr(self, 'prompt_display'):
            self.translator.prompt_callback = self._on_prompt

        # 获取模型配置
        max_context_k = 8
        max_tokens = 1000
        if model_name:
            model_config = self.config_manager.get_config_by_name(model_name)
            if model_config:
                max_context_k = getattr(model_config, 'max_context', 8)
                max_tokens = getattr(model_config, 'max_tokens', 1000)

        if self.translator:
            self.translation_service = TranslationService(
                translator=self.translator,
                db=self.db,
                logger=self.logger,
                max_context_k=max_context_k,
                max_tokens=max_tokens,
            )
        else:
            self.translation_service = None

        self.name_panel.set_db(self.db)
        self.name_panel.set_translation_service(self.translation_service)
        self.name_panel.set_translator(self.translator)
        self.name_panel.set_max_context(max_context_k)

        self.strings_panel.set_db(self.db)
        self.strings_panel.set_translation_service(self.translation_service)
        self.strings_panel._on_task_state_change = lambda running: self.set_task_running(running, 'ui')

        self.dialogue_panel.set_db(self.db)
        self.dialogue_panel.set_translation_service(self.translation_service)
        self.dialogue_panel._on_task_state_change = lambda running: self.set_task_running(running, 'dialogue')

        # name_panel 的回调在 __init__ 后设置（因为 _translate_all 直接管理按钮）
        self.name_panel._on_task_state_change = lambda running: self.set_task_running(running, 'name')

        self.export_panel.set_db(self.db)

        await self.name_panel.async_refresh()
        await self.strings_panel.async_refresh()
        await self.dialogue_panel.async_refresh()
        await self.export_panel.async_refresh()

        self._refresh_header_options()
        safe_ui(setattr, self.header_project_select, 'value', name)

        d_count = await loop.run_in_executor(None, self.db.get_dialogue_count)
        safe_ui(setattr, self.header_progress, 'text', f'进度: {d_count["translated"]}/{d_count["total"]}')

        if switch_to_names:
            self._switch_panel('names')
        await self.project_panel.async_refresh_projects()

        # 恢复按钮状态（如果后台任务仍在运行）
        self._restore_task_state()

        safe_ui(ui.notify, f'已打开项目: {name}', type='positive')

    def set_task_running(self, running: bool, task_type: str):
        """设置指定面板的任务运行状态"""
        self._task_states[task_type] = running

    def _restore_task_state(self):
        """恢复各面板按钮状态（页面重建后调用）"""
        if self._task_states.get('name'):
            safe_ui(self.name_panel.translate_all_btn.set_visibility, False)
            safe_ui(self.name_panel.stop_btn.set_visibility, True)
        if self._task_states.get('ui'):
            self.strings_panel._set_buttons_translating(True)
        if self._task_states.get('dialogue'):
            self.dialogue_panel._set_buttons_translating(True)

    def _on_prompt(self, system_prompt: str, user_prompt: str, task_type: str):
        if not hasattr(self, 'prompt_display') or not self.prompt_display:
            return
        type_labels = {'name': '人名翻译', 'ui': '字符串翻译', 'dialogue': '对话翻译', 'analysis': '分析'}
        label = type_labels.get(task_type, task_type)
        safe_ui(self.prompt_display.clear)
        safe_ui(self.prompt_display.push, f'───── {label} ─────')
        safe_ui(self.prompt_display.push, f'[系统]\n{system_prompt}')
        safe_ui(self.prompt_display.push, f'[用户]\n{user_prompt}')


_app_instance = None


def create_app():
    global _app_instance
    if _app_instance is None:
        _app_instance = App()
    _app_instance.create()
    return _app_instance


@ui.page('/')
def index():
    create_app()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="Ren'Py Translator",
        port=7980,
        reload=False,
        favicon='🎮',
        storage_secret='renpy-translator-secret-key',
    )
