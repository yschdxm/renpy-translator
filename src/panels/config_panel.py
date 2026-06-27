"""模型配置面板"""

from pathlib import Path
from nicegui import ui


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (RuntimeError, AttributeError):
        return None

from config_manager import ConfigManager, ModelConfig
from translator import AITranslator, TranslationConfig
from sdk_manager import SDKManager
from logger import TranslationLogger


class ConfigPanel:
    """模型配置面板"""

    def __init__(self, config_manager: ConfigManager,
                 sdk_manager: SDKManager,
                 logger: TranslationLogger):
        self.config_manager = config_manager
        self.sdk_manager = sdk_manager
        self.logger = logger

        # 配置表单组件
        self.name_input: ui.input = None
        self.api_base: ui.input = None
        self.api_key: ui.input = None
        self.model: ui.input = None
        self.temp_slider: ui.slider = None
        self.temp_label: ui.label = None
        self.max_tokens: ui.number = None
        self.context_lines: ui.number = None
        self.max_context: ui.number = None

        # SDK 组件
        self.sdk_path_input: ui.input = None
        self.sdk_status: ui.label = None

        # 配置列表
        self.config_list: ui.list = None

        # 当前翻译器（回调给主应用）
        self.on_translator_changed: callable = None

    def create(self, container: ui.column):
        """创建面板"""
        with container:
            # 配置表单
            with ui.card().classes('w-full'):
                ui.label('编辑配置').classes('text-h6')
                self.name_input = ui.input(label='配置名称', placeholder='GPT-4')
                self.api_base = ui.input(label='API地址', value='https://api.openai.com/v1')
                self.api_key = ui.input(label='API Key', password=True)
                self.model = ui.input(label='模型名称', placeholder='gpt-4')

                with ui.row().classes('gap-2 items-center w-full'):
                    ui.label('Temperature:')
                    self.temp_slider = ui.slider(min=0, max=2, value=0.3, step=0.1).classes('flex-1')
                    self.temp_label = ui.label('0.3')

                self.temp_slider.on_value_change(lambda e: self.temp_label.set_text(str(e.value)))

                with ui.row().classes('gap-2'):
                    self.max_tokens = ui.number(label='最大输出Token数', value=4096, min=1)
                    self.context_lines = ui.number(label='翻译上下文行数', value=3, min=0)
                    self.max_context = ui.number(label='模型最大上下文(K)', value=8, min=1)

                with ui.row().classes('gap-2'):
                    ui.button('💾 保存配置', color='primary', on_click=self._save_config)
                    ui.button('🔄 清空表单', on_click=self._clear_form)

            ui.separator()

            # SDK 配置
            with ui.card().classes('w-full'):
                ui.label("Ren'Py SDK 配置").classes('text-h6')
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
            self.refresh()

    def refresh(self):
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
                        ui.button(icon='edit',
                                  on_click=lambda n=c.name: self._edit_config(n)).props('flat dense')
                        ui.button(icon='delete', color='red',
                                  on_click=lambda n=c.name: self._delete_config(n)).props('flat dense')

    def _save_config(self):
        """保存配置"""
        name = self.name_input.value
        if not name:
            _safe(ui.notify,'请输入配置名称', type='warning')
            return

        config = ModelConfig(
            name=name,
            api_base=self.api_base.value,
            api_key=self.api_key.value,
            model=self.model.value,
            temperature=float(self.temp_slider.value),
            max_tokens=int(self.max_tokens.value),
            context_lines=int(self.context_lines.value),
            max_context=int(self.max_context.value or 8)
        )

        existing = self.config_manager.get_config_by_name(name)
        if existing:
            self.config_manager.update_config(name, config)
            _safe(ui.notify,f'配置已更新: {name}', type='positive')
        else:
            self.config_manager.add_config(config)
            _safe(ui.notify,f'配置已保存: {name}', type='positive')

        self.refresh()

    def _edit_config(self, name: str):
        """编辑配置"""
        config = self.config_manager.get_config_by_name(name)
        if not config:
            _safe(ui.notify,'配置不存在', type='negative')
            return

        self.name_input.value = config.name
        self.api_base.value = config.api_base
        self.api_key.value = config.api_key
        self.model.value = config.model
        self.temp_slider.value = config.temperature
        self.max_tokens.value = config.max_tokens
        self.context_lines.value = config.context_lines
        self.max_context.value = getattr(config, 'max_context', 8)

        _safe(ui.notify,f'已加载配置: {name}', type='info')

    def _delete_config(self, name: str):
        """删除配置"""
        with ui.dialog() as dialog:
            with ui.card():
                ui.label(f'确定要删除配置 "{name}" 吗？')
                with ui.row().classes('gap-2'):
                    ui.button('取消', on_click=dialog.close)
                    ui.button('删除', color='red', on_click=lambda: (
                        self.config_manager.delete_config(name),
                        self.refresh(),
                        _safe(ui.notify,f'已删除配置: {name}', type='positive'),
                        dialog.close()
                    ))
        dialog.props('persistent')
        dialog.open()

    def _clear_form(self):
        """清空表单"""
        self.name_input.value = ''
        self.api_base.value = 'https://api.openai.com/v1'
        self.api_key.value = ''
        self.model.value = ''
        self.temp_slider.value = 0.3
        self.max_tokens.value = 4096
        self.context_lines.value = 3

    def _find_sdk_path(self) -> str:
        """查找 SDK 路径"""
        configs = self.config_manager.load_all_configs()
        for c in configs:
            if hasattr(c, 'sdk_path') and c.sdk_path:
                return c.sdk_path

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
            self.sdk_status.text = "❌ 未找到 Ren'Py SDK"
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

    def get_sdk_path(self) -> str:
        """获取当前 SDK 路径"""
        if hasattr(self, 'sdk_path_input') and self.sdk_path_input:
            return self.sdk_path_input.value
        return ''

    def create_translator(self, config_name: str) -> AITranslator:
        """根据配置名创建翻译器"""
        config = self.config_manager.get_config_by_name(config_name)
        if not config:
            return None

        trans_config = TranslationConfig(
            api_base=config.api_base,
            api_key=config.api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            context_lines=config.context_lines
        )
        return AITranslator(trans_config)
