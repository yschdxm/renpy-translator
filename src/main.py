"""Ren'Py Translator - NiceGUI 主入口

模块化架构，所有翻译逻辑通过 TranslationService 统一调度，
数据存储使用 SQLite（单条翻译后立即写入，毫秒级）。
"""

import asyncio
import os
import sys

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nicegui import ui

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


class App:
    """Ren'Py Translator 主应用"""

    def __init__(self):
        # 管理器
        self.project_manager = ProjectManager()
        self.config_manager = ConfigManager()
        self.parser = RenpyParser()
        self.sdk_manager = SDKManager()
        self.logger = TranslationLogger()

        # 当前状态
        self.db: ProjectDatabase = None
        self.translator: AITranslator = None
        self.translation_service: TranslationService = None

        # 面板
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

        # UI 组件
        self.header_project_select: ui.select = None
        self.header_progress: ui.label = None
        self._panels: dict = {}
        self._active_panel = 'projects'

    def create(self):
        """创建 UI"""
        self._active_panel = 'projects'

        ui.add_head_html('<style>.q-splitter__separator{background:#e0e0e0!important;width:1px!important;}</style>')

        # ========== 顶部栏 ==========
        with ui.header().classes('items-center').style('background: #1a1a2e;'):
            ui.label("🎮 Ren'Py Translator").classes('text-h6 text-white q-ml-md').style('font-weight: 600;')
            ui.separator().props('vertical').classes('q-mx-sm')
            self.header_project_select = ui.select(
                options={}, label='切换项目', value=None
            ).classes('w-48').props('dark dense outlined hide-details')
            async def _on_select(e):
                if e.value and isinstance(e.value, str):
                    await self._open_project(e.value)
            self.header_project_select.on_value_change(_on_select)

            ui.space()
            self.header_progress = ui.label('').classes('text-caption text-grey-5')

        # ========== 主体 ==========
        with ui.column().classes('w-full').style('height: calc(100vh - 50px); padding: 0;'):
            with ui.splitter(horizontal=False).classes('w-full h-full') as splitter:
                # 左侧导航
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

                # 右侧内容
                with splitter.after:
                    splitter.props(':model-value=15')
                    self._panels = {}
                    with ui.column().classes('full-width').style('overflow-y: auto; height: 100%;'):
                        with ui.card().classes('w-full flat bordered').style('display: block;') as panel:
                            self.project_panel.create(panel)
                        self._panels['projects'] = panel

                        with ui.card().classes('w-full flat bordered').style('display: none;') as panel:
                            self.name_panel.create(panel)
                        self._panels['names'] = panel

                        with ui.card().classes('w-full flat bordered').style('display: none;') as panel:
                            self.strings_panel.create(panel)
                        self._panels['strings'] = panel

                        with ui.card().classes('w-full flat bordered').style('display: none;') as panel:
                            self.dialogue_panel.create(panel)
                        self._panels['dialogue'] = panel

                        with ui.card().classes('w-full flat bordered').style('display: none;') as panel:
                            self.export_panel.create(panel)
                        self._panels['export'] = panel

                        with ui.card().classes('w-full flat bordered').style('display: none;') as panel:
                            self.config_panel.create(panel)
                        self._panels['config'] = panel

        # 初始刷新
        self._refresh_header_options()
        self.config_panel.refresh()

    def _switch_panel(self, panel_key: str):
        """切换面板"""
        for key, panel in self._panels.items():
            panel.style('display: block;' if key == panel_key else 'display: none;')
        self._active_panel = panel_key

    def _refresh_header_options(self):
        """刷新顶栏项目下拉"""
        if not self.header_project_select:
            return
        projects = self.project_manager.list_projects()
        options = {p.name: p.name for p in projects}
        self.header_project_select.options = options
        self.header_project_select.update()

    async def _open_project(self, name: str):
        """打开项目（所有阻塞操作在线程池中执行）"""
        import asyncio as _aio
        loop = _aio.get_event_loop()

        # 关闭旧数据库
        if self.db:
            await loop.run_in_executor(None, self.db.close)

        # 打开新数据库（sqlite3.connect 在线程池）
        db = await loop.run_in_executor(None, self.project_manager.open_project, name)
        if not db:
            ui.notify('项目不存在', type='negative')
            return

        self.db = db

        # 读取配置（sqlite3 查询在线程池）
        model_name = await loop.run_in_executor(None, db.get_meta, 'model_config_name')
        if model_name:
            self.translator = await loop.run_in_executor(
                None, self.config_panel.create_translator, model_name
            )

        # 初始化翻译服务
        if self.translator:
            self.translation_service = TranslationService(
                translator=self.translator,
                db=self.db,
                logger=self.logger,
                max_concurrent=5
            )
        else:
            self.translation_service = None

        # 设置各面板的数据库和服务引用
        self.name_panel.set_db(self.db)
        self.name_panel.set_translation_service(self.translation_service)
        self.name_panel.set_translator(self.translator)

        # 传递模型上下文大小，用于动态计算分段
        if model_name:
            model_config = self.config_manager.get_config_by_name(model_name)
            if model_config:
                self.name_panel.set_max_context(getattr(model_config, 'max_context', 8))

        self.strings_panel.set_db(self.db)
        self.strings_panel.set_translation_service(self.translation_service)

        self.dialogue_panel.set_db(self.db)
        self.dialogue_panel.set_translation_service(self.translation_service)

        self.export_panel.set_db(self.db)

        # 刷新所有面板
        await self.name_panel.async_refresh()
        await self.strings_panel.async_refresh()
        await self.dialogue_panel.async_refresh()
        await self.export_panel.async_refresh()

        # 更新顶栏
        self._refresh_header_options()
        self.header_project_select.value = name

        d_count = await loop.run_in_executor(None, self.db.get_dialogue_count)
        self.header_progress.text = f'进度: {d_count["translated"]}/{d_count["total"]}'

        # 切换到人名面板
        self._switch_panel('names')
        await self.project_panel.async_refresh_projects()

        ui.notify(f'已打开项目: {name}', type='positive')


def create_app():
    """创建应用"""
    app = App()
    app.create()
    return app


# NiceGUI 页面
@ui.page('/')
def index():
    create_app()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="Ren'Py Translator",
        port=7980,
        reload=False,
        favicon='🎮',
    )
