"""日志显示面板组件"""

from nicegui import ui


class LogPanel:
    """日志显示面板

    用法：
        log_panel = LogPanel()
        log_panel.build_ui(container)
        log_panel.push("INFO", "翻译完成")
        log_panel.clear()
    """

    def __init__(self, height: str = 'h-32', max_lines: int = 500):
        self.height = height
        self.max_lines = max_lines
        self.log_widget: ui.log = None
        self._line_count = 0

    def build_ui(self, container, label: str = '日志'):
        """构建 UI"""
        with container:
            ui.label(label).classes('text-subtitle2')
            self.log_widget = ui.log().classes(f'w-full {self.height}')

    def push(self, message: str):
        """推送到日志

        Args:
            message: 日志消息
        """
        if not self.log_widget:
            return

        self.log_widget.push(message)
        self._line_count += 1

        # 超过最大行数时清理（NiceGUI ui.log 自动管理，此处仅计数）
        if self._line_count > self.max_lines * 2:
            self._line_count = self.max_lines

    def clear(self):
        """清空日志"""
        if self.log_widget:
            self.log_widget.clear()
            self._line_count = 0

    def get_push_callback(self):
        """获取 push 回调函数（用于绑定到 TranslationLogger）"""
        def callback(message: str):
            self.push(message)
        return callback
