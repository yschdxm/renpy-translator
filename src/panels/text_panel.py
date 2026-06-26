"""通用文本翻译面板 - 字符串和对话共用

通过 content_type 参数区分 'ui' 和 'dialogue'，
单条翻译、翻译本页、翻译全部 共用同一套 TranslationService 逻辑。
"""

import asyncio
from nicegui import ui

from database import ProjectDatabase
from translation_service import TranslationService
from logger import TranslationLogger
from components.paginated_table import PaginatedTable
from components.progress_panel import ProgressPanel
from components.log_panel import LogPanel


class TextTranslationPanel:
    """通用文本翻译面板

    - content_type='ui'  -> 字符串翻译
    - content_type='dialogue' -> 对话翻译
    """

    def __init__(self, content_type: str, title: str,
                 show_character: bool = False,
                 logger: TranslationLogger = None):
        self.content_type = content_type
        self.title = title
        self.show_character = show_character
        self.logger = logger

        self.db: ProjectDatabase = None
        self.translation_service: TranslationService = None

        self.table: PaginatedTable = None
        self.progress: ProgressPanel = None
        self.log_panel: LogPanel = None

        # 按钮
        self.translate_page_btn: ui.button = None
        self.translate_all_btn: ui.button = None
        self.stop_btn: ui.button = None

        # 角色筛选（对话模式）
        self.char_filter: ui.select = None
        self._char_filter_value = ''

    def set_db(self, db: ProjectDatabase):
        """设置数据库引用"""
        self.db = db

    def set_translation_service(self, service: TranslationService):
        """设置翻译服务"""
        self.translation_service = service

    def create(self, container: ui.column):
        """创建面板"""
        with container:
            # 统计和操作栏
            with ui.row().classes('w-full items-center gap-2'):
                self.stats_label = ui.label(f'请先打开项目').classes('text-subtitle1')
                ui.space()
                self.translate_page_btn = ui.button(
                    '🚀 翻译本页', color='primary',
                    on_click=self._translate_page
                )
                self.translate_all_btn = ui.button(
                    '⚡ 全部翻译', color='secondary',
                    on_click=self._translate_all
                )
                self.stop_btn = ui.button(
                    '⏹ 停止', color='red',
                    on_click=self._stop_translation
                )
                self.stop_btn.set_visibility(False)
                ui.button('🔄 刷新', on_click=self.refresh).props('flat dense')

            # 角色筛选（对话模式）
            if self.show_character:
                with ui.row().classes('w-full gap-2'):
                    self.char_filter = ui.select(
                        options=['全部'], label='角色', value='全部'
                    ).classes('w-48')
                    self.char_filter.on_value_change(self._on_char_filter_change)

            # 表格
            columns = [
                {'name': 'index', 'label': '#', 'field': 'index', 'sortable': True,
                 'style': 'width: 50px'},
                {'name': 'original', 'label': '原文', 'field': 'original',
                 'style': 'white-space: normal; max-width: 400px'},
                {'name': 'translated', 'label': '译文', 'field': 'translated',
                 'style': 'white-space: normal; max-width: 400px'},
                {'name': 'status', 'label': '状态', 'field': 'status',
                 'style': 'width: 80px'},
                {'name': 'action', 'label': '操作', 'field': 'action',
                 'style': 'width: 100px'},
            ]

            if self.show_character:
                columns.insert(1, {
                    'name': 'character', 'label': '角色', 'field': 'character',
                    'sortable': True, 'style': 'width: 80px'
                })

            self.table = PaginatedTable(columns=columns, page_size=50, row_key='index')

            # 自定义单元格
            self.table.add_slot('body-cell-translated', '''
                <q-td :props="props">
                    <q-input v-model="props.row.translated" dense type="textarea" autogrow
                        @change="$parent.$emit('update:text', props.row)" />
                </q-td>
            ''')
            self.table.add_slot('body-cell-status', '''
                <q-td :props="props">
                    <q-chip :color="props.row.status === '翻译中' ? 'orange' : (props.row.status === '完成' ? 'green' : 'grey')"
                        text-color="white" dense size="sm">
                        {{ props.row.status }}
                    </q-chip>
                </q-td>
            ''')

            if self.show_character:
                self.table.add_slot('body-cell-action', '''
                    <q-td :props="props">
                        <q-btn flat dense color="primary" label="AI翻译"
                            :disable="props.row.status === '翻译中'"
                            @click="$parent.$emit('translate_item', props.row)" />
                        <q-btn flat dense color="secondary" label="上下文"
                            @click="$parent.$emit('show_context', props.row)" />
                    </q-td>
                ''')
            else:
                self.table.add_slot('body-cell-action', '''
                    <q-td :props="props">
                        <q-btn flat dense color="primary" label="AI翻译"
                            :disable="props.row.status === '翻译中'"
                            @click="$parent.$emit('translate_item', props.row)" />
                    </q-td>
                ''')

            # 注册事件
            self.table.on('update:text', self._on_text_update)
            self.table.on('translate_item', self._on_translate_item)
            if self.show_character:
                self.table.on('show_context', self._on_show_context)

            # 构建 UI
            self.table.build_ui(container)

            # 进度和日志
            self.progress = ProgressPanel()
            self.progress.build_ui(container)

            self.log_panel = LogPanel(height='h-32')
            self.log_panel.build_ui(container, label=f'{self.title}日志')

            if self.logger:
                self.logger.bind_ui(self.content_type, self.log_panel.get_push_callback())

    def refresh(self):
        """同步刷新（仅在同步上下文中使用）"""
        if not self.db:
            return
        self.table.set_query(self._query_items)
        self.table.refresh()
        if self.content_type == 'ui':
            counts = self.db.get_ui_text_count()
        else:
            counts = self.db.get_dialogue_count()
        self.stats_label.text = f'📊 总计: {counts["total"]} | ✅ 已翻译: {counts["translated"]}'
        if self.show_character and self.char_filter:
            characters = self.db.get_dialogue_characters()
            variable_map = self.db.get_variable_map()
            options = ['全部'] + [variable_map.get(c, c) for c in characters]
            self.char_filter.set_options(options)

    async def async_refresh(self):
        """异步刷新（非阻塞）"""
        if not self.db:
            return
        loop = asyncio.get_event_loop()

        if self.content_type == 'ui':
            counts = await loop.run_in_executor(None, self.db.get_ui_text_count)
        else:
            counts = await loop.run_in_executor(None, self.db.get_dialogue_count)

        self.table.set_query(self._query_items)
        self.table.refresh()
        self.stats_label.text = f'📊 总计: {counts["total"]} | ✅ 已翻译: {counts["translated"]}'

        if self.show_character and self.char_filter:
            characters = await loop.run_in_executor(None, self.db.get_dialogue_characters)
            variable_map = await loop.run_in_executor(None, self.db.get_variable_map)
            options = ['全部'] + [variable_map.get(c, c) for c in characters]
            self.char_filter.set_options(options)

    def _query_items(self, page: int, page_size: int, **filters) -> tuple[list, int]:
        """从 SQLite 分页查询"""
        filter_mode = filters.get('filter_mode', 'all')
        search = filters.get('search', '')

        if self.content_type == 'ui':
            items, total = self.db.get_ui_texts_page(page, page_size, filter_mode, search)
        else:
            character = filters.get('character', '')
            items, total = self.db.get_dialogues_page(
                page, page_size, filter_mode, character, search
            )

        rows = []
        for d in items:
            row = {
                'index': d['id'],
                'original': d.get('original_text', ''),
                'translated': d.get('translated_text', ''),
                'status': '完成' if d.get('is_translated') else '待翻译',
                'action': d['id'],
            }
            if self.show_character:
                row['character'] = d.get('character', '') or '旁白'
            rows.append(row)

        return rows, total

    def _on_text_update(self, e):
        """译文输入框更新 -> 立即写入 SQLite"""
        row = e.args
        if row and self.db:
            item_id = row['action']
            if self.content_type == 'ui':
                self.db.update_ui_text(item_id, row['translated'])
            else:
                self.db.update_dialogue(item_id, row['translated'])

    async def _on_translate_item(self, e):
        """AI翻译单条"""
        row = e.args
        if not self.translation_service:
            ui.notify('请先配置翻译器', type='warning')
            return

        if row:
            item_id = row['action']
            await self._do_translate_single(item_id)

    async def _do_translate_single(self, item_id: int):
        """翻译单条"""
        loop = asyncio.get_event_loop()

        if self.content_type == 'ui':
            item = await loop.run_in_executor(None, self.db.get_ui_text, item_id)
        else:
            item = await loop.run_in_executor(None, self.db.get_dialogue, item_id)

        if not item:
            return

        ok = await self.translation_service.translate_single(
            item_id=item_id,
            content_type=self.content_type,
            original_text=item['original_text'],
            character=item.get('character', ''),
            context_before=item.get('context_before'),
            context_after=item.get('context_after'),
        )

        await self.async_refresh()

    async def _translate_page(self):
        """翻译当前页"""
        if not self.translation_service:
            ui.notify('请先配置翻译器', type='warning')
            return

        items = self.table.get_page_items()
        to_translate = [item for item in items if item.get('status') != '完成']

        if not to_translate:
            ui.notify('当前页已全部翻译', type='info')
            return

        loop = asyncio.get_event_loop()

        # DB 查询在线程池中
        def _load_items():
            db_items = []
            for row in to_translate:
                if self.content_type == 'ui':
                    item = self.db.get_ui_text(row['action'])
                else:
                    item = self.db.get_dialogue(row['action'])
                if item:
                    db_items.append(item)
            return db_items

        db_items = await loop.run_in_executor(None, _load_items)

        self._set_buttons_translating(True)

        def on_progress(current, total, status):
            self.progress.update(current, total, status)

        result = await self.translation_service.translate_batch(
            content_type=self.content_type,
            items=db_items,
            progress_callback=on_progress
        )

        self._set_buttons_translating(False)
        self.progress.reset()
        await self.async_refresh()

        if result['stopped']:
            ui.notify(f'翻译已停止: 成功 {result["success"]}', type='warning')
        else:
            ui.notify(f'翻译完成: 成功 {result["success"]}', type='positive')

    async def _translate_all(self):
        """翻译全部未翻译"""
        if not self.translation_service:
            ui.notify('请先配置翻译器', type='warning')
            return

        # 检查前置条件（对话翻译时检查人名和分析）
        if self.content_type == 'dialogue':
            loop = asyncio.get_event_loop()
            ok, msg = await loop.run_in_executor(None, self._check_prerequisites)
            if not ok:
                ui.notify(msg, type='warning')
                return

        self._set_buttons_translating(True)

        def on_progress(current, total, status):
            self.progress.update(current, total, status)

        result = await self.translation_service.translate_batch(
            content_type=self.content_type,
            items=None,
            progress_callback=on_progress
        )

        self._set_buttons_translating(False)
        self.progress.reset()
        await self.async_refresh()

        if result['stopped']:
            ui.notify(f'翻译已停止: 成功 {result["success"]}', type='warning')
        else:
            ui.notify(f'翻译完成: 成功 {result["success"]}, 失败 {result["failed"]}', type='positive')

    def _check_prerequisites(self) -> tuple[bool, str]:
        """检查对话翻译的前置条件（同步，在线程池中调用）"""
        if not self.db:
            return False, '请先打开项目'

        name_counts = self.db.get_char_dict_count()
        if name_counts['untranslated'] > 0:
            return False, f'请先完成人名翻译（还有 {name_counts["untranslated"]} 个未翻译）'

        profiles = self.db.get_all_profiles()
        characters = self.db.get_characters()
        unanalyzed = [c['name'] for c in characters if c.get('name') and c['name'] not in profiles]
        if unanalyzed:
            return False, f'请先完成人物分析（还有 {len(unanalyzed)} 个未分析）'

        return True, ''

    async def _stop_translation(self):
        """停止翻译"""
        if self.translation_service:
            await self.translation_service.stop()

    def _set_buttons_translating(self, translating: bool):
        """切换按钮状态"""
        self.translate_page_btn.set_visibility(not translating)
        self.translate_all_btn.set_visibility(not translating)
        self.stop_btn.set_visibility(translating)

    async def _on_char_filter_change(self, e):
        """角色筛选变化（对话模式，DB 查询在线程池中）"""
        if self.show_character:
            if e.value == '全部':
                self._char_filter_value = ''
            else:
                loop = asyncio.get_event_loop()
                variable_map = await loop.run_in_executor(None, self.db.get_variable_map)
                reverse_map = {v: k for k, v in variable_map.items()}
                self._char_filter_value = reverse_map.get(e.value, e.value)

            self.table.set_filter(character=self._char_filter_value)
            self.table.current_page = 0
            self.table.refresh()

    async def _on_show_context(self, e):
        """显示上下文对话框（DB 查询在线程池中）"""
        row = e.args
        if not row or not self.db:
            return

        item_id = row['action']
        loop = asyncio.get_event_loop()

        def _load_context():
            item = self.db.get_dialogue(item_id)
            if not item:
                return None, [], []
            ctx_before, ctx_after = self.db.get_dialogue_neighbors(item_id, 5)
            return item, ctx_before, ctx_after

        item, context_before, context_after = await loop.run_in_executor(None, _load_context)
        if not item:
            return

        current_char = item.get('character', '') or '旁白'
        current_text = item.get('original_text', '')

        with ui.dialog() as dialog, ui.card().classes('w-full max-w-3xl'):
            ui.label(f'📖 上下文（前后各5句）').classes('text-h6')

            if context_before:
                ui.label('前文:').classes('text-subtitle2 text-grey')
                for line in context_before:
                    ui.label(f'  {line}').classes('text-body2')

            ui.separator()
            ui.label(f'>>> 【{current_char}】{current_text} <<<').classes(
                'text-body1 text-primary font-bold')

            if context_after:
                ui.separator()
                ui.label('后文:').classes('text-subtitle2 text-grey')
                for line in context_after:
                    ui.label(f'  {line}').classes('text-body2')

            ui.button('关闭', on_click=dialog.close).classes('mt-4')

        dialog.props('persistent')
        dialog.open()
