"""项目管理面板 - 创建/导入/删除/导出项目

所有文件 I/O、数据库操作、子进程调用均通过 run_in_executor 执行，
绝不阻塞 NiceGUI 事件循环。
"""

import asyncio
import os
import zipfile
import shutil


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (RuntimeError, AttributeError):
        return None
import json
import tempfile
from pathlib import Path
from datetime import datetime
from nicegui import ui

from project_manager import ProjectManager
from database import ProjectDatabase
from logger import TranslationLogger


class ProjectPanel:
    """项目管理面板"""

    def __init__(self, project_manager: ProjectManager,
                 logger: TranslationLogger,
                 on_project_open: callable = None,
                 get_sdk_path: callable = None,
                 get_model_names: callable = None):
        self.project_manager = project_manager
        self.logger = logger
        self.on_project_open = on_project_open
        self.get_sdk_path = get_sdk_path
        self.get_model_names = get_model_names

        self.project_card_container: ui.column = None
        self.export_packages_container: ui.column = None
        self._project_cards: dict = {}  # {name: {'card': card, 'label_name': lbl, 'label_progress': lbl}}

    def create(self, container: ui.column):
        """创建面板"""
        with container:
            with ui.row().classes('items-center q-mb-md'):
                ui.label('项目管理').classes('text-h6')
                ui.space()
                ui.button('新建项目', on_click=self._show_create_dialog,
                          icon='add', color='primary').props('dense')
                ui.button('导入项目', on_click=self._show_import_dialog,
                          icon='file_upload').props('dense outline')

            ui.separator()

            self.project_card_container = ui.column().classes('full-width')
            self.export_packages_container = ui.column().classes('full-width')

            ui.separator()
            with ui.row().classes('items-center q-mt-md q-mb-sm'):
                ui.label('已导出的项目包').classes('text-subtitle1')
                ui.space()
                ui.button('刷新', icon='refresh',
                          on_click=self._run_refresh_exports).props('flat dense')

        # 初始加载项目列表（create 中有 slot 上下文，直接同步加载）
        self._load_initial_projects()

    async def _run_refresh_exports(self):
        await self.async_refresh_export_packages()

    async def _run_open_project(self, name: str):
        await self._open_project(name)

    def _run_export_project(self, name: str):
        """导出项目（创建 UI 后调用 async 方法）"""
        # 在 slot 上下文中创建进度对话框
        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label('📦 导出项目').classes('text-h6')
            progress_bar = ui.linear_progress(value=0, show_value=True).classes('w-full')
            progress_label = ui.label('准备中...').classes('text-caption')
        dialog.props('persistent')
        dialog.open()
        asyncio.create_task(self._export_project(name, dialog, progress_bar, progress_label))

    def _run_edit_project(self, name: str):
        """编辑项目（先创建对话框，再异步加载数据）"""
        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label('✏️ 编辑项目').classes('text-h6')
            name_input = ui.input(label='项目名称').classes('w-full')
            model_names = self.get_model_names() if self.get_model_names else []
            model_select = ui.select(
                options=model_names, label='AI模型'
            ).classes('w-full')
            with ui.row().classes('gap-2'):
                ui.button('取消', on_click=dialog.close)
                async def _run_save():
                    await self._save_edit(name, name_input.value, model_select.value, dialog)
                ui.button('保存', color='primary', on_click=_run_save)
        dialog.props('persistent')
        asyncio.create_task(self._load_edit_data(name, dialog, name_input, model_select))

    def _load_initial_projects(self):
        """初始加载项目列表（同步，在 create 调用，有 slot 上下文）"""
        projects = self.project_manager.list_projects()

        with self.project_card_container:
            if not projects:
                with ui.card().classes('w-full flat bordered q-pa-lg'):
                    with ui.column().classes('items-center'):
                        ui.icon('folder_off', size='3rem').classes('text-grey-5')
                        ui.label('暂无项目').classes('text-grey-6 q-mt-sm')
                        ui.label('点击上方「新建项目」开始').classes('text-caption text-grey-5')
                return

            for p in projects:
                with ui.card().classes('w-full flat bordered q-mb-sm') as card:
                    with ui.row().classes('items-center full-width q-pa-sm'):
                        ui.icon('folder', size='1.5rem').classes('text-grey-6')
                        with ui.column().classes('q-ml-sm'):
                            lbl_name = ui.label(p.name).classes('text-subtitle1')
                            lbl_progress = ui.label(p.progress_text).props('caption')
                        ui.space()
                        with ui.row().classes('gap-xs items-center'):
                            ui.button('打开', on_click=lambda n=p.name: asyncio.create_task(self._run_open_project(n))).props('dense flat no-caps')
                            ui.button('导出', on_click=lambda n=p.name: asyncio.create_task(self._run_export_project(n))).props('dense flat no-caps')
                            ui.button(icon='edit', on_click=lambda n=p.name: asyncio.create_task(self._run_edit_project(n))).props('flat dense round size=sm')
                            ui.button(icon='delete', on_click=lambda n=p.name: self._confirm_delete(n)).props('flat dense round size=sm color=negative')
                self._project_cards[p.name] = {
                    'card': card,
                    'label_name': lbl_name,
                    'label_progress': lbl_progress,
                }

    async def async_refresh_projects(self):
        """刷新项目卡片列表（就地更新，不清空重建）"""
        loop = asyncio.get_event_loop()
        projects = await loop.run_in_executor(None, self.project_manager.list_projects)

        # 构建新项目名集合
        new_names = {p.name for p in projects}
        old_names = {name for name in self._project_cards}

        # 删除不再存在的项目卡片
        for name in old_names - new_names:
            self._project_cards[name]['card'].delete()
            del self._project_cards[name]

        # 更新或创建项目卡片
        for p in projects:
            if p.name in self._project_cards:
                # 更新已有卡片的文本
                card_data = self._project_cards[p.name]
                card_data['label_name'].text = p.name
                card_data['label_progress'].text = p.progress_text
            else:
                # 创建新卡片
                with self.project_card_container:
                    with ui.card().classes('w-full flat bordered q-mb-sm') as card:
                        with ui.row().classes('items-center full-width q-pa-sm'):
                            ui.icon('folder', size='1.5rem').classes('text-grey-6')
                            with ui.column().classes('q-ml-sm'):
                                lbl_name = ui.label(p.name).classes('text-subtitle1')
                                lbl_progress = ui.label(p.progress_text).props('caption')
                            ui.space()
                            with ui.row().classes('gap-xs items-center'):
                                ui.button('打开', on_click=lambda n=p.name: asyncio.create_task(self._run_open_project(n))).props('dense flat no-caps')
                                ui.button('导出', on_click=lambda n=p.name: self._run_export_project(n)).props('dense flat no-caps')
                                ui.button(icon='edit', on_click=lambda n=p.name: self._run_edit_project(n)).props('flat dense round size=sm')
                                ui.button(icon='delete', on_click=lambda n=p.name: self._confirm_delete(n)).props('flat dense round size=sm color=negative')
                    self._project_cards[p.name] = {
                        'card': card,
                        'label_name': lbl_name,
                        'label_progress': lbl_progress,
                    }

        # 处理空列表
        if not projects and not hasattr(self, '_empty_placeholder'):
            with self.project_card_container:
                self._empty_placeholder = ui.card().classes('w-full flat bordered q-pa-lg')
                with self._empty_placeholder:
                    with ui.column().classes('items-center'):
                        ui.icon('folder_off', size='3rem').classes('text-grey-5')
                        ui.label('暂无项目').classes('text-grey-6 q-mt-sm')
                        ui.label('点击上方「新建项目」开始').classes('text-caption text-grey-5')
        elif projects and hasattr(self, '_empty_placeholder'):
            self._empty_placeholder.delete()
            del self._empty_placeholder

    async def _open_project(self, name: str):
        """打开项目（委托给主应用）"""
        if self.on_project_open:
            await self.on_project_open(name)

    # ========== 创建项目 ==========

    def _show_create_dialog(self):
        """显示创建项目对话框"""
        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label('➕ 创建新项目').classes('text-h6')
            ui.label('选择游戏目录路径或上传zip文件').classes('text-caption text-grey')

            name_input = ui.input(label='项目名称', placeholder='MyGame').classes('w-full')
            input_method = ui.toggle(['路径输入', '上传zip'], value='路径输入').classes('w-full')

            path_container = ui.column().classes('w-full')
            with path_container:
                game_dir_input = ui.input(label='游戏目录', placeholder='C:\\path\\to\\game').classes('w-full')

            zip_container = ui.column().classes('w-full')
            zip_container.style('display: none;')
            zip_data = {'zip_path': None}

            with zip_container:
                async def on_zip_upload(e):
                    zip_status.text = '⏳ 正在保存文件...'
                    loop = asyncio.get_event_loop()
                    temp_dir = await loop.run_in_executor(None, tempfile.mkdtemp)
                    temp_path = Path(temp_dir) / e.file.name
                    await e.file.save(str(temp_path))
                    zip_data['zip_path'] = str(temp_path)
                    zip_status.text = f'✅ 已上传: {e.file.name}'

                ui.upload(label='选择游戏zip文件', on_upload=on_zip_upload,
                          max_files=1, auto_upload=True).props('accept=.zip')
                zip_status = ui.label('未选择文件').classes('text-caption')

            def _on_method_change(e):
                if e.value == '路径输入':
                    path_container.style('display: block;')
                    zip_container.style('display: none;')
                else:
                    path_container.style('display: none;')
                    zip_container.style('display: block;')

            input_method.on_value_change(_on_method_change)

            model_names = self.get_model_names() if self.get_model_names else []
            model_select = ui.select(
                options=model_names, label='AI模型',
                value=model_names[0] if model_names else None
            ).classes('w-full')

            async def _on_create():
                name = name_input.value
                game_dir = game_dir_input.value

                # 基础校验
                if not name:
                    ui.notify('请填写项目名称', type='warning')
                    return
                if input_method.value == '上传zip' and not zip_data.get('zip_path'):
                    ui.notify('请先上传zip文件', type='warning')
                    return
                if input_method.value == '路径输入' and not game_dir:
                    ui.notify('请填写游戏目录', type='warning')
                    return

                # 立即关闭对话框，显示进度条
                dialog.close()
                await asyncio.sleep(0)

                with ui.dialog() as progress_dialog, ui.card().classes('w-96'):
                    ui.label('➕ 创建项目').classes('text-h6')
                    progress_bar = ui.linear_progress(value=0, show_value=True).classes('w-full')
                    progress_label = ui.label('准备中...').classes('text-caption')

                progress_dialog.props('persistent')
                progress_dialog.open()

                loop = asyncio.get_event_loop()

                try:
                    # 解压 zip（带详细进度）
                    if input_method.value == '上传zip':
                        progress_bar.value = 0.01
                        progress_label.text = '正在解压游戏文件...'
                        await asyncio.sleep(0)

                        extract_dir = Path(zip_data['zip_path']).parent / 'extracted'
                        progress_data = {'current': 0, 'total': 0, 'done': False, 'error': None}

                        def _extract_zip():
                            try:
                                extract_dir.mkdir(exist_ok=True)
                                with zipfile.ZipFile(zip_data['zip_path'], 'r') as zf:
                                    members = zf.namelist()
                                    progress_data['total'] = len(members)
                                    for i, member in enumerate(members):
                                        zf.extract(member, str(extract_dir))
                                        progress_data['current'] = i + 1
                                entries = list(extract_dir.iterdir())
                                if len(entries) == 1 and entries[0].is_dir():
                                    progress_data['result'] = str(entries[0])
                                else:
                                    progress_data['result'] = str(extract_dir)
                            except Exception as ex:
                                progress_data['error'] = str(ex)
                            finally:
                                progress_data['done'] = True

                        extract_task = loop.run_in_executor(None, _extract_zip)

                        # 轮询更新进度条
                        while not progress_data['done']:
                            total = progress_data['total']
                            current = progress_data['current']
                            if total > 0:
                                pct = 0.02 + (current / total) * 0.03
                                progress_bar.value = pct
                                progress_label.text = f'正在解压游戏文件... ({current}/{total})'
                            await asyncio.sleep(0.2)

                        await extract_task

                        if progress_data.get('error'):
                            progress_label.text = f'❌ 解压失败: {progress_data["error"]}'
                            await asyncio.sleep(2)
                            progress_dialog.close()
                            return

                        game_dir = progress_data['result']

                        # 只清理临时 zip 文件，不解压目录
                        await loop.run_in_executor(
                            None, Path(zip_data['zip_path']).unlink, True
                        )

                    # 检查游戏目录
                    progress_bar.value = 0.05
                    progress_label.text = '正在检查游戏目录...'
                    await asyncio.sleep(0)

                    dir_exists = await loop.run_in_executor(None, Path(game_dir).exists)
                    if not dir_exists:
                        progress_label.text = '❌ 游戏目录不存在'
                        await asyncio.sleep(2)
                        progress_dialog.close()
                        return

                    # 执行创建（传入已有的进度条和标签）
                    await self._do_create(name, game_dir, model_select.value,
                                          progress_dialog, progress_bar, progress_label)

                    # 创建成功后清理临时解压目录
                    if input_method.value == '上传zip':
                        try:
                            await loop.run_in_executor(
                                None, shutil.rmtree, extract_dir, True
                            )
                        except Exception as clean_err:
                            self.logger.warning(f'清理临时解压目录失败: {clean_err}')

                except Exception as e:
                    import traceback
                    self.logger.error(f'创建项目失败: {e}\n{traceback.format_exc()}')

                    # 清理已创建的空项目目录
                    try:
                        await loop.run_in_executor(
                            None, self.project_manager.delete_project, name
                        )
                    except Exception as clean_err:
                        self.logger.error(f'清理项目目录失败: {clean_err}')

                    # 安全更新 UI
                    try:
                        progress_label.text = f'❌ 创建失败: {str(e)}'
                        await asyncio.sleep(3)
                    except Exception as ui_err:
                        self.logger.error(f'更新进度UI失败: {ui_err}')

                    try:
                        progress_dialog.close()
                    except Exception as close_err:
                        self.logger.error(f'关闭进度对话框失败: {close_err}')

                    try:
                        ui.notify(f'创建失败: {str(e)}', type='negative')
                    except Exception as notify_err:
                        self.logger.error(f'发送通知失败: {notify_err}')

            with ui.row().classes('gap-2'):
                ui.button('取消', on_click=dialog.close)
                ui.button('创建', color='primary', on_click=_on_create)

        dialog.props('persistent')
        dialog.open()

    async def _do_create(self, name: str, game_dir: str, model: str,
                         progress_dialog, progress_bar, progress_label):
        """执行创建项目（所有阻塞操作在线程池中）

        进度对话框由调用方创建并传入，本方法直接复用。
        异常由调用方捕获处理。
        """
        loop = asyncio.get_event_loop()

        # 步骤1: 创建项目数据库
        progress_bar.value = 0.05
        progress_label.text = '正在初始化项目...'
        await asyncio.sleep(0)

        db = await loop.run_in_executor(
            None, self.project_manager.create_project, name, game_dir, model or ''
        )
        project_dir = self.project_manager._get_project_dir(name)
        game_work_dir = project_dir / 'game'

        # 步骤2: 复制游戏文件（逐文件复制，带进度）
        progress_bar.value = 0.06
        progress_label.text = '正在复制游戏文件...'
        await asyncio.sleep(0)

        copy_progress = {'current': 0, 'total': 0, 'done': False}

        def _copy_game():
            try:
                if game_work_dir.exists():
                    shutil.rmtree(game_work_dir)
                # 先统计文件总数
                src = Path(game_dir)
                total = sum(1 for _ in src.rglob('*') if _.is_file())
                copy_progress['total'] = total if total > 0 else 1
                # 逐文件复制
                game_work_dir.mkdir(parents=True, exist_ok=True)
                for root, dirs, files in os.walk(src):
                    rel_root = Path(root).relative_to(src)
                    dst_root = game_work_dir / rel_root
                    dst_root.mkdir(parents=True, exist_ok=True)
                    for f in files:
                        shutil.copy2(Path(root) / f, dst_root / f)
                        copy_progress['current'] += 1
            finally:
                copy_progress['done'] = True

        copy_task = loop.run_in_executor(None, _copy_game)

        # 轮询更新进度条
        while not copy_progress['done']:
            total = copy_progress['total']
            current = copy_progress['current']
            if total > 0:
                pct = 0.06 + (current / total) * 0.04
                progress_bar.value = pct
                progress_label.text = f'正在复制游戏文件... ({current}/{total})'
            await asyncio.sleep(0.3)

        await copy_task
        progress_bar.value = 0.30

        # 步骤3: 解包 rpa & 反编译 rpyc
        progress_label.text = '正在解包游戏资源...'
        await asyncio.sleep(0)

        from renpy_parser import RenpyParser
        parser = RenpyParser()

        def _parse():
            return parser.parse_directory(
                str(game_work_dir), extract_rpa=True,
                work_dir=str(game_work_dir)
            )

        result = await loop.run_in_executor(None, _parse)
        progress_bar.value = 0.50

        # 步骤4: SDK 生成翻译文件
        sdk_path = self.get_sdk_path() if self.get_sdk_path else ''
        if sdk_path:
            progress_label.text = '正在使用 SDK 生成翻译文件...'
            await asyncio.sleep(0)

            from sdk_manager import SDKManager
            sdk = SDKManager()
            sdk.sdk_path = Path(sdk_path)

            def _sdk():
                return sdk.generate_translations(str(game_work_dir), 'chinese')

            sdk_result = await loop.run_in_executor(None, _sdk)
            if not sdk_result['success']:
                raise Exception(f'SDK 生成翻译文件失败: {sdk_result["message"]}')

        progress_bar.value = 0.70

        # 步骤5: 解析角色信息
        progress_label.text = '正在解析角色信息...'
        await asyncio.sleep(0)

        fresh_parser = RenpyParser()

        def _parse_chars():
            return fresh_parser.parse_directory(
                str(game_work_dir), extract_rpa=False
            )

        char_result = await loop.run_in_executor(None, _parse_chars)

        # 保存角色信息到数据库（在线程池中）
        characters = [{"variable": c.variable, "display_name": c.name}
                      for c in char_result['characters']]

        def _save_chars():
            db.insert_characters(characters)

        await loop.run_in_executor(None, _save_chars)
        progress_bar.value = 0.80

        # 步骤6: 解析翻译文件
        tl_dir = game_work_dir / 'game' / 'tl' / 'chinese'

        def _check_tl_dir():
            return tl_dir.exists()

        tl_exists = await loop.run_in_executor(None, _check_tl_dir)
        if tl_exists:
            progress_label.text = '正在解析翻译文件...'
            await asyncio.sleep(0)

            def _parse_tl():
                return self._parse_translation_files(tl_dir, str(game_work_dir))

            tl_result = await loop.run_in_executor(None, _parse_tl)

            def _save_tl():
                db.insert_dialogues(tl_result.get('dialogues', []))
                db.insert_ui_texts(tl_result.get('ui_texts', []))

                # 统计每个角色的台词数并更新 characters 表
                line_counts = {}
                for d in tl_result.get('dialogues', []):
                    char = d.get('character', '')
                    if char:
                        line_counts[char] = line_counts.get(char, 0) + 1

                # variable_map: variable -> display_name
                var_map = db.get_variable_map()
                for var_name, count in line_counts.items():
                    display_name = var_map.get(var_name, var_name)
                    db.update_character_lines_count(display_name, count)

            await loop.run_in_executor(None, _save_tl)

        progress_bar.value = 0.95

        # 步骤7: 清理冲突文件
        await loop.run_in_executor(None, self._cleanup_conflicts, game_work_dir)

        # 更新时间戳和统计
        def _finalize():
            db.set_meta("updated_at", datetime.now().isoformat())
            d_count = db.get_dialogue_count()
            u_count = db.get_ui_text_count()
            db.close()
            return d_count, u_count

        d_count, u_count = await loop.run_in_executor(None, _finalize)

        progress_bar.value = 1.0
        progress_label.text = f'✅ 创建成功！{d_count["total"]} 条对话, {u_count["total"]} 条字符串'

        await asyncio.sleep(0.5)
        progress_dialog.close()

        ui.notify(f'项目 "{name}" 创建成功！', type='positive')
        await self.async_refresh_projects()

        if self.on_project_open:
            await self.on_project_open(name)

    # ========== 解析翻译文件（纯同步，在线程池中调用） ==========

    def _parse_translation_files(self, tl_dir: Path, game_dir: str) -> dict:
        """解析 SDK 生成的翻译文件"""
        import re
        dialogues = []
        ui_texts = []
        game_path = Path(game_dir)

        for tl_file in tl_dir.rglob('*.rpy'):
            try:
                with open(tl_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                dialogue_lines = []
                strings_lines = []
                in_strings = False

                for line in lines:
                    if 'translate chinese strings:' in line:
                        in_strings = True
                    if in_strings:
                        strings_lines.append(line)
                    else:
                        dialogue_lines.append(line)

                if dialogue_lines:
                    self._parse_dialogue_blocks(dialogue_lines, tl_file, game_path, dialogues)
                if strings_lines:
                    self._parse_strings_block(strings_lines, tl_file, game_path, ui_texts)
            except Exception as e:
                self.logger.error(f"解析翻译文件失败 {tl_file}: {e}")

        return {'dialogues': dialogues, 'ui_texts': ui_texts}

    def _parse_dialogue_blocks(self, lines, tl_file, game_path, dialogues):
        """解析对话格式的翻译块，提取 label 归属"""
        import re
        current_file = str(tl_file.relative_to(tl_file.parent.parent.parent))
        if current_file.startswith('tl/chinese/'):
            current_file = current_file[len('tl/chinese/'):]

        current_label = ""
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            i += 1
            if not line:
                continue

            # 提取 label：translate chinese label_hash:
            translate_match = re.match(r'^translate\s+\w+\s+(\w+)\s*:', line)
            if translate_match:
                raw_label = translate_match.group(1)
                # label 格式通常是 "start_abc123"，取下划线前的部分
                current_label = raw_label.split('_')[0] if '_' in raw_label else raw_label
                continue

            if re.match(r'^\s+#\s+game/', line):
                continue

            comment_match = re.match(r'^\s+#\s*(.*)', line)
            if comment_match:
                comment_text = comment_match.group(1).strip()
                if not comment_text:
                    continue
                char_match = re.match(r'^(\w+)\s+"(.*)"', comment_text)
                narration_match = re.match(r'^"(.*)"', comment_text)
                if char_match:
                    character = char_match.group(1)
                    text = char_match.group(2).replace('\\"', '"')
                elif narration_match:
                    character = ''
                    text = narration_match.group(1).replace('\\"', '"')
                else:
                    continue
                if not text or text == '""':
                    if i < len(lines):
                        i += 1
                    continue
                if i < len(lines):
                    i += 1

                full_path = str(game_path / 'game' / current_file)
                entry = {
                    'file_path': full_path, 'line_number': 0,
                    'label': current_label,
                    'character': character, 'original_text': text,
                    'translated_text': '', 'is_translated': False,
                }
                if any(f in current_file for f in ['screens', 'gui', 'options', 'common']):
                    ui_texts.append(entry)
                else:
                    dialogues.append(entry)

    def _parse_strings_block(self, lines, tl_file, game_path, ui_texts):
        """解析 strings 格式的翻译块"""
        import re
        current_file = None
        current_line = None
        current_old = None

        for line in lines:
            line = line.rstrip()
            file_match = re.match(r'^\s+#\s+(.+):(\d+)', line)
            if file_match:
                current_file = file_match.group(1)
                current_line = int(file_match.group(2))
                continue
            old_match = re.match(r'^\s+old\s+"(.*)"', line)
            if old_match:
                current_old = old_match.group(1).replace('\\"', '"')
                continue
            new_match = re.match(r'^\s+new\s+"(.*)"', line)
            if new_match and current_old is not None:
                full_path = str(game_path / 'game' / current_file) if current_file else ''
                entry = {
                    'file_path': full_path, 'line_number': current_line or 0,
                    'label': '',
                    'character': '', 'original_text': current_old,
                    'translated_text': '', 'is_translated': False,
                }
                ui_texts.append(entry)
                current_old = None

    def _cleanup_conflicts(self, game_dir: Path):
        """清理冲突文件（纯同步，在线程池中调用）"""
        game_sub = Path(game_dir) / 'game'
        for rpa_file in game_sub.glob('*.rpa'):
            rpa_file.unlink()
        scripts_dir = game_sub / 'scripts'
        if scripts_dir.exists():
            for rpy_file in scripts_dir.glob('*.rpy'):
                py_file = rpy_file.with_suffix('.py')
                if py_file.exists():
                    try:
                        with open(rpy_file, 'r', encoding='utf-8') as f:
                            content = f.read(100)
                        if '从.rpyc文件自动提取' in content or '\x00' in content:
                            rpy_file.unlink()
                    except Exception as e:
                        self.logger.warning(f'清理冲突文件 {rpy_file.name}: {e}')
                        rpy_file.unlink()

    # ========== 导入项目 ==========

    def _show_import_dialog(self):
        """显示导入对话框"""
        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label('📥 导入项目').classes('text-h6')
            import_file_data = {'path': None}

            async def on_upload(e):
                import_label.text = '⏳ 正在保存文件...'
                loop = asyncio.get_event_loop()
                temp_dir = await loop.run_in_executor(None, tempfile.mkdtemp)
                temp_path = Path(temp_dir) / e.file.name
                await e.file.save(str(temp_path))
                import_file_data['path'] = str(temp_path)
                import_label.text = f'✅ 已选择: {e.file.name}'

            ui.upload(label='选择项目zip文件', on_upload=on_upload,
                      max_files=1, auto_upload=True).props('accept=.zip')
            import_label = ui.label('未选择文件').classes('text-caption')

            import_name = ui.input(label='项目名称（可选）', placeholder='留空使用原名称')

            # 预创建进度对话框（在 slot 上下文中）
            with ui.dialog() as progress_dialog, ui.card().classes('w-96'):
                ui.label('📥 导入项目').classes('text-h6')
                progress_bar = ui.linear_progress(value=0, show_value=True).classes('w-full')
                progress_label = ui.label('正在导入...').classes('text-caption')
            progress_dialog.props('persistent')

            with ui.row().classes('gap-2'):
                ui.button('取消', on_click=dialog.close)
                def _run_import():
                    dialog.close()
                    progress_dialog.open()
                    asyncio.create_task(self._do_import(
                        import_name.value, import_file_data,
                        progress_dialog, progress_bar, progress_label
                    ))
                ui.button('导入', color='primary', on_click=_run_import)

        dialog.props('persistent')
        dialog.open()

    async def _do_import(self, project_name, import_file_data,
                         progress_dialog, progress_bar, progress_label):
        """执行导入（所有阻塞操作在线程池中）"""
        if not import_file_data.get('path'):
            _safe(ui.notify, '请先选择zip文件', type='warning')
            progress_dialog.close()
            return

        try:
            loop = asyncio.get_event_loop()

            progress_bar.value = 0.3
            progress_label.text = '正在解压文件...'
            await asyncio.sleep(0)

            def _do_import():
                with tempfile.TemporaryDirectory() as temp_dir:
                    with zipfile.ZipFile(import_file_data['path'], 'r') as zf:
                        zf.extractall(temp_dir)
                    return self.project_manager.import_from_zip(temp_dir, project_name)

            result = await loop.run_in_executor(None, _do_import)

            progress_bar.value = 1.0
            progress_label.text = '导入完成！'
            await asyncio.sleep(0.5)

            if result['success']:
                ui.notify(result['message'], type='positive')
                await self.async_refresh_projects()
            else:
                ui.notify(result['message'], type='negative')

            # 清理临时文件（在线程池中）
            try:
                await loop.run_in_executor(
                    None, shutil.rmtree,
                    Path(import_file_data['path']).parent, True
                )
            except Exception as clean_err:
                self.logger.warning(f'清理导入临时文件失败: {clean_err}')

        except Exception as e:
            self.logger.error(f'导入项目失败: {e}')
            ui.notify(f'导入失败: {str(e)}', type='negative')
        finally:
            await asyncio.sleep(0)
            try:
                progress_dialog.close()
            except Exception as close_err:
                self.logger.warning(f'关闭导入进度对话框失败: {close_err}')

    # ========== 编辑项目 ==========

    async def _load_edit_data(self, name, dialog, name_input, model_select):
        """异步加载编辑数据并填充表单"""
        loop = asyncio.get_event_loop()

        def _load_meta():
            db = self.project_manager.open_project(name)
            if not db:
                return None
            meta = db.get_all_meta()
            db.close()
            return meta

        meta = await loop.run_in_executor(None, _load_meta)
        if not meta:
            _safe(ui.notify, '项目不存在', type='negative')
            dialog.close()
            return

        name_input.value = meta.get('name', '')
        model_select.value = meta.get('model_config_name', '')
        dialog.open()

    async def _save_edit(self, old_name, new_name, model, dialog):
        """保存编辑（所有阻塞操作在线程池中）"""
        if not new_name:
            ui.notify('项目名称不能为空', type='warning')
            return

        loop = asyncio.get_event_loop()

        if old_name != new_name:
            exists = await loop.run_in_executor(None, self.project_manager.project_exists, new_name)
            if exists:
                ui.notify('项目名称已存在', type='negative')
                return

            def _rename():
                old_dir = self.project_manager._get_project_dir(old_name)
                new_dir = self.project_manager._get_project_dir(new_name)
                old_dir.rename(new_dir)

            await loop.run_in_executor(None, _rename)

        def _update_meta():
            db = self.project_manager.open_project(new_name)
            if db:
                db.set_meta("name", new_name)
                db.set_meta("model_config_name", model)
                db.set_meta("updated_at", datetime.now().isoformat())
                db.close()

        await loop.run_in_executor(None, _update_meta)

        ui.notify('项目已更新', type='positive')
        dialog.close()
        await self.async_refresh_projects()

    # ========== 删除项目 ==========

    def _confirm_delete(self, name: str):
        """确认删除（整个流程在同一个 async 函数中，保留 slot 上下文）"""
        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label('🗑️ 删除项目').classes('text-h6')
            ui.label(f'确定要删除项目 "{name}" 吗？').classes('text-body1')
            ui.label('此操作不可撤销。').classes('text-caption text-negative')
            with ui.row().classes('gap-2'):
                ui.button('取消', on_click=dialog.close)
                ui.button('删除', color='negative',
                          on_click=lambda: self._run_delete(name, dialog))
        dialog.props('persistent')
        dialog.open()

    async def _run_delete(self, name: str, confirm_dialog):
        """删除项目的完整流程（async handler，slot 上下文保留）"""
        confirm_dialog.close()
        await asyncio.sleep(0)

        # 创建进度对话框
        with ui.dialog() as progress_dialog, ui.card().classes('w-96'):
            ui.label('🗑️ 删除项目').classes('text-h6')
            progress_bar = ui.linear_progress(value=0, show_value=True).classes('w-full')
            progress_label = ui.label(f'正在删除 "{name}"...').classes('text-caption')

        progress_dialog.props('persistent')
        progress_dialog.open()

        loop = asyncio.get_event_loop()
        delete_progress = {'current': 0, 'total': 0, 'done': False}

        def _delete():
            try:
                project_dir = self.project_manager._get_project_dir(name)
                if not project_dir.exists():
                    return
                all_files = [f for f in project_dir.rglob('*') if f.is_file()]
                delete_progress['total'] = len(all_files) if all_files else 1
                for f in all_files:
                    f.unlink()
                    delete_progress['current'] += 1
                import shutil
                shutil.rmtree(project_dir, ignore_errors=True)
            except Exception as e:
                self.logger.error(f'删除项目文件失败: {e}')
            finally:
                delete_progress['done'] = True

        delete_task = loop.run_in_executor(None, _delete)

        while not delete_progress['done']:
            total = delete_progress['total']
            current = delete_progress['current']
            if total > 0:
                progress_bar.value = current / total
                progress_label.text = f'正在删除... ({current}/{total})'
            await asyncio.sleep(0.2)

        await delete_task

        progress_bar.value = 1.0
        progress_label.text = f'✅ 已删除 {delete_progress["current"]} 个文件'
        await asyncio.sleep(0.5)
        progress_dialog.close()

        ui.notify(f'项目 "{name}" 已删除', type='positive')
        await self.async_refresh_projects()

    # ========== 导出项目 ==========

    async def _export_project(self, name: str, dialog, progress_bar, progress_label):
        """导出项目为 ZIP 包（所有阻塞操作在线程池中）"""

        try:
            loop = asyncio.get_event_loop()

            progress_bar.value = 0.1
            progress_label.text = '正在读取项目数据...'
            await asyncio.sleep(0)

            def _do_export():
                export_dir = Path(self.project_manager.projects_dir).parent / 'exports'
                export_dir.mkdir(exist_ok=True)
                export_path = export_dir / f'{name}.zip'

                data = self.project_manager.export_project_json(name)
                if not data:
                    raise Exception('项目不存在')

                project_dir = self.project_manager._get_project_dir(name)

                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_project = Path(temp_dir) / name
                    temp_project.mkdir()

                    with open(temp_project / 'project.json', 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)

                    game_dir = project_dir / 'game'
                    if game_dir.exists():
                        shutil.copytree(game_dir, temp_project / 'game')

                    fonts_dir = Path(__file__).parent.parent.parent / 'fonts'
                    if fonts_dir.exists():
                        shutil.copytree(fonts_dir, temp_project / 'fonts')

                    with zipfile.ZipFile(export_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        for root, dirs, files in os.walk(temp_project):
                            for file in files:
                                file_path = Path(root) / file
                                arcname = file_path.relative_to(temp_dir)
                                zf.write(file_path, arcname)

                return export_path

            result_path = await loop.run_in_executor(None, _do_export)

            progress_bar.value = 1.0
            progress_label.text = f'✅ 导出成功: {result_path.name}'
            await asyncio.sleep(0.5)
            dialog.close()
            _safe(ui.notify, f'✅ 导出成功: {result_path}', type='positive')

        except Exception as e:
            self.logger.error(f'导出项目失败: {e}')
            try:
                progress_label.text = f'❌ 导出失败: {str(e)}'
            except Exception as ui_err:
                self.logger.warning(f'更新导出进度UI失败: {ui_err}')
            try:
                ui.notify(f'导出失败: {str(e)}', type='negative')
            except Exception as notify_err:
                self.logger.warning(f'发送导出失败通知失败: {notify_err}')
            await asyncio.sleep(2)
            try:
                dialog.close()
            except Exception as close_err:
                self.logger.warning(f'关闭导出进度对话框失败: {close_err}')

    # ========== 刷新导出包列表 ==========

    async def async_refresh_export_packages(self):
        """刷新已导出的项目包列表（文件系统操作在线程池中）"""
        loop = asyncio.get_event_loop()

        def _load_zips():
            exports_dir = Path(self.project_manager.projects_dir).parent / 'exports'
            if not exports_dir.exists():
                return []
            files = sorted(exports_dir.glob('*.zip'),
                           key=lambda f: f.stat().st_mtime, reverse=True)
            # 在线程池中读取文件大小，避免主线程阻塞
            return [(f, f.stat().st_size) for f in files]

        zip_infos = await loop.run_in_executor(None, _load_zips)

        self.export_packages_container.clear()
        with self.export_packages_container:
            if not zip_infos:
                ui.label('暂无导出包').props('caption')
                return

            for zf, file_size in zip_infos:
                size_mb = file_size / 1024 / 1024
                with ui.card().classes('w-full flat bordered q-mb-xs'):
                    with ui.row().classes('items-center full-width q-pa-sm'):
                        ui.icon('inventory_2').classes('text-grey-7')
                        with ui.column().classes('q-ml-sm'):
                            ui.label(zf.name).classes('text-body2')
                            ui.label(f'{size_mb:.1f} MB').props('caption')
                        ui.space()
                        with ui.row().classes('gap-xs'):
                            ui.button(icon='download',
                                      on_click=lambda p=str(zf): ui.download(p)).props('flat dense round size=sm')
                            ui.button(icon='folder_open',
                                      on_click=lambda p=str(zf.parent): os.startfile(p)).props('flat dense round size=sm')
