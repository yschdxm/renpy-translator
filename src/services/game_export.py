"""游戏导出服务（从 export_panel 抽取的纯逻辑，无 UI 依赖）

用法:
    exporter = GameExporter(project_manager, db, logger)
    result = exporter.export(project_name, log=print, progress=lambda v, t: None)
"""
import json as _json
import os
import re
import shutil
from pathlib import Path

from database import ProjectDatabase
from logger import TranslationLogger
from project_manager import ProjectManager

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


def escape_translation(text: str, percent: str = 'say') -> str:
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


class GameExporter:
    """导出翻译后的游戏为独立目录（阻塞式，调用方放 executor/线程）"""

    def __init__(self, project_manager: ProjectManager,
                 db: ProjectDatabase, logger: TranslationLogger):
        self.project_manager = project_manager
        self.db = db
        self.logger = logger

    def build_translation_dict(self) -> dict:
        """从库构建 原文 -> 译文 字典（导出与导出后自愈重填共用）"""
        translation_dict = {}
        for d in self.db.get_dialogues_page(0, 999999, filter_mode='translated')[0]:
            if d.get('translated_text'):
                translation_dict[d['original_text']] = d['translated_text']
        for u in self.db.get_ui_texts_page(0, 999999, filter_mode='translated')[0]:
            if u.get('translated_text'):
                translation_dict[u['original_text']] = u['translated_text']
        for c in self.db.get_characters():
            if c['cn_name'] and c['cn_name'].strip():
                translation_dict[c['display_name']] = c['cn_name']
        glossary = self.db.get_glossary()
        for en, cn in glossary.items():
            if cn and cn.strip():
                translation_dict[en] = cn
        return translation_dict

    def export(self, project_name: str, log, progress) -> dict:
        """执行导出。log(str) 写日志；progress(0~1, 阶段文本) 报进度。

        Returns: {'success': bool, 'message': str}
        """
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
            translation_dict = self.build_translation_dict()

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

            # 生成角色名翻译（Character("Name") 未包 _()，不在翻译系统内）
            progress(0.97, '正在生成角色名翻译...')
            n_count = self._write_character_names(export_dir, log)
            if n_count:
                log(f'角色名翻译: {n_count} 个角色')

            log('')
            log(f'导出目录: {export_dir}')

            return {'success': True, 'message': f'导出目录: {export_dir}'}

        except Exception as e:
            log(f'导出异常: {str(e)}')
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
                                    translated = escape_translation(translation_dict[text])
                                    new_lines.append(f'    {content_match.group(1)} "{translated}"')
                                    filled += 1
                                else:
                                    new_lines.append(lines[i + 1])
                                i += 2
                                continue
                            elif narration_match:
                                text = narration_match.group(1).replace('\\"', '"')
                                if text in translation_dict:
                                    translated = escape_translation(translation_dict[text])
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
                                    escaped = escape_translation(translated, percent='string')
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

    def _write_character_names(self, export_dir: Path, log) -> int:
        """生成角色名翻译文件，返回处理的角色变量数

        游戏里 Character("Name") 的名字若未用 _() 包装，不会进入 Ren'Py
        翻译系统（tl 模板中没有对应 old/new 行），人名表译文无从生效。
        这里扫描源码中的角色定义，生成 translate python 块：
        切到中文时覆盖 .name 为人名表译文，切回英文时还原。
        动态名（[chad_name] 等玩家可改名变量）跳过。
        """
        characters = self.db.get_characters()
        # 变量名 -> 中文名（角色身份按变量，同名显示名不会串）
        var_cn = {}
        for c in characters:
            cn = (c['cn_name'] or '').strip()
            var = (c['variable'] or '').strip()
            if cn and var:
                var_cn[var] = cn
        if not var_cn:
            return 0

        # 扫描源码中的角色定义：define 变量名 = Character("原名")
        define_re = re.compile(
            r'define\s+(\w+)\s*=\s*Character\(\s*["\']([^"\']+)["\']'
        )
        assigns = []  # (变量名, 原名, 中文名)
        seen = set()
        for rpy in (export_dir / 'game').rglob('*.rpy'):
            if 'tl' in rpy.parts:
                continue
            try:
                content = rpy.read_text(encoding='utf-8')
            except Exception:
                continue
            for m in define_re.finditer(content):
                var, en = m.group(1), m.group(2)
                cn = var_cn.get(var, '')
                # 跳过未翻译、译文同原文、动态名（[chad_name] 等玩家可改名变量）
                if cn and cn != en and '[' not in en and (var, en) not in seen:
                    seen.add((var, en))
                    assigns.append((var, en, cn))
        if not assigns:
            log('未在源码中匹配到角色定义，跳过角色名翻译')
            return 0

        lines = [
            '# 角色名翻译（导出工具自动生成）',
            '# Character() 名字未包 _()，不在翻译系统内，',
            '# 用 translate python 块在语言切换时覆盖/还原 .name。',
        ]
        for lang, use_cn in (('chinese', True), ('None', False)):
            lines.append(f'translate {lang} python:')
            for var, en, cn in assigns:
                name = (cn if use_cn else en).replace('"', '\\"')
                lines.append(f'    {var}.name = "{name}"')
            lines.append('')

        out = export_dir / 'game' / 'tl' / 'chinese' / 'zz_char_names.rpy'
        out.write_text('\n'.join(lines), encoding='utf-8')
        return len(assigns)

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
        from rt_home import find_resource
        fonts_dir = find_resource('fonts')
        if fonts_dir is None:
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
