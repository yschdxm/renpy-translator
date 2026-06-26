"""翻译进度和队列状态显示组件"""

from nicegui import ui


class ProgressPanel:
    """翻译进度和队列状态显示

    用法：
        progress = ProgressPanel()
        progress.build_ui(container)
        progress.update(current=50, total=100, status="翻译中...")
        progress.reset()
    """

    def __init__(self):
        self.status_label: ui.label = None
        self.progress_bar: ui.linear_progress = None

    def build_ui(self, container, show_progress_bar: bool = True):
        """构建 UI"""
        with container:
            with ui.row().classes('w-full items-center gap-2'):
                self.status_label = ui.label('就绪').classes('text-caption')
                if show_progress_bar:
                    self.progress_bar = ui.linear_progress(
                        value=0, show_value=False
                    ).classes('flex-1')

    def update(self, current: int = 0, total: int = 0, status: str = ""):
        """更新进度

        Args:
            current: 当前完成数
            total: 总数
            status: 状态文本
        """
        if status:
            self.status_label.text = status
        elif total > 0:
            self.status_label.text = f'进度: {current}/{total}'

        if self.progress_bar and total > 0:
            self.progress_bar.value = current / total

    def set_indeterminate(self, status: str = "处理中..."):
        """设置为不确定进度状态"""
        self.status_label.text = status
        if self.progress_bar:
            self.progress_bar.value = 0
            self.progress_bar.props('indeterminate')

    def reset(self):
        """重置"""
        self.status_label.text = '就绪'
        if self.progress_bar:
            self.progress_bar.value = 0
            self.progress_bar.props(remove='indeterminate')

    def show_success(self, message: str):
        """显示成功状态"""
        self.status_label.text = f'✅ {message}'
        if self.progress_bar:
            self.progress_bar.value = 1.0
            self.progress_bar.props(remove='indeterminate')

    def show_error(self, message: str):
        """显示错误状态"""
        self.status_label.text = f'❌ {message}'
        if self.progress_bar:
            self.progress_bar.props(remove='indeterminate')
