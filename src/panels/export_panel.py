"""导出面板 - 导出翻译后的游戏"""

import asyncio
import os
import re
import shutil
from pathlib import Path
from queue import Queue
from nicegui import ui


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (RuntimeError, AttributeError):
        return None

from database import ProjectDatabase
from project_manager import ProjectManager
from logger import TranslationLogger
from components.log_panel import LogPanel


class ExportPanel:
    """导出面板"""

    def __init__(self, project_manager: ProjectManager, logger: TranslationLogger):
        self.project_manager = project_manager
        self.logger = logger
        self.db: ProjectDatabase = None

        self.stats_label: ui.label = None
        self.export_btn: ui.button = None
        self.log_panel: LogPanel = None

    def set_db(self, db: ProjectDatabase):
        self.db = db

    def create(self, container: ui.column):
        """创建面板"""
        with container:
            ui.label('📦 导出翻译后的游戏').classes('text-h5')
            ui.label('将翻译后的游戏导出为独立目录，可直接运行').classes('text-body1 text-grey')
            ui.separator()

            with ui.card().classes('w-full'):
                self.stats_label = ui.label('请先打开项目').classes('text-subtitle1')
                with ui.row().classes('gap-8'):
                    self.dialogue_stats = ui.label('对话翻译: -').classes('text-body1')
                    self.ui_stats = ui.label('字符串翻译: -').classes('text-body1')
                    self.name_stats = ui.label('人名翻译: -').classes('text-body1')

            ui.separator()

            with ui.row().classes('gap-2'):
                self.export_btn = ui.button(
                    '📦 开始导出', color='positive',
                    on_click=self._export_game
                ).classes('px-8')
                ui.button('🔄 刷新统计', on_click=self.refresh_stats)

            ui.separator()
            self.log_panel = LogPanel(height='h-64')
            self.log_panel.build_ui(container, label='导出日志')

            self.logger.bind_ui('export', self.log_panel.get_push_callback())

    def refresh(self):
        """同步刷新"""
        self._do_refresh_stats()

    async def async_refresh(self):
        """异步刷新（非阻塞）"""
        if not self.db:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._do_refresh_stats)

    def refresh_stats(self):
        """刷新统计（同步）"""
        self._do_refresh_stats()

    def _do_refresh_stats(self):
        """实际刷新统计逻辑（同步，在线程池中调用）"""
        if not self.db:
            return

        d_count = self.db.get_dialogue_count()
        u_count = self.db.get_ui_text_count()
        n_count = self.db.get_char_dict_count()

        _safe(setattr, self.dialogue_stats, 'text', f'对话翻译: {d_count["translated"]}/{d_count["total"]}')
        _safe(setattr, self.ui_stats, 'text', f'字符串翻译: {u_count["translated"]}/{u_count["total"]}')
        _safe(setattr, self.name_stats, 'text', f'人名翻译: {n_count["translated"]}/{n_count["total"]}')

        total = d_count['total'] + u_count['total'] + n_count['total']
        translated = d_count['translated'] + u_count['translated'] + n_count['translated']
        percent = (translated / total * 100) if total > 0 else 0
        self.stats_label.text = f'📊 总体进度: {translated}/{total} ({percent:.1f}%)'

    async def _export_game(self):
        """导出翻译后的游戏"""
        if not self.db:
            _safe(ui.notify, '请先打开项目', type='warning')
            return

        loop = asyncio.get_event_loop()
        d_count = await loop.run_in_executor(None, self.db.get_dialogue_count)
        if d_count['translated'] == 0:
            _safe(ui.notify, '没有已翻译的内容可导出', type='warning')
            return

        _safe(self.export_btn.disable)
        _safe(setattr, self.export_btn, 'text', '导出中...')
        await self._do_export()

    async def _do_export(self):
        """异步导出"""
        loop = asyncio.get_event_loop()
        log_queue = Queue()

        try:
            self.log_panel.clear()
            self.logger.info('开始导出游戏...', panel='export')

            async def process_log_queue():
                while True:
                    try:
                        msg = log_queue.get_nowait()
                        if msg == '__DONE__':
                            break
                        self.logger.info(msg, panel='export')
                    except Exception as e:
                        # queue.Empty 是正常的轮询空队列，其他异常需要记录
                        if 'Empty' not in type(e).__name__:
                            self.logger.warning(f'日志队列处理异常: {e}')
                    await asyncio.sleep(0.1)

            log_task = asyncio.create_task(process_log_queue())

            meta = await loop.run_in_executor(None, self.db.get_all_meta)
            project_name = meta.get('name', 'unknown')

            result = await loop.run_in_executor(
                None,
                lambda: self._export_thread(project_name, log_queue)
            )

            try:
                await asyncio.wait_for(log_task, timeout=10.0)
            except asyncio.TimeoutError:
                log_task.cancel()

            if result['success']:
                self.logger.info('✅ 导出完成！', panel='export')
            else:
                self.logger.error(f'❌ 导出失败: {result["message"]}', panel='export')

        except Exception as e:
            self.logger.error(f'❌ 导出异常: {str(e)}', panel='export')

        finally:
            _safe(self.export_btn.enable)
            _safe(setattr, self.export_btn, 'text', '📦 开始导出')

    def _export_thread(self, project_name: str, log_queue: Queue) -> dict:
        """在线程中执行导出"""
        def log(msg):
            log_queue.put(msg)

        try:
            project_dir = self.project_manager._get_project_dir(project_name)
            game_work_dir = project_dir / 'game'
            export_dir = project_dir / 'output'

            # 清理旧输出
            if export_dir.exists():
                log('清理旧的输出目录...')
                shutil.rmtree(export_dir)

            # 复制游戏文件
            log('复制游戏文件...')
            shutil.copytree(game_work_dir, export_dir)
            log('游戏文件复制完成')

            # 构建翻译字典
            translation_dict = {}

            # 对话翻译
            dialogues = self.db.get_dialogues_page(0, 999999, filter_mode='translated')[0]
            for d in dialogues:
                if d.get('translated_text'):
                    translation_dict[d['original_text']] = d['translated_text']

            # UI 字符串翻译
            ui_texts = self.db.get_ui_texts_page(0, 999999, filter_mode='translated')[0]
            for u in ui_texts:
                if u.get('translated_text'):
                    translation_dict[u['original_text']] = u['translated_text']

            # 人名翻译
            characters = self.db.get_characters()
            for c in characters:
                if c['cn_name'] and c['cn_name'].strip():
                    translation_dict[c['display_name']] = c['cn_name']

            # 术语表
            glossary = self.db.get_glossary()
            for en, cn in glossary.items():
                if cn and cn.strip():
                    translation_dict[en] = cn

            log(f'翻译字典: {len(translation_dict)} 条')

            # 填充对话翻译
            tl_dir = export_dir / 'game' / 'tl' / 'chinese'
            if tl_dir.exists():
                log('填充对话翻译...')
                d_count = self._fill_dialogue(tl_dir, translation_dict)
                log(f'对话翻译: {d_count} 条')

                log('填充字符串翻译...')
                u_count = self._fill_strings(tl_dir, translation_dict)
                log(f'字符串翻译: {u_count} 条')

            # 添加语言选择
            log('添加语言选择界面...')
            self._add_language_selector(export_dir, log)

            # 添加中文字体
            log('添加中文字体支持...')
            self._add_chinese_font(export_dir, log)

            log('')
            log(f'导出目录: {export_dir}')

            log_queue.put('__DONE__')
            return {'success': True, 'message': f'导出目录: {export_dir}'}

        except Exception as e:
            log(f'导出异常: {str(e)}')
            log_queue.put('__DONE__')
            return {'success': False, 'message': str(e)}

    def _fill_dialogue(self, tl_dir: Path, translation_dict: dict) -> int:
        """填充对话翻译"""
        filled = 0
        for tl_file in tl_dir.rglob('*.rpy'):
            try:
                with open(tl_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                lines = content.split('\n')
                new_lines = []
                i = 0
                while i < len(lines):
                    line = lines[i]
                    comment_match = re.match(r'^\s+#\s+(.*)', line)
                    if comment_match:
                        comment_text = comment_match.group(1).strip()
                        if not comment_text or comment_text.startswith('game/'):
                            new_lines.append(line)
                            i += 1
                            continue

                        if i + 1 < len(lines) and re.match(r'^\s+#\s+(.*)', lines[i + 1]):
                            if re.match(r'^\s+#\s+(.*)', lines[i + 1]).group(1).strip() == comment_text:
                                new_lines.append(line)
                                i += 1
                                continue

                        new_lines.append(line)

                        if i + 1 < len(lines):
                            content_match = re.match(r'^\s+(\w+)\s+"(.*)"', lines[i + 1])
                            narration_match = re.match(r'^\s+"(.*)"', lines[i + 1])

                            if content_match:
                                text = content_match.group(2).replace('\\"', '"')
                                if text in translation_dict:
                                    translated = translation_dict[text].replace('"', '\\"')
                                    new_lines.append(f'    {content_match.group(1)} "{translated}"')
                                    filled += 1
                                else:
                                    new_lines.append(lines[i + 1])
                                i += 2
                                continue
                            elif narration_match:
                                text = narration_match.group(1).replace('\\"', '"')
                                if text in translation_dict:
                                    translated = translation_dict[text].replace('"', '\\"')
                                    new_lines.append(f'    "{translated}"')
                                    filled += 1
                                else:
                                    new_lines.append(lines[i + 1])
                                i += 2
                                continue

                    new_lines.append(line)
                    i += 1

                with open(tl_file, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(new_lines))

            except Exception as e:
                self.logger.error(f'填充失败 {tl_file.name}: {e}', panel='export')

        return filled

    def _fill_strings(self, tl_dir: Path, translation_dict: dict) -> int:
        """填充字符串翻译"""
        filled = 0
        for tl_file in tl_dir.rglob('*.rpy'):
            try:
                with open(tl_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                lines = content.split('\n')
                new_lines = []
                i = 0
                while i < len(lines):
                    line = lines[i]
                    old_match = re.match(r'^\s+old\s+"(.*)"\s*$', line)
                    if old_match:
                        old_text = old_match.group(1).replace('\\"', '"')
                        new_lines.append(line)
                        translated = translation_dict.get(old_text)

                        if i + 1 < len(lines):
                            new_match = re.match(r'^\s+new\s+"(.*)"\s*$', lines[i + 1])
                            if new_match:
                                if translated:
                                    escaped = translated.replace('"', '\\"')
                                    new_lines.append(f'    new "{escaped}"')
                                    filled += 1
                                else:
                                    new_lines.append(lines[i + 1])
                                i += 2
                                continue

                    new_lines.append(line)
                    i += 1

                with open(tl_file, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(new_lines))

            except Exception as e:
                self.logger.error(f'填充失败 {tl_file.name}: {e}', panel='export')

        return filled

    def _add_language_selector(self, export_dir: Path, log):
        """添加语言选择界面"""
        possible = [
            export_dir / 'game' / 'scripts' / 'screens.rpy',
            export_dir / 'game' / 'screens.rpy',
        ]

        source = None
        for p in possible:
            if p.exists():
                source = p
                break

        if not source:
            log('未找到 screens.rpy，跳过语言选择')
            return

        with open(source, 'r', encoding='utf-8') as f:
            content = f.read()

        if 'Language("chinese")' in content:
            log('语言选择已存在')
            return

        target = '            null height (4 * gui.pref_spacing)'
        if target not in content:
            log('未找到插入位置')
            return

        block = '''            vbox:
                label _("Language")
                textbutton "English" action Language(None)
                textbutton "中文" action Language("chinese")

'''
        content = content.replace(target, block + target)

        with open(source, 'w', encoding='utf-8') as f:
            f.write(content)

        log('已添加语言选择')

    def _add_chinese_font(self, export_dir: Path, log):
        """添加中文字体支持"""
        fonts_dir = Path(__file__).parent.parent.parent / 'fonts'
        if not fonts_dir.exists():
            log('未找到字体目录')
            return

        font_files = [f for f in fonts_dir.iterdir()
                      if f.suffix.lower() in ['.ttf', '.ttc', '.otf']]
        if not font_files:
            log('字体目录为空')
            return

        dest = export_dir / 'game' / 'fonts'
        dest.mkdir(exist_ok=True)

        for f in font_files:
            try:
                shutil.copy2(f, dest / f.name)
            except Exception as e:
                log(f'复制字体失败: {e}')

        # 配置 gui.rpy
        font_name = font_files[0].name
        font_path = f'fonts/{font_name}'

        gui_paths = [
            export_dir / 'game' / 'scripts' / 'gui.rpy',
            export_dir / 'game' / 'gui.rpy',
        ]

        for gui_file in gui_paths:
            if gui_file.exists():
                with open(gui_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                content = re.sub(
                    r'(define gui\.text_font\s*=\s*)"[^"]*"',
                    lambda m: f'{m.group(1)}"{font_path}"', content
                )
                content = re.sub(
                    r'(define gui\.name_text_font\s*=\s*)"[^"]*"',
                    lambda m: f'{m.group(1)}"{font_path}"', content
                )
                content = re.sub(
                    r'(define gui\.interface_text_font\s*=\s*)"[^"]*"',
                    lambda m: f'{m.group(1)}"{font_path}"', content
                )

                with open(gui_file, 'w', encoding='utf-8') as f:
                    f.write(content)

                log(f'已配置 {gui_file.name}')
                break
