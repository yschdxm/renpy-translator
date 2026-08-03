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


# Ren'Py 对台词和菜单选项做 % 格式化（sayexports/menuexports 中的
# what % tag_quoting_dict），裸 % 会抛 ValueError，必须写成 %%。
# 合法 %(name)s 变量和已转义的 %% 需要保留。
_SAY_PROTECT_RE = re.compile(
    r'%%|%\(\w+\)[#0\- +]*(?:\d+)?(?:\.\d+)?[diouxXeEfFgGcrs]'
)

# UI 字符串是混合场景：菜单选项同样被 % 格式化（裸 % 要转义），
# 但 strftime/代码格式化串（%m月%d日、存档位 %s、%(n)d 等）的 % 必须保留，
# 否则日期/档位号显示会坏。规则：% 后跟 ASCII 字母或 ( 的保留，其余转义。
_STRING_PROTECT_RE = re.compile(
    r'%%|%\(\w+\)[#0\- +]*(?:\d+)?(?:\.\d+)?[diouxXeEfFgGcrs]|%[a-zA-Z]'
)


def _escape_translation(text: str, percent: str = 'say') -> str:
    """转义译文中的特殊字符，保证写入 .rpy 后是合法且可运行的字符串

    - 真实换行符 → \\n 转义序列（Ren'Py 字符串不支持跨行，换行会破坏解析）
    - 裸 % → %%（Ren'Py 对台词/菜单选项做 % 格式化，裸 % 会 ValueError）
      percent='say'：台词，仅保留 %% 与 %(name)s 变量
      percent='string'：UI 字符串，额外保留 strftime/%s 等代码格式符
    - 双引号 → \\"
    译文里已有的 \\n（反斜杠+n 两字符）、%%、%(name)s 变量不受影响。
    """
    text = text.replace('\r', '').replace('\n', '\\n')

    # % 转义：先保护合法占位，再把剩余裸 % 变 %%，最后还原
    protect_re = _SAY_PROTECT_RE if percent == 'say' else _STRING_PROTECT_RE
    protected = []

    def _protect(m):
        protected.append(m.group(0))
        return f'\x00{len(protected) - 1}\x00'

    text = protect_re.sub(_protect, text)
    text = text.replace('%', '%%')
    for i, p in enumerate(protected):
        text = text.replace(f'\x00{i}\x00', p)

    return text.replace('"', '\\"')

from database import ProjectDatabase
from project_manager import ProjectManager
from logger import TranslationLogger


