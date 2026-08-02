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
from translator import FatalAPIError
from logger import TranslationLogger
from components.paginated_table import PaginatedTable
from components.progress_panel import ProgressPanel


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
        self._project_dir: str = ''

        self.table: PaginatedTable = None
        self.progress: ProgressPanel = None

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

    def set_project_dir(self, project_dir: str):
        """设置项目工作目录（用于回扫源码定位字符串上下文）"""
        self._project_dir = project_dir

    def create(self, container: ui.column):
        with container:
            with ui.row().classes('w-full items-center gap-2'):
                self.stats_label = ui.label('请先打开项目').classes('text-subtitle1')
                ui.space()
                if self.content_type == 'ui':
                    ui.button(
                        '📍 重建上下文', color='accent',
                        on_click=self._rebuild_ui_hints
                    )
                if self.content_type == 'dialogue':
                    ui.button(
                        '🎨 风格指南', color='accent',
                        on_click=self._open_style_guide
                    )
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
        # 表格刷新含同步 SQLite 查询，放入线程池避免阻塞事件循环
        await loop.run_in_executor(None, self.table.refresh)
        _safe(setattr, self.stats_label, 'text', f'📊 总计: {counts["total"]} | ✅ 已翻译: {counts["translated"]}')

        if self.show_character and self.char_filter:
            characters = await loop.run_in_executor(None, self.db.get_dialogue_characters)
            variable_map = await loop.run_in_executor(None, self.db.get_variable_map)
            options = ['全部'] + [variable_map.get(c, c) for c in characters]
            self.char_filter.set_options(options)

    def _update_row_status(self, item_id: int, status: str = None, translated: str = None):
        """实时更新当前页中某一行的状态/译文（不重查整页，不阻塞事件循环）"""
        t = self.table.table if self.table else None
        if not t:
            return
        for row in t.rows:
            if row.get('index') == item_id:
                if status is not None:
                    row['status'] = status
                if translated is not None:
                    row['translated'] = translated
                _safe(t.update)
                break

    def _update_rows(self, status_map: dict, translated_map: dict = None):
        """批量更新当前页中多行的状态/译文（合并为一次 table.update）"""
        t = self.table.table if self.table else None
        if not t:
            return
        changed = False
        for row in t.rows:
            rid = row.get('index')
            if rid in status_map:
                row['status'] = status_map[rid]
                if translated_map and rid in translated_map:
                    row['translated'] = translated_map[rid]
                changed = True
        if changed:
            _safe(t.update)

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

        except FatalAPIError as e:
            self.logger.error(f'API 致命错误: {e}', panel=self.content_type)
            _safe(ui.notify, str(e), type='negative', timeout=10000)
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
        to_translate_rows = [item for item in items if item.get('status') != '完成']

        if not to_translate_rows:
            _safe(ui.notify, '当前页已全部翻译', type='info')
            return

        # 从 db 取完整条目（含 label/context_hint，供场景分隔与出处提示）
        def _get_full(row):
            if self.content_type == 'ui':
                return self.db.get_ui_text(row['action'])
            return self.db.get_dialogue(row['action'])

        # 表格行映射为翻译条目
        to_translate = []
        for row in to_translate_rows:
            char = row.get('character', '') or ''
            if char == '旁白':
                char = ''
            full = _get_full(row) or {}
            to_translate.append({
                'id': row['action'],
                'original_text': row.get('original', ''),
                'character': char,
                'label': full.get('label', ''),
                'context_hint': full.get('context_hint', ''),
            })

        self._cancel = False
        _safe(self.translate_page_btn.set_visibility, False)
        _safe(self.stop_btn.set_visibility, True)
        if self._on_task_state_change:
            self._on_task_state_change(True)

        total = len(to_translate)
        self.logger.info(f'开始翻译当前页 {total} 条 ({self.content_type})', panel=self.content_type)

        success = 0
        try:
            batches = await self.translation_service.prepare_batches(to_translate, self.content_type)
            processed = 0
            for batch in batches:
                if self._cancel:
                    break

                self.progress.update(processed, total, f'翻译中: {processed}/{total}')

                # 批内所有行标记为翻译中（一次表格更新）
                batch_ids = [it['id'] for it in batch]
                self._processing_ids.update(batch_ids)
                self._update_rows({i: '翻译中' for i in batch_ids})

                try:
                    results = await self.translation_service.translate_batch(batch, self.content_type)
                except FatalAPIError as e:
                    # 不可重试的致命错误，中止整个批量任务
                    self.logger.error(f'API 致命错误，批量翻译中止: {e}', panel=self.content_type)
                    _safe(ui.notify, str(e), type='negative', timeout=10000)
                    self._update_rows({i: '待翻译' for i in batch_ids})
                    self._cancel = True
                    break

                # 批完成：回写译文 + 状态（一次表格更新）
                status_map = {i: ('完成' if i in results else '待翻译') for i in batch_ids}
                self._update_rows(status_map, translated_map=results)
                success += len(results)
                processed += len(batch)
                self._processing_ids.difference_update(batch_ids)

        except Exception as e:
            self.logger.error(f'批量翻译异常中断: {e}', panel=self.content_type)

        finally:
            self._processing_ids.clear()
            _safe(self.translate_page_btn.set_visibility, True)
            _safe(self.stop_btn.set_visibility, False)
            self.progress.reset()
            if self._on_task_state_change:
                self._on_task_state_change(False)
            try:
                await self.async_refresh()
            except Exception:
                pass

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
        try:
            # 按句数（≤100）与 token 上限（输入+输出不超窗口）动态分组
            batches = await self.translation_service.prepare_batches(to_translate, self.content_type)
            self.logger.info(f'共 {total} 条，分为 {len(batches)} 个批次', panel=self.content_type)
            processed = 0
            for batch in batches:
                if self._cancel:
                    break

                self.progress.update(processed, total, f'翻译中: {processed}/{total}')

                # 批内所有行标记为翻译中（一次表格更新）
                batch_ids = [it['id'] for it in batch]
                self._processing_ids.update(batch_ids)
                self._update_rows({i: '翻译中' for i in batch_ids})

                try:
                    results = await self.translation_service.translate_batch(batch, self.content_type)
                except FatalAPIError as e:
                    # 不可重试的致命错误，中止整个批量任务
                    self.logger.error(f'API 致命错误，批量翻译中止: {e}', panel=self.content_type)
                    _safe(ui.notify, str(e), type='negative', timeout=10000)
                    self._update_rows({i: '待翻译' for i in batch_ids})
                    self._cancel = True
                    break

                # 批完成：回写译文 + 状态（一次表格更新）
                status_map = {i: ('完成' if i in results else '待翻译') for i in batch_ids}
                self._update_rows(status_map, translated_map=results)
                success += len(results)
                processed += len(batch)
                self._processing_ids.difference_update(batch_ids)

        except Exception as e:
            self.logger.error(f'批量翻译异常中断: {e}', panel=self.content_type)

        finally:
            self._processing_ids.clear()
            _safe(self.translate_all_btn.set_visibility, True)
            _safe(self.stop_btn.set_visibility, False)
            self.progress.reset()
            if self._on_task_state_change:
                self._on_task_state_change(False)
            try:
                await self.async_refresh()
            except Exception:
                pass

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

    # ========== 字符串上下文重建（UI 模式） ==========

    async def _rebuild_ui_hints(self):
        """回扫游戏源码，重建 UI 字符串的出处上下文"""
        if not self.db:
            _safe(ui.notify, '请先打开项目', type='warning')
            return
        if not self._project_dir:
            _safe(ui.notify, '项目目录未知，无法回扫源码', type='negative')
            return

        from renpy_parser import RenpyParser
        loop = asyncio.get_event_loop()

        # ongoing 通知不会自动消失，完成后更新同一个通知对象
        progress_notice = ui.notification('正在回扫源码定位字符串出处...', spinner=True, timeout=None)

        def _locate():
            parser = RenpyParser()
            return parser.locate_ui_string_contexts(self._project_dir)

        try:
            hints = await loop.run_in_executor(None, _locate)
            matched = await loop.run_in_executor(None, self.db.update_ui_hints, hints)

            counts = await loop.run_in_executor(None, self.db.get_ui_text_count)
            total = counts['total']
            _safe(setattr, progress_notice, 'message',
                  f'上下文重建完成: {matched}/{total} 条字符串已定位出处')
            _safe(setattr, progress_notice, 'spinner', False)
            _safe(setattr, progress_notice, 'type', 'positive' if matched else 'warning')
            self.logger.info(f'UI 上下文重建: {matched}/{total} 命中', panel='ui')
            await asyncio.sleep(3)
            _safe(progress_notice.dismiss)
        except Exception as e:
            self.logger.error(f'上下文重建失败: {e}', panel='ui')
            _safe(setattr, progress_notice, 'message', f'重建失败: {e}')
            _safe(setattr, progress_notice, 'spinner', False)
            _safe(setattr, progress_notice, 'type', 'negative')
            await asyncio.sleep(5)
            _safe(progress_notice.dismiss)

    # ========== 风格指南（对话模式） ==========

    async def _open_style_guide(self):
        """打开风格指南对话框（展示/生成/编辑/保存）"""
        if not self.db:
            _safe(ui.notify, '请先打开项目', type='warning')
            return

        loop = asyncio.get_event_loop()
        existing = await loop.run_in_executor(None, self.db.get_meta, 'style_guide')

        with ui.dialog() as dialog, ui.card().classes('w-full max-w-3xl'):
            ui.label('🎨 作品风格指南').classes('text-h6')
            ui.label('注入所有翻译的系统提示词，决定全作翻译的文风基调。'
                     '可点击「生成」由 AI 分析台词抽样自动撰写，也可直接手写或编辑。'
                     ).classes('text-caption text-grey')

            editor = ui.textarea(value=existing or '').classes('w-full').props('rows=14 outlined')

            with ui.row().classes('gap-2'):
                gen_btn = ui.button('🤖 生成', color='primary')
                save_btn = ui.button('💾 保存', color='positive')
                ui.button('关闭', on_click=dialog.close)

            async def _generate():
                if not self.translation_service:
                    _safe(ui.notify, '请先配置翻译器', type='warning')
                    return
                gen_btn.disable()
                _safe(setattr, gen_btn, 'text', '生成中...')
                try:
                    sample = await loop.run_in_executor(None, self._sample_dialogue_text)
                    if not sample:
                        _safe(ui.notify, '项目中没有可抽样的对话', type='warning')
                        return
                    guide = await loop.run_in_executor(
                        None, self.translation_service.translator.generate_style_guide, sample
                    )
                    if guide:
                        editor.value = guide
                        _safe(ui.notify, '风格指南已生成，可编辑后保存', type='positive')
                    else:
                        _safe(ui.notify, '生成失败（空结果）', type='negative')
                except Exception as e:
                    _safe(ui.notify, f'生成失败: {e}', type='negative')
                finally:
                    gen_btn.enable()
                    _safe(setattr, gen_btn, 'text', '🤖 生成')

            async def _save():
                await loop.run_in_executor(None, self.db.set_meta, 'style_guide', editor.value or '')
                _safe(ui.notify, '风格指南已保存，后续翻译将按此风格执行', type='positive')
                dialog.close()

            gen_btn.on_click(_generate)
            save_btn.on_click(_save)

        dialog.props('persistent')
        dialog.open()

    def _sample_dialogue_text(self) -> str:
        """抽样对话文本用于风格分析（同步，在线程池中调用）

        每个 label 取前 3 句 + 随机 50 句，总量限 ~30K 字符。
        """
        import random
        rows = self.db._conn.execute(
            "SELECT label, character, original_text FROM dialogues "
            "WHERE length(original_text) > 5 ORDER BY file_path, line_number"
        ).fetchall()
        if not rows:
            return ""

        # 每个 label 前 3 句
        samples, seen_labels = [], set()
        for r in rows:
            label = r['label'] or ''
            if label not in seen_labels:
                seen_labels.add(label)
                label_rows = [x for x in rows if (x['label'] or '') == label][:3]
                samples.extend(label_rows)

        # 随机补 50 句
        pool = [r for r in rows if r not in samples]
        if pool:
            samples.extend(random.sample(pool, min(50, len(pool))))

        # 拼装，限 30K 字符
        lines, total = [], 0
        for r in samples:
            char = r['character'] or '旁白'
            line = f"{char}: {r['original_text']}"
            if total + len(line) > 30000:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)
