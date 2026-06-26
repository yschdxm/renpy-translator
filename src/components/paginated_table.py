"""通用分页表格组件

人名/字符串/对话面板共用，数据通过回调从 SQLite 分页查询，
内存中始终只有当前页的 50 条数据。
"""

from typing import Callable, Optional, Any
from nicegui import ui


class PaginatedTable:
    """通用分页表格

    用法：
        table = PaginatedTable(columns=[...], page_size=50)
        table.set_query(lambda page, size, filters: db.get_dialogues_page(page, size, **filters))
        table.build_ui(container)
        table.refresh()
    """

    def __init__(self, columns: list[dict], page_size: int = 50,
                 row_key: str = 'id'):
        self.columns = columns
        self.page_size = page_size
        self.row_key = row_key
        self.current_page = 0
        self._total_count = 0
        self._total_pages = 0

        # 查询回调: (page, page_size, filters_dict) -> (rows: list[dict], total: int)
        self._query_fn: Optional[Callable] = None

        # 当前筛选状态
        self._filter_mode = 'all'
        self._search_text = ''
        self._extra_filters: dict[str, Any] = {}

        # UI 组件
        self.table: Optional[ui.table] = None
        self.page_label: Optional[ui.label] = None
        self.page_input: Optional[ui.number] = None
        self.stats_label: Optional[ui.label] = None
        self.page_size_input: Optional[ui.number] = None

        # 自定义 slot 定义（由面板注入）
        self._custom_slots: list[tuple[str, str]] = []
        # 自定义事件处理器
        self._event_handlers: dict[str, Callable] = {}

    def set_query(self, query_fn: Callable):
        """设置数据查询函数

        query_fn 签名: (page: int, page_size: int, **filters) -> (rows: list[dict], total: int)
        """
        self._query_fn = query_fn

    def add_slot(self, name: str, template: str):
        """添加自定义表格 slot"""
        self._custom_slots.append((name, template))

    def on(self, event_name: str, handler: Callable):
        """注册自定义事件处理器"""
        self._event_handlers[event_name] = handler

    def set_filter(self, filter_mode: str = None, search: str = None, **extra):
        """设置筛选条件"""
        if filter_mode is not None:
            self._filter_mode = filter_mode
        if search is not None:
            self._search_text = search
        self._extra_filters.update(extra)
        self.current_page = 0

    def get_filters(self) -> dict:
        """获取当前筛选条件"""
        filters = {
            'filter_mode': self._filter_mode,
            'search': self._search_text,
        }
        filters.update(self._extra_filters)
        return filters

    def refresh(self):
        """刷新当前页（从 SQLite 查询）"""
        if not self._query_fn or not self.table:
            return

        filters = self.get_filters()
        rows, total = self._query_fn(self.current_page, self.page_size, **filters)

        self._total_count = total
        self._total_pages = max(1, (total + self.page_size - 1) // self.page_size)
        self.current_page = min(self.current_page, self._total_pages - 1)

        self.table.rows = rows
        self.table.update()

        # 更新分页标签
        if self.page_label:
            self.page_label.text = f'第 {self.current_page + 1} / {self._total_pages} 页'
        if self.page_input:
            self.page_input.value = self.current_page + 1
        if self.stats_label:
            translated = sum(1 for r in rows if r.get('is_translated', False))
            self.stats_label.text = f'📊 总计: {total} | 当前页: {len(rows)} 条'

    def goto_page(self, page: int):
        """跳转到指定页"""
        if page < 0:
            page = 0
        if self._total_pages > 0 and page >= self._total_pages:
            page = self._total_pages - 1
        self.current_page = max(0, page)
        self.refresh()

    def prev_page(self):
        """上一页"""
        if self.current_page > 0:
            self.current_page -= 1
            self.refresh()

    def next_page(self):
        """下一页"""
        if self.current_page < self._total_pages - 1:
            self.current_page += 1
            self.refresh()

    def goto_first_page(self):
        """跳转到第一页"""
        self.current_page = 0
        self.refresh()

    def goto_last_page(self):
        """跳转到最后一页"""
        self.current_page = max(0, self._total_pages - 1)
        self.refresh()

    def get_page_items(self) -> list[dict]:
        """获取当前页数据（用于批量翻译）"""
        if not self._query_fn:
            return []
        filters = self.get_filters()
        rows, _ = self._query_fn(self.current_page, self.page_size, **filters)
        return rows

    def get_all_untranslated(self) -> list[dict]:
        """获取所有未翻译的数据（用于翻译全部）"""
        if not self._query_fn:
            return []
        # 一次性查询所有未翻译的
        filters = self.get_filters()
        filters['filter_mode'] = 'untranslated'
        rows, total = self._query_fn(0, total if (total := 999999) else 999999, **filters)
        return rows

    def build_ui(self, container: ui.column,
                 show_search: bool = True,
                 show_filter: bool = True,
                 search_placeholder: str = '搜索原文/译文'):
        """构建 UI"""
        with container:
            # 统计和分页控件
            with ui.row().classes('w-full items-center gap-2'):
                self.stats_label = ui.label('').classes('text-subtitle1')
                ui.space()

                if show_filter:
                    self.filter_select = ui.select(
                        options={'all': '全部', 'untranslated': '未翻译', 'translated': '已翻译'},
                        label='筛选', value='all'
                    ).classes('w-32')
                    self.filter_select.on_value_change(lambda e: self._on_filter_change(e.value))

                self.page_size_input = ui.number(
                    label='每页', value=self.page_size, min=10, max=200
                ).classes('w-24')
                self.page_size_input.on_value_change(
                    lambda e: self._on_page_size_change(int(e.value or 50))
                )

                ui.button('⏮', on_click=self.goto_first_page).props('flat dense')
                ui.button('◀', on_click=self.prev_page).props('flat dense')
                self.page_label = ui.label('第 1 页')
                ui.button('▶', on_click=self.next_page).props('flat dense')
                ui.button('⏭', on_click=self.goto_last_page).props('flat dense')
                self.page_input = ui.number(label='跳转', value=1, min=1).classes('w-20')
                ui.button('跳', on_click=lambda: self.goto_page(
                    int(self.page_input.value or 1) - 1
                )).props('flat dense')

            # 搜索栏
            if show_search:
                with ui.row().classes('w-full gap-2'):
                    self.search_input = ui.input(
                        label='搜索', placeholder=search_placeholder
                    ).classes('flex-1')
                    ui.button('🔍', on_click=self._on_search).props('flat dense')

            # 表格
            self.table = ui.table(
                columns=self.columns,
                rows=[],
                row_key=self.row_key
            ).classes('w-full')

            # 注册自定义 slot
            for slot_name, slot_template in self._custom_slots:
                self.table.add_slot(slot_name, slot_template)

            # 注册自定义事件处理器
            for event_name, handler in self._event_handlers.items():
                self.table.on(event_name, handler)

    def _on_filter_change(self, value: str):
        """筛选变化"""
        self._filter_mode = value
        self.current_page = 0
        self.refresh()

    def _on_page_size_change(self, value: int):
        """每页数量变化"""
        self.page_size = max(10, value)
        self.current_page = 0
        self.refresh()

    def _on_search(self):
        """搜索"""
        if hasattr(self, 'search_input'):
            self._search_text = self.search_input.value or ''
        self.current_page = 0
        self.refresh()
