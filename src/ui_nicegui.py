"""Ren'Py翻译工具 - NiceGUI前端"""

import os
import sys
from nicegui import ui

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from project_manager import ProjectManager, Project
from config_manager import ConfigManager, ModelConfig
from translator import AITranslator, TranslationConfig
from renpy_parser import RenpyParser


class TranslatorUI:
    """翻译工具UI"""

    def __init__(self):
        # 后端管理器
        self.project_manager = ProjectManager()
        self.config_manager = ConfigManager()
        self.parser = RenpyParser()

        # 当前状态
        self.current_project: Project = None
        self.translator: AITranslator = None
        self.current_index = 0
        self.filter_mode = "all"
        self.search_text = ""

        # UI组件引用
        self.project_list = None
        self.dialogue_preview = None
        self.translation_input = None
        self.status_label = None
        self.progress_label = None

    def create(self):
        """创建UI"""
        # 页面标题
        ui.markdown('# 🎮 Ren\'Py翻译工具')

        # 顶部状态栏
        with ui.header().classes('bg-primary text-white'):
            with ui.row().classes('w-full items-center'):
                ui.label('🎮 Ren\'Py翻译工具').classes('text-h5')
                ui.space()
                self.status_label = ui.label('未打开项目').classes('text-subtitle1')

        # 主布局：左侧边栏 + 右侧内容
        with ui.row().classes('w-full'):
            # 左侧边栏
            with ui.column().classes('w-64 gap-2'):
                self._create_sidebar()

            # 右侧内容区
            with ui.column().classes('flex-1 gap-2'):
                self._create_main_content()

    def _create_sidebar(self):
        """创建左侧边栏"""
        with ui.card().classes('w-full'):
            ui.label('📁 项目管理').classes('text-h6')

            # 项目列表
            self.project_list = ui.list().classes('w-full')

            # 刷新项目列表
            self._refresh_project_list()

            ui.separator()

            # 新建项目
            ui.label('新建项目').classes('text-subtitle2')
            new_name = ui.input(label='项目名称', placeholder='MyGame')
            new_game_dir = ui.input(label='游戏目录', placeholder='Z:\\game\\MyGame')

            # 模型选择
            model_names = [c.name for c in self.config_manager.load_all_configs()]
            new_model = ui.select(options=model_names, label='AI模型', value=model_names[0] if model_names else None)

            ui.button('➕ 创建项目', on_click=lambda: self._create_project(
                new_name.value, new_game_dir.value, new_model.value
            )).classes('w-full')

    def _create_main_content(self):
        """创建主内容区"""
        # 标签页
        with ui.tabs() as tabs:
            tab_translate = ui.tab('翻译', icon='translate')
            tab_batch = ui.tab('批量', icon='speed')
            tab_dict = ui.tab('词典', icon='dictionary')
            tab_config = ui.tab('配置', icon='settings')

        with ui.tab_panels(tabs, value='翻译').classes('w-full'):
            # 翻译工作台
            with ui.tab_panel('翻译'):
                self._create_translate_panel()

            # 批量翻译
            with ui.tab_panel('批量'):
                self._create_batch_panel()

            # 人名词典
            with ui.tab_panel('词典'):
                self._create_dict_panel()

            # 模型配置
            with ui.tab_panel('配置'):
                self._create_config_panel()

    def _create_translate_panel(self):
        """创建翻译工作台"""
        # 进度显示
        self.progress_label = ui.label('请先打开项目').classes('text-subtitle1')

        # 筛选栏
        with ui.row().classes('w-full gap-2'):
            search = ui.input(label='搜索', placeholder='搜索原文/译文').classes('flex-1')
            filter_mode = ui.select(
                options={'all': '全部', 'untranslated': '未翻译', 'translated': '已翻译'},
                label='筛选', value='all'
            )
            filter_char = ui.input(label='角色', placeholder='角色名')
            ui.button('🔍', on_click=lambda: self._apply_filter(
                search.value, filter_mode.value, filter_char.value
            ))

        ui.separator()

        # 对话预览卡片
        with ui.card().classes('w-full'):
            self.dialogue_preview = ui.markdown('暂无对话')

        # 翻译输入
        with ui.card().classes('w-full'):
            self.translation_input = ui.textarea(
                label='翻译输入（留空则AI翻译）',
                placeholder='输入手动翻译...'
            ).classes('w-full')

            # 操作按钮
            with ui.row().classes('w-full gap-2'):
                ui.button('⏮ 第一句', on_click=lambda: self._navigate('first'))
                ui.button('⬅ 上一句', on_click=lambda: self._navigate('prev'))
                ui.button('➡ 下一句', on_click=lambda: self._navigate('next'))
                ui.button('⏭ 最后', on_click=lambda: self._navigate('last'))

            with ui.row().classes('w-full gap-2'):
                ui.button('🤖 AI翻译', color='primary', on_click=self._ai_translate)
                ui.button('✏️ 手动翻译', on_click=self._manual_translate)
                ui.button('💾 保存', on_click=self._save_project)

    def _create_batch_panel(self):
        """创建批量翻译面板"""
        ui.label('⚡ 批量翻译').classes('text-h5')

        # 统计信息
        self.batch_stats = ui.label('请先打开项目').classes('text-subtitle1')

        # 分页控件
        with ui.row().classes('w-full items-center gap-2'):
            self.batch_page_size = ui.number(label='每页条数', value=50, min=10, max=200).classes('w-32')
            ui.button('⏮', on_click=lambda: self._batch_goto_page(0)).props('flat')
            ui.button('◀', on_click=lambda: self._batch_prev_page()).props('flat')
            self.batch_page_label = ui.label('第 1 页')
            ui.button('▶', on_click=lambda: self._batch_next_page()).props('flat')
            ui.button('⏭', on_click=lambda: self._batch_goto_page(-1)).props('flat')
            ui.space()
            ui.button('🚀 翻译本页', color='primary', on_click=self._batch_translate_page)
            ui.button('🔄 刷新', on_click=self._batch_refresh)

        ui.separator()

        # 对话表格
        self.batch_table = ui.table(
            columns=[
                {'name': 'index', 'label': '#', 'field': 'index', 'sortable': True},
                {'name': 'character', 'label': '角色', 'field': 'character', 'sortable': True},
                {'name': 'original', 'label': '原文', 'field': 'original', 'style': 'max-width: 400px; white-space: normal;'},
                {'name': 'translated', 'label': '译文', 'field': 'translated', 'style': 'max-width: 400px; white-space: normal;'},
                {'name': 'status', 'label': '状态', 'field': 'status', 'sortable': True},
            ],
            rows=[],
            row_key='index',
            pagination=0
        ).classes('w-full')

        # 状态日志
        ui.separator()
        ui.label('翻译日志').classes('text-subtitle1')
        self.batch_log = ui.log().classes('w-full h-48')

        # 分页状态
        self.batch_current_page = 0

    def _create_dict_panel(self):
        """创建人名词典面板"""
        ui.label('👤 人名词典').classes('text-h5')

        with ui.card().classes('w-full'):
            self.dict_editor = ui.textarea(
                label='人名词典（格式：英文名 → 中文名）',
                placeholder='Eileen → 艾琳\nLucy → 露西'
            ).classes('w-full h-64')

            with ui.row().classes('gap-2'):
                ui.button('📂 加载', on_click=self._load_dict)
                ui.button('💾 保存', color='primary', on_click=self._save_dict)

    def _create_config_panel(self):
        """创建模型配置面板"""
        ui.label('⚙️ 模型配置').classes('text-h5')

        # 配置表单
        with ui.card().classes('w-full'):
            self.config_name_input = ui.input(label='配置名称', placeholder='GPT-4')
            self.config_api_base = ui.input(label='API地址', value='https://api.openai.com/v1')
            self.config_api_key = ui.input(label='API Key', password=True)
            self.config_model = ui.input(label='模型名称', placeholder='gpt-4')

            with ui.row().classes('gap-2 items-center w-full'):
                ui.label('Temperature:')
                self.config_temp = ui.slider(min=0, max=2, value=0.3, step=0.1).classes('flex-1')
                self.temp_label = ui.label('0.3')

            # 绑定slider值到label
            self.config_temp.on_value_change(lambda e: self.temp_label.set_text(str(e.value)))

            with ui.row().classes('gap-2'):
                self.config_max_tokens = ui.number(label='最大Token', value=1000, min=100, max=4000)
                self.config_context = ui.number(label='上下文行数', value=3, min=0, max=10)

            with ui.row().classes('gap-2'):
                ui.button('💾 保存配置', color='primary', on_click=self._save_config_form)
                ui.button('🔄 清空表单', on_click=self._clear_config_form)

        ui.separator()

        # 配置列表
        ui.label('已有配置').classes('text-subtitle1')
        self.config_list = ui.list().classes('w-full')
        self._refresh_config_list()

    # ========== 项目操作 ==========

    def _refresh_project_list(self):
        """刷新项目列表"""
        self.project_list.clear()
        projects = self.project_manager.list_projects()

        with self.project_list:
            for p in projects:
                with ui.item(on_click=lambda name=p.name: self._open_project(name)):
                    with ui.item_section():
                        ui.item_label(p.name)
                        ui.item_label(p.progress_text).props('caption')

    def _create_project(self, name, game_dir, model):
        """创建项目"""
        if not name or not game_dir:
            ui.notify('请填写完整信息', type='warning')
            return

        try:
            # 创建项目
            project = self.project_manager.create_project(name, game_dir, model or '')

            # 解析游戏
            ui.notify('正在解析游戏...', type='info')
            result = self.parser.parse_directory(game_dir, extract_rpa=True, decompile_rpyc=True)

            # 保存解析结果
            project.dialogues = [self._dialogue_to_dict(d) for d in result['dialogues']]
            project.ui_texts = [self._dialogue_to_dict(d) for d in result['ui_texts']]
            project.characters = [self._character_to_dict(c) for c in result['characters']]

            # 初始化人名词典
            for char in result['characters']:
                if char.name not in project.char_dict:
                    project.char_dict[char.name] = char.name

            self.project_manager.save_project(project)

            ui.notify(f'项目创建成功！共 {len(project.dialogues)} 条对话', type='positive')
            self._refresh_project_list()

        except Exception as e:
            ui.notify(f'创建失败: {str(e)}', type='negative')

    def _open_project(self, name):
        """打开项目"""
        project = self.project_manager.load_project(name)
        if not project:
            ui.notify('项目不存在', type='negative')
            return

        self.current_project = project
        self.current_index = project.last_position.get('index', 0)
        self.batch_current_page = 0

        # 初始化翻译器
        if project.model_config_name:
            self._init_translator(project.model_config_name)

        # 更新UI
        self._update_progress()
        self._update_preview()
        self._batch_refresh()
        self.status_label.text = f'当前项目: {name}'

        ui.notify(f'已打开项目: {name}', type='positive')

    def _save_project(self):
        """保存项目"""
        if not self.current_project:
            ui.notify('未打开项目', type='warning')
            return

        # 更新位置
        filtered = self._get_filtered_dialogues()
        if filtered and self.current_index < len(filtered):
            dialogue = filtered[self.current_index]
            self.current_project.last_position = {
                'index': self.current_index,
                'file': dialogue.get('file_path', ''),
                'line': dialogue.get('line_number', 0)
            }

        if self.project_manager.save_project(self.current_project):
            ui.notify('项目已保存', type='positive')
        else:
            ui.notify('保存失败', type='negative')

    # ========== 翻译操作 ==========

    def _get_filtered_dialogues(self):
        """获取筛选后的对话"""
        if not self.current_project:
            return []

        dialogues = self.current_project.dialogues

        if self.filter_mode == 'untranslated':
            dialogues = [d for d in dialogues if not d.get('is_translated', False)]
        elif self.filter_mode == 'translated':
            dialogues = [d for d in dialogues if d.get('is_translated', False)]

        if self.search_text:
            search_lower = self.search_text.lower()
            dialogues = [d for d in dialogues
                        if search_lower in d.get('original_text', '').lower()
                        or search_lower in d.get('translated_text', '').lower()]

        return dialogues

    def _update_progress(self):
        """更新进度显示"""
        if not self.current_project:
            self.progress_label.text = '请先打开项目'
            return

        stats = self.current_project.get_stats()
        filtered = self._get_filtered_dialogues()
        self.progress_label.text = (
            f"📊 总进度: {stats['translated_dialogues']}/{stats['total_dialogues']} 对话 | "
            f"当前筛选: {len(filtered)} 条 | 位置: {self.current_index + 1}/{len(filtered)}"
        )

    def _update_preview(self):
        """更新对话预览"""
        filtered = self._get_filtered_dialogues()
        if not filtered or self.current_index >= len(filtered):
            self.dialogue_preview.content = '暂无对话'
            return

        dialogue = filtered[self.current_index]

        # 构建预览文本
        preview = f"""
**📍 位置:** {dialogue.get('file_path', '')}:{dialogue.get('line_number', 0)}

**👤 角色:** {dialogue.get('character', '') or '旁白'}

**📝 原文:**
{dialogue.get('original_text', '')}

**🔄 译文:**
{dialogue.get('translated_text', '') or '*未翻译*'}
"""

        # 上下文
        ctx_before = dialogue.get('context_before', [])
        ctx_after = dialogue.get('context_after', [])

        if ctx_before:
            preview += '\n\n**--- 前文 ---**\n'
            for line in ctx_before[-2:]:
                preview += f'{line}\n'

        if ctx_after:
            preview += '\n**--- 后文 ---**\n'
            for line in ctx_after[:2]:
                preview += f'{line}\n'

        self.dialogue_preview.content = preview

    def _apply_filter(self, search, mode, char):
        """应用筛选"""
        self.search_text = search
        self.filter_mode = mode
        self.current_index = 0
        self._update_progress()
        self._update_preview()

    def _navigate(self, direction):
        """导航对话"""
        filtered = self._get_filtered_dialogues()
        if not filtered:
            return

        if direction == 'first':
            self.current_index = 0
        elif direction == 'prev':
            self.current_index = max(0, self.current_index - 1)
        elif direction == 'next':
            self.current_index = min(len(filtered) - 1, self.current_index + 1)
        elif direction == 'last':
            self.current_index = len(filtered) - 1

        self._update_progress()
        self._update_preview()

    def _ai_translate(self):
        """AI翻译"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return

        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        filtered = self._get_filtered_dialogues()
        if not filtered or self.current_index >= len(filtered):
            return

        dialogue = filtered[self.current_index]

        try:
            translated = self.translator.translate_text(
                text=dialogue['original_text'],
                character=dialogue.get('character', ''),
                context_before=dialogue.get('context_before', []),
                context_after=dialogue.get('context_after', []),
                character_dict=self.current_project.char_dict
            )

            dialogue['translated_text'] = translated
            dialogue['is_translated'] = True

            self._update_progress()
            self._update_preview()
            self._save_project()

            ui.notify('翻译完成', type='positive')

        except Exception as e:
            ui.notify(f'翻译失败: {str(e)}', type='negative')

    def _manual_translate(self):
        """手动翻译"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return

        text = self.translation_input.value
        if not text:
            ui.notify('请输入翻译内容', type='warning')
            return

        filtered = self._get_filtered_dialogues()
        if not filtered or self.current_index >= len(filtered):
            return

        dialogue = filtered[self.current_index]
        dialogue['translated_text'] = text
        dialogue['is_translated'] = True

        self.translation_input.value = ''
        self._update_progress()
        self._update_preview()
        self._save_project()

        ui.notify('翻译已保存', type='positive')

    def _batch_refresh(self):
        """刷新批量翻译表格"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return

        dialogues = self.current_project.dialogues
        total = len(dialogues)
        translated = sum(1 for d in dialogues if d.get('is_translated', False))
        untranslated = total - translated

        # 更新统计
        self.batch_stats.text = f'📊 总计: {total} | ✅ 已翻译: {translated} | ❌ 未翻译: {untranslated}'

        # 计算分页
        page_size = int(self.batch_page_size.value)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        self.batch_current_page = min(self.batch_current_page, total_pages - 1)

        # 获取当前页数据
        start = self.batch_current_page * page_size
        end = min(start + page_size, total)
        page_dialogues = dialogues[start:end]

        # 更新表格
        rows = []
        for i, d in enumerate(page_dialogues):
            status = '✅' if d.get('is_translated', False) else '❌'
            rows.append({
                'index': start + i + 1,
                'character': d.get('character', '') or '旁白',
                'original': d.get('original_text', '')[:100] + ('...' if len(d.get('original_text', '')) > 100 else ''),
                'translated': d.get('translated_text', '')[:100] + ('...' if len(d.get('translated_text', '')) > 100 else ''),
                'status': status
            })

        self.batch_table.rows = rows
        self.batch_page_label.text = f'第 {self.batch_current_page + 1} / {total_pages} 页'

    def _batch_prev_page(self):
        """上一页"""
        if self.batch_current_page > 0:
            self.batch_current_page -= 1
            self._batch_refresh()

    def _batch_next_page(self):
        """下一页"""
        if not self.current_project:
            return

        page_size = int(self.batch_page_size.value)
        total = len(self.current_project.dialogues)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1

        if self.batch_current_page < total_pages - 1:
            self.batch_current_page += 1
            self._batch_refresh()

    def _batch_goto_page(self, page):
        """跳转到指定页"""
        if not self.current_project:
            return

        if page == -1:
            # 跳转到最后一页
            page_size = int(self.batch_page_size.value)
            total = len(self.current_project.dialogues)
            self.batch_current_page = (total + page_size - 1) // page_size - 1 if total > 0 else 0
        else:
            self.batch_current_page = max(0, page)

        self._batch_refresh()

    def _batch_translate_page(self):
        """翻译当前页"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return

        if not self.translator:
            ui.notify('请先配置翻译器', type='warning')
            return

        # 获取当前页未翻译的对话
        page_size = int(self.batch_page_size.value)
        start = self.batch_current_page * page_size
        end = min(start + page_size, len(self.current_project.dialogues))
        page_dialogues = self.current_project.dialogues[start:end]

        to_translate = [d for d in page_dialogues if not d.get('is_translated', False)]
        if not to_translate:
            ui.notify('当前页所有对话已翻译', type='info')
            return

        total = len(to_translate)
        success = 0
        stopped = False

        with self.batch_log:
            print(f'开始翻译第 {self.batch_current_page + 1} 页，共 {total} 条未翻译')

            for i, dialogue in enumerate(to_translate):
                try:
                    translated = self.translator.translate_text(
                        text=dialogue['original_text'],
                        character=dialogue.get('character', ''),
                        context_before=dialogue.get('context_before', []),
                        context_after=dialogue.get('context_after', []),
                        character_dict=self.current_project.char_dict
                    )
                    dialogue['translated_text'] = translated
                    dialogue['is_translated'] = True
                    success += 1
                    print(f'✅ [{success}/{total}] {dialogue["original_text"][:30]}...')

                except Exception as e:
                    error_msg = str(e)
                    print(f'❌ 翻译失败: {error_msg}')
                    ui.notify(f'API错误，翻译已停止: {error_msg}', type='negative')
                    stopped = True
                    break

        # 保存项目
        self._save_project()
        self._batch_refresh()
        self._update_progress()

        if stopped:
            ui.notify(f'翻译已停止！成功: {success}/{total}，请检查API配置后重试', type='warning')
        else:
            ui.notify(f'本页翻译完成！成功: {success}/{total}', type='positive')

    # ========== 人名词典 ==========

    def _load_dict(self):
        """加载人名词典"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return

        lines = []
        for en, cn in sorted(self.current_project.char_dict.items()):
            lines.append(f'{en} → {cn}')
        self.dict_editor.value = '\n'.join(lines)

    def _save_dict(self):
        """保存人名词典"""
        if not self.current_project:
            ui.notify('请先打开项目', type='warning')
            return

        new_dict = {}
        for line in self.dict_editor.value.strip().split('\n'):
            if '→' in line:
                parts = line.split('→')
                en_name = parts[0].strip()
                cn_name = parts[1].strip()
                if en_name and cn_name:
                    new_dict[en_name] = cn_name

        self.current_project.char_dict = new_dict
        self._save_project()
        ui.notify(f'已保存 {len(new_dict)} 条记录', type='positive')

    # ========== 模型配置 ==========

    def _refresh_config_list(self):
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
                        ui.button(icon='edit', on_click=lambda name=c.name: self._edit_config(name)).props('flat dense')
                        ui.button(icon='delete', color='red', on_click=lambda name=c.name: self._delete_config(name)).props('flat dense')

    def _save_config_form(self):
        """从表单保存配置"""
        name = self.config_name_input.value
        if not name:
            ui.notify('请输入配置名称', type='warning')
            return

        config = ModelConfig(
            name=name,
            api_base=self.config_api_base.value,
            api_key=self.config_api_key.value,
            model=self.config_model.value,
            temperature=float(self.config_temp.value),
            max_tokens=int(self.config_max_tokens.value),
            context_lines=int(self.config_context.value)
        )

        # 检查是否已存在
        existing = self.config_manager.get_config_by_name(name)
        if existing:
            self.config_manager.update_config(name, config)
            ui.notify(f'配置已更新: {name}', type='positive')
        else:
            self.config_manager.add_config(config)
            ui.notify(f'配置已保存: {name}', type='positive')

        self._refresh_config_list()

    def _edit_config(self, name):
        """编辑配置 - 填充表单"""
        config = self.config_manager.get_config_by_name(name)
        if not config:
            ui.notify('配置不存在', type='negative')
            return

        # 填充表单
        self.config_name_input.value = config.name
        self.config_api_base.value = config.api_base
        self.config_api_key.value = config.api_key
        self.config_model.value = config.model
        self.config_temp.value = config.temperature
        self.config_max_tokens.value = config.max_tokens
        self.config_context.value = config.context_lines

        ui.notify(f'已加载配置: {name}', type='info')

    def _delete_config(self, name):
        """删除配置"""
        # 使用对话框确认
        with ui.dialog() as dialog:
            with ui.card():
                ui.label(f'确定要删除配置 "{name}" 吗？')
                with ui.row().classes('gap-2'):
                    ui.button('取消', on_click=dialog.close)
                    ui.button('删除', color='red', on_click=lambda: (
                        self.config_manager.delete_config(name),
                        self._refresh_config_list(),
                        ui.notify(f'已删除配置: {name}', type='positive'),
                        dialog.close()
                    ))
        dialog.open()

    def _clear_config_form(self):
        """清空配置表单"""
        self.config_name_input.value = ''
        self.config_api_base.value = 'https://api.openai.com/v1'
        self.config_api_key.value = ''
        self.config_model.value = ''
        self.config_temp.value = 0.3
        self.config_max_tokens.value = 1000
        self.config_context.value = 3

    def _init_translator(self, config_name):
        """初始化翻译器"""
        config = self.config_manager.get_config_by_name(config_name)
        if not config:
            return

        trans_config = TranslationConfig(
            api_base=config.api_base,
            api_key=config.api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            context_lines=config.context_lines
        )
        self.translator = AITranslator(trans_config)

    # ========== 辅助方法 ==========

    def _dialogue_to_dict(self, d):
        """将DialogueLine转为字典"""
        return {
            'file_path': d.file_path,
            'line_number': d.line_number,
            'character': d.character,
            'original_text': d.original_text,
            'translated_text': d.translated_text,
            'is_translated': d.is_translated,
            'context_before': d.context_before or [],
            'context_after': d.context_after or []
        }

    def _character_to_dict(self, c):
        """将CharacterInfo转为字典"""
        return {
            'variable': c.variable,
            'name': c.name,
            'chinese_name': c.chinese_name
        }


def create_app():
    """创建应用"""
    app = TranslatorUI()
    app.create()
    return app
