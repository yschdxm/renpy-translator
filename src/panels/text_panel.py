"""通用文本翻译面板 - 字符串和对话共用

翻译逻辑完全照搬人名面板，只改内容类型和查询方法。
"""

import asyncio
from nicegui import ui


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (RuntimeError, AttributeError):
        return None

from database import ProjectDatabase
from translation_service import TranslationService
from logger import TranslationLogger
from components.paginated_table import PaginatedTable
from components.progress_panel import ProgressPanel
from components.log_panel import LogPanel


class TextTranslationPanel:
    """通用文本翻译面板 - 字符串和对话共用"""

    def __init__(self, content_type: str, title: str,
                 show_character: bool = False,
                 logger: TranslationLogger = None):
        self.content_type = content_type
        self.title = title
        self.show_character = show_character
        self.logger = logger
        self._on_task_state_change: callable = None
        self._processing_ids: set = set()

        self.db: ProjectDatabase = None
        self.translation_service: TranslationService = None

        self.table: PaginatedTable = None
        self.progress: ProgressPanel = None
        self.log_panel: LogPanel = None

        self.translate_page_btn: ui.button = None
        self.translate_all_btn: ui.button = None
        self.stop_btn: ui.button = None
        self._cancel = False

        # 角色筛选
        self.char_filter: ui.select = None
        self._char_filter_value = ''

    def set_db(self, db: ProjectDatabase):
        self.db = db

    def set_translation_service(self, service: TranslationService):
        self.translation_service = service

    def create(self, container: ui.column):
        with container:
            with ui.row().classes('w-full items-center gap-2'):
                self.stats_label = ui.label('请先打开项目').classes('text-subtitle1')
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
                    on_click=self._stop
                )
                _safe(self.stop_btn.set_visibility, False)
                ui.button('🔄 刷新', on_click=self.async_refresh).props('flat dense')

            # 角色筛选（对话模式）
            if self.show_character:
                with ui.row().classes('w-full gap-2'):
                    self.char_filter = ui.select(
                        options=['全部'], label='角色', value='全部'
                    ).classes('w-48')
                    self.char_filter.on_value_change(self._on_char_filter_change)

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
                            @click="$parent.$emit('translate_item', props.row)" />
                        <q-btn flat dense color="secondary" label="上下文"
                            @click="$parent.$emit('show_context', props.row)" />
                    </q-td>
                ''')
            else:
                self.table.add_slot('body-cell-action', '''
                    <q-td :props="props">
                        <q-btn flat dense color="primary" label="AI翻译"
                            @click="$parent.$emit('translate_item', props.row)" />
                    </q-td>
                ''')

            self.table.on('update:text', self._on_text_update)
            self.table.on('translate_item', self._on_translate_item)
            if self.show_character:
                self.table.on('show_context', self._on_show_context)

            self.table.build_ui(container)

            self.progress = ProgressPanel()
            self.progress.build_ui(container)

            self.log_panel = LogPanel(height='h-32')
            self.log_panel.build_ui(container, label=f'{self.title}日志')

            if self.logger:
                self.logger.bind_ui(self.content_type, self.log_panel.get_push_callback())

    # ========== 刷新（和人名面板一致） ==========

    def refresh(self):
        if not self.db:
            return
        self.table.set_query(self._query_items)
        self.table.refresh()
        self._update_stats()

    async def async_refresh(self):
        if not self.db:
            return
        loop = asyncio.get_event_loop()
        counts = await loop.run_in_executor(None, self._get_counts)
        self.table.set_query(self._query_items)
        _safe(self.table.refresh)
        _safe(setattr, self.stats_label, 'text', f'📊 总计: {counts["total"]} | ✅ 已翻译: {counts["translated"]}')

        if self.show_character and self.char_filter:
            characters = await loop.run_in_executor(None, self.db.get_dialogue_characters)
            variable_map = await loop.run_in_executor(None, self.db.get_variable_map)
            options = ['全部'] + [variable_map.get(c, c) for c in characters]
            self.char_filter.set_options(options)

    def _get_counts(self):
        if self.content_type == 'ui':
            return self.db.get_ui_text_count()
        return self.db.get_dialogue_count()

    def _update_stats(self):
        if not self.db:
            return
        counts = self._get_counts()
        _safe(setattr, self.stats_label, 'text', f'📊 总计: {counts["total"]} | ✅ 已翻译: {counts["translated"]}')

    # ========== 查询（和人名面板的 _query_names 一致的结构） ==========

    def _query_items(self, page: int, page_size: int, **filters):
        filter_mode = filters.get('filter_mode', 'all')
        search = filters.get('search', '')

        if self.content_type == 'ui':
            items, total = self.db.get_ui_texts_page(page, page_size, filter_mode, search)
        else:
            character = filters.get('character', '')
            items, total = self.db.get_dialogues_page(page, page_size, filter_mode, character, search)

        rows = []
        for d in items:
            is_processing = d['id'] in self._processing_ids
            if is_processing:
                status = '翻译中'
            elif d.get('is_translated'):
                status = '完成'
            else:
                status = '待翻译'

            row = {
                'index': d['id'],
                'original': d.get('original_text', ''),
                'translated': d.get('translated_text', ''),
                'status': status,
                'action': d['id'],
            }
            if self.show_character:
                row['character'] = d.get('character', '') or '旁白'
            rows.append(row)

        return rows, total

    # ========== 事件处理 ==========

    def _on_text_update(self, e):
        row = e.args
        if row and self.db:
            item_id = row['action']
            if self.content_type == 'ui':
                self.db.update_ui_text(item_id, row['translated'])
            else:
                self.db.update_dialogue(item_id, row['translated'])

    async def _on_translate_item(self, e):
        row = e.args
        if not self.translation_service:
            _safe(ui.notify, '请先配置翻译器', type='warning')
            return
        if row:
            await self._do_translate_single(row['action'])

    async def _do_translate_single(self, item_id: int):
        """翻译单条（和人名面板的 _do_translate_and_analyze 结构一致）"""
        # 获取原文
        if self.content_type == 'ui':
            item = self.db.get_ui_text(item_id)
        else:
            item = self.db.get_dialogue(item_id)

        if not item:
            return

        # 标记处理中
        self._processing_ids.add(item_id)
        _safe(self.table.refresh)

        try:
            ok = await self.translation_service.translate_single(
                item_id=item_id,
                content_type=self.content_type,
                original_text=item['original_text'],
                character=item.get('character', ''),
            )

            self._processing_ids.discard(item_id)
            await self.async_refresh()

        except Exception as e:
            self.logger.error(f'翻译失败: {e}', panel=self.content_type)
            self._processing_ids.discard(item_id)
            await self.async_refresh()

    # ========== 批量翻译（和人名面板的 _translate_all 结构一致） ==========

    async def _translate_page(self):
        """翻译当前页"""
        if not self.translation_service:
            _safe(ui.notify, '请先配置翻译器', type='warning')
            return

        items = self.table.get_page_items()
        to_translate = [item for item in items if item.get('status') != '完成']

        if not to_translate:
            _safe(ui.notify, '当前页已全部翻译', type='info')
            return

        self._cancel = False
        _safe(self.translate_page_btn.set_visibility, False)
        _safe(self.stop_btn.set_visibility, True)
        if self._on_task_state_change:
            self._on_task_state_change(True)

        total = len(to_translate)
        self.logger.info(f'开始翻译当前页 {total} 条 ({self.content_type})', panel=self.content_type)

        success = 0
        for i, row in enumerate(to_translate):
            if self._cancel:
                break

            self.progress.update(i, total, f'翻译中: {i+1}/{total}')

            # 标记处理中
            self._processing_ids.add(row['action'])
            _safe(self.table.refresh)

            try:
                ok = await self.translation_service.translate_single(
                    item_id=row['action'],
                    content_type=self.content_type,
                    original_text=row.get('original', ''),
                    character=row.get('character', ''),
                )
                if ok:
                    success += 1
            except Exception as e:
                self.logger.error(f'翻译失败: {e}', panel=self.content_type)

            self._processing_ids.discard(row['action'])
            await self.async_refresh()

        self._processing_ids.clear()
        _safe(self.translate_page_btn.set_visibility, True)
        _safe(self.stop_btn.set_visibility, False)
        self.progress.reset()
        if self._on_task_state_change:
            self._on_task_state_change(False)
        await self.async_refresh()

        if self._cancel:
            _safe(ui.notify, f'翻译已停止: 成功 {success}/{total}', type='warning')
        else:
            _safe(ui.notify, f'翻译完成: 成功 {success}/{total}', type='positive')

    async def _translate_all(self):
        """翻译全部未翻译（和人名面板的 _translate_all 结构一致）"""
        if not self.translation_service:
            _safe(ui.notify, '请先配置翻译器', type='warning')
            return

        # 检查前置条件（对话翻译时检查人名和分析）
        if self.content_type == 'dialogue':
            loop = asyncio.get_event_loop()
            ok, msg = await loop.run_in_executor(None, self._check_prerequisites)
            if not ok:
                _safe(ui.notify, msg, type='warning')
                return

        self._cancel = False
        _safe(self.translate_all_btn.set_visibility, False)
        _safe(self.stop_btn.set_visibility, True)
        if self._on_task_state_change:
            self._on_task_state_change(True)

        loop = asyncio.get_event_loop()

        # 获取所有未翻译
        if self.content_type == 'ui':
            to_translate = await loop.run_in_executor(None, self.db.get_untranslated_ui_texts)
        else:
            to_translate = await loop.run_in_executor(None, self.db.get_untranslated_dialogues)

        total = len(to_translate)
        if total == 0:
            _safe(ui.notify, '所有内容已翻译', type='info')
            _safe(self.translate_all_btn.set_visibility, True)
            _safe(self.stop_btn.set_visibility, False)
            self.progress.reset()
            if self._on_task_state_change:
                self._on_task_state_change(False)
            return

        self.logger.info(f'开始翻译 {total} 条 ({self.content_type})', panel=self.content_type)

        success = 0
        for i, item in enumerate(to_translate):
            if self._cancel:
                break

            self.progress.update(i, total, f'翻译中: {i+1}/{total}')

            # 标记处理中
            self._processing_ids.add(item['id'])
            _safe(self.table.refresh)

            try:
                ok = await self.translation_service.translate_single(
                    item_id=item['id'],
                    content_type=self.content_type,
                    original_text=item.get('original_text', ''),
                    character=item.get('character', ''),
                )
                if ok:
                    success += 1
            except Exception as e:
                self.logger.error(f'翻译失败: {e}', panel=self.content_type)

            self._processing_ids.discard(item['id'])
            await self.async_refresh()

        self._processing_ids.clear()
        _safe(self.translate_all_btn.set_visibility, True)
        _safe(self.stop_btn.set_visibility, False)
        self.progress.reset()
        if self._on_task_state_change:
            self._on_task_state_change(False)
        await self.async_refresh()

        if self._cancel:
            _safe(ui.notify, f'翻译已停止: 成功 {success}/{total}', type='warning')
        else:
            _safe(ui.notify, f'翻译完成: 成功 {success}/{total}', type='positive')

    def _check_prerequisites(self) -> tuple[bool, str]:
        """检查对话翻译的前置条件（同步，在线程池中调用）"""
        if not self.db:
            return False, '请先打开项目'

        name_counts = self.db.get_char_dict_count()
        if name_counts['untranslated'] > 0:
            return False, f'请先完成人名翻译（还有 {name_counts["untranslated"]} 个未翻译）'

        profiles = self.db.get_all_profiles()
        characters = self.db.get_characters()
        unanalyzed = [c['display_name'] for c in characters
                      if c['display_name'] not in profiles and not c['is_placeholder']]
        if unanalyzed:
            return False, f'请先完成人物分析（还有 {len(unanalyzed)} 个未分析）'

        return True, ''

    async def _stop(self):
        self._cancel = True
        if self.translation_service:
            await self.translation_service.stop()

    def _set_buttons_translating(self, translating: bool):
        _safe(self.translate_page_btn.set_visibility, not translating)
        _safe(self.translate_all_btn.set_visibility, not translating)
        _safe(self.stop_btn.set_visibility, translating)

    # ========== 角色筛选（对话模式） ==========

    async def _on_char_filter_change(self, e):
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

    # ========== 上下文查看（对话模式） ==========

    async def _on_show_context(self, e):
        row = e.args
        if not row or not self.db:
            return

        item_id = row['action']
        loop = asyncio.get_event_loop()

        def _load_context():
            item = self.db.get_dialogue(item_id)
            if not item:
                return None, [], []
            ctx_before, ctx_after = self.db.get_dialogue_context(item_id, 'dialogue', 5)
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
                for ctx in context_before:
                    char = ctx.get('character', '') or '旁白'
                    orig = ctx.get('original_text', '')
                    trans = ctx.get('translated_text', '')
                    if trans:
                        ui.label(f'  [已译] {char}: "{orig}" → "{trans}"').classes('text-body2 text-positive')
                    else:
                        ui.label(f'  {char}: "{orig}"').classes('text-body2')

            ui.separator()
            ui.label(f'>>> 【{current_char}】{current_text} <<<').classes(
                'text-body1 text-primary font-bold')

            if context_after:
                ui.separator()
                ui.label('后文:').classes('text-subtitle2 text-grey')
                for ctx in context_after:
                    char = ctx.get('character', '') or '旁白'
                    orig = ctx.get('original_text', '')
                    trans = ctx.get('translated_text', '')
                    if trans:
                        ui.label(f'  [已译] {char}: "{orig}" → "{trans}"').classes('text-body2 text-positive')
                    else:
                        ui.label(f'  {char}: "{orig}"').classes('text-body2')

            ui.button('关闭', on_click=dialog.close).classes('mt-4')

        dialog.props('persistent')
        dialog.open()
