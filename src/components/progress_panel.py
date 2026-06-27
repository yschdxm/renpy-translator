"""翻译进度和队列状态显示组件"""

from nicegui import ui


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (RuntimeError, AttributeError):
        return None


class ProgressPanel:
    """翻译进度和队列状态显示"""

    def __init__(self):
        self.status_label: ui.label = None
        self.progress_bar: ui.linear_progress = None

    def build_ui(self, container, show_progress_bar: bool = True):
        with container:
            with ui.row().classes('w-full items-center gap-2'):
                self.status_label = ui.label('就绪').classes('text-caption')
                if show_progress_bar:
                    self.progress_bar = ui.linear_progress(
                        value=0, show_value=False
                    ).classes('flex-1')

    def update(self, current: int = 0, total: int = 0, status: str = ""):
        if status:
            _safe(setattr, self.status_label, 'text', status)
        elif total > 0:
            _safe(setattr, self.status_label, 'text', f'进度: {current}/{total}')

        if self.progress_bar and total > 0:
            _safe(setattr, self.progress_bar, 'value', current / total)

    def set_indeterminate(self, status: str = "处理中..."):
        _safe(setattr, self.status_label, 'text', status)
        if self.progress_bar:
            _safe(setattr, self.progress_bar, 'value', 0)
            _safe(self.progress_bar.props, 'indeterminate')

    def reset(self):
        _safe(setattr, self.status_label, 'text', '就绪')
        if self.progress_bar:
            _safe(setattr, self.progress_bar, 'value', 0)
            _safe(self.progress_bar.props, remove='indeterminate')

    def show_success(self, message: str):
        _safe(setattr, self.status_label, 'text', f'✅ {message}')
        if self.progress_bar:
            _safe(setattr, self.progress_bar, 'value', 1.0)
            _safe(self.progress_bar.props, remove='indeterminate')

    def show_error(self, message: str):
        _safe(setattr, self.status_label, 'text', f'❌ {message}')
        if self.progress_bar:
            _safe(self.progress_bar.props, remove='indeterminate')