class ExportPanel:
    """导出面板"""

    def __init__(self, project_manager: ProjectManager, logger: TranslationLogger):
        self.project_manager = project_manager
        self.logger = logger
        self.db: ProjectDatabase = None

        self.stats_label: ui.label = None
        self.export_btn: ui.button = None
        self.log_panel = None

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

        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label('📦 导出游戏').classes('text-h6')
            progress_bar = ui.linear_progress(value=0, show_value=True).classes('w-full')
            progress_label = ui.label('准备中...').classes('text-caption')
        dialog.props('persistent')
        dialog.open()

        await self._do_export(dialog, progress_bar, progress_label)

    async def _do_export(self, dialog, progress_bar, progress_label):
        """异步导出"""
        loop = asyncio.get_event_loop()
        msg_queue = Queue()

        try:
            self.logger.info('开始导出游戏...', panel='export')

            async def process_queue():
                while True:
                    try:
                        msg = msg_queue.get_nowait()
                        if msg == '__DONE__':
                            break
                        if msg[0] == 'progress':
                            progress_bar.value = msg[1]
                            progress_label.text = msg[2]
                        else:
                            self.logger.info(msg[1], panel='export')
                    except Exception as e:
                        # queue.Empty 是正常的轮询空队列，其他异常需要记录
                        if 'Empty' not in type(e).__name__:
                            self.logger.warning(f'日志队列处理异常: {e}')
                    await asyncio.sleep(0.1)

            queue_task = asyncio.create_task(process_queue())

            meta = await loop.run_in_executor(None, self.db.get_all_meta)
            project_name = meta.get('name', 'unknown')

            result = await loop.run_in_executor(
                None,
                lambda: self._export_thread(project_name, msg_queue)
            )

            try:
                await asyncio.wait_for(queue_task, timeout=10.0)
            except asyncio.TimeoutError:
                queue_task.cancel()

            if result['success']:
                progress_bar.value = 1.0
                progress_label.text = '✅ 导出完成！'
                self.logger.info('✅ 导出完成！', panel='export')
                await asyncio.sleep(2)
            else:
                progress_label.text = f'❌ 导出失败: {result["message"]}'
                self.logger.error(f'❌ 导出失败: {result["message"]}', panel='export')
                await asyncio.sleep(5)

        except Exception as e:
            progress_label.text = f'❌ 导出异常: {str(e)}'
            self.logger.error(f'❌ 导出异常: {str(e)}', panel='export')
            await asyncio.sleep(5)

        finally:
            dialog.close()
            _safe(self.export_btn.enable)
            _safe(setattr, self.export_btn, 'text', '📦 开始导出')

    def _export_thread(self, project_name: str, msg_queue: Queue) -> dict:
        """在线程中执行导出

        通过队列回报两种消息：('log', 文本) 写入日志面板；
        ('progress', 0~1, 阶段文本) 更新进度条。
        """
        def log(msg):
            msg_queue.put(('log', msg))

        def progress(value, text):
            msg_queue.put(('progress', value, text))

        try:
            project_dir = self.project_manager._get_project_dir(project_name)
            game_work_dir = project_dir / 'game'
            export_dir = project_dir / 'output'

            # 清理旧输出
            if export_dir.exists():
                progress(0.01, '正在清理旧的输出目录...')
                log('清理旧的输出目录...')
                shutil.rmtree(export_dir)

            # 复制游戏文件（逐文件复制，带进度）
            progress(0.02, '正在复制游戏文件...')
            log('复制游戏文件...')
            total = sum(1 for p in game_work_dir.rglob('*') if p.is_file()) or 1
            copied = 0
            for root, dirs, files in os.walk(game_work_dir):
                rel_root = Path(root).relative_to(game_work_dir)
                dst_root = export_dir / rel_root
                dst_root.mkdir(parents=True, exist_ok=True)
                for f in files:
                    shutil.copy2(Path(root) / f, dst_root / f)
                    copied += 1
                    if copied % 20 == 0 or copied == total:
                        progress(0.02 + (copied / total) * 0.48,
                                 f'正在复制游戏文件... ({copied}/{total})')
            progress(0.5, '游戏文件复制完成')
            log('游戏文件复制完成')

            # 移除反编译生成的 .rpy：Ren'Py 会优先加载 .rpy 而非原始 .rpyc，
            # 反编译代码仅供解析用，导出时删除以保证游戏运行原始编译代码
            decompiled_meta = self.db.get_meta('decompiled_rpy_files')
            if decompiled_meta:
                import json as _json
                try:
                    removed = 0
                    for rel in _json.loads(decompiled_meta):
                        target = export_dir / rel
                        if target.exists():
                            target.unlink()
                            removed += 1
                    if removed:
                        log(f'已移除 {removed} 个反编译产生的 .rpy（游戏将运行原始 .rpyc）')
                except Exception as e:
                    log(f'清理反编译文件失败: {e}')

            # 构建翻译字典
            progress(0.55, '正在构建翻译字典...')
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
                d_count = self._fill_dialogue(
                    tl_dir, translation_dict,
                    lambda c, t: progress(0.55 + (c / t) * 0.2,
                                          f'正在填充对话翻译... ({c}/{t})'))
                log(f'对话翻译: {d_count} 条')

                log('填充字符串翻译...')
                u_count = self._fill_strings(
                    tl_dir, translation_dict,
                    lambda c, t: progress(0.75 + (c / t) * 0.1,
                                          f'正在填充字符串翻译... ({c}/{t})'))
                log(f'字符串翻译: {u_count} 条')

            # 添加语言选择
            progress(0.9, '正在添加语言选择界面...')
            log('添加语言选择界面...')
            self._add_language_selector(export_dir, log)

            # 添加中文字体
            progress(0.95, '正在添加中文字体支持...')
            log('添加中文字体支持...')
            self._add_chinese_font(export_dir, log)

            log('')
            log(f'导出目录: {export_dir}')

            msg_queue.put('__DONE__')
            return {'success': True, 'message': f'导出目录: {export_dir}'}

        except Exception as e:
            log(f'导出异常: {str(e)}')
            msg_queue.put('__DONE__')
            return {'success': False, 'message': str(e)}

    def _fill_dialogue(self, tl_dir: Path, translation_dict: dict, progress_cb=None) -> int:
        """填充对话翻译，progress_cb(已处理文件数, 总文件数) 回报进度"""
        filled = 0
        files = list(tl_dir.rglob('*.rpy'))
        total = len(files) or 1
        for file_idx, tl_file in enumerate(files, 1):
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
                        # 跳过源文件位置注释（# game/xxx.rpy:123、# renpy/common/00sync.rpy:305 等），
                        # 它们后面跟的是 old/new 行而不是对话，不能按对话处理
                        if (not comment_text or comment_text.startswith('game/')
                                or re.match(r'^[\w./-]+\.rpym?:\d+', comment_text)):
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

                            # old/new 是 strings 块的行，绝不能当对话发言处理
                            if content_match and content_match.group(1) not in ('old', 'new'):
                                text = content_match.group(2).replace('\\"', '"')
                                if text in translation_dict:
                                    translated = _escape_translation(translation_dict[text])
                                    new_lines.append(f'    {content_match.group(1)} "{translated}"')
                                    filled += 1
                                else:
                                    new_lines.append(lines[i + 1])
                                i += 2
                                continue
                            elif narration_match:
                                text = narration_match.group(1).replace('\\"', '"')
                                if text in translation_dict:
                                    translated = _escape_translation(translation_dict[text])
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

            if progress_cb:
                progress_cb(file_idx, total)

        return filled

    def _fill_strings(self, tl_dir: Path, translation_dict: dict, progress_cb=None) -> int:
        """填充字符串翻译，progress_cb(已处理文件数, 总文件数) 回报进度"""
        filled = 0
        files = list(tl_dir.rglob('*.rpy'))
        total = len(files) or 1
        for file_idx, tl_file in enumerate(files, 1):
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
                                    escaped = _escape_translation(translated, percent='string')
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

            if progress_cb:
                progress_cb(file_idx, total)

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

        # 写入字体映射：游戏里写死的字体引用（如 font "DejaVuSans.ttf"）不走
        # gui.text_font，中文会渲染成方块。font_replacement_map 在字体加载层
        # 全局替换 Ren'Py 内置 DejaVuSans 系列，覆盖所有写死的引用。
        tl_chinese_dir = export_dir / 'game' / 'tl' / 'chinese'
        if tl_chinese_dir.exists():
            override_file = tl_chinese_dir / 'font_override.rpy'
            lines = [
                '# 中文字体映射（导出工具自动生成）',
                "# 将 Ren'Py 内置 DejaVuSans 系列映射到中文字体，",
                '# 覆盖游戏中写死 font "DejaVuSans.ttf" 等引用的样式。',
                'init python:',
            ]
            for base in ('DejaVuSans.ttf', 'DejaVuSans-Bold.ttf',
                         'DejaVuSans-Oblique.ttf', 'DejaVuSans-BoldOblique.ttf'):
                for bold in (False, True):
                    for italic in (False, True):
                        lines.append(
                            f'    config.font_replacement_map["{base}", {bold}, {italic}] = '
                            f'("{font_path}", False, False)'
                        )
            with open(override_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
            log(f'已写入字体映射: tl/chinese/{override_file.name}')
