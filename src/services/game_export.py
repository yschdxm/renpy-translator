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


class ExportCancelled(Exception):
    """导出被取消（协作式：循环内检查 cancel_event 触发）"""


def zip_directory(src_dir: Path, zip_path: Path, progress=None,
                  cancel_event=None):
    """把目录打成 zip（内容含目录结构），progress(0~1, 文本) 逐文件回报"""
    import zipfile
    files = [p for p in src_dir.rglob('*') if p.is_file()]
    total = len(files) or 1
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, p in enumerate(files, 1):
            if cancel_event is not None and cancel_event.is_set():
                raise ExportCancelled()
            zf.write(p, p.relative_to(src_dir))
            if progress and (i % 10 == 0 or i == total):
                progress(i / total, f'正在打包... ({i}/{total})')


class GameExporter:
    """导出翻译后的游戏为独立目录（阻塞式，调用方放 executor/线程）"""

    def __init__(self, project_manager: ProjectManager,
                 db: ProjectDatabase, logger: TranslationLogger):
        self.project_manager = project_manager
        self.db = db
        self.logger = logger
        self._cancel_event = None  # threading.Event，由 export() 注入

    def _check_cancel(self):
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise ExportCancelled()

    def build_translation_dict(self) -> dict:
        """从库构建 原文 -> 译文 字典（导出与导出后自愈重填共用）"""
        # 只取已翻译条目的原文/译文两列，绕开分页拉全量
        translation_dict = dict(self.db.iter_translated_pairs())
        for c in self.db.get_characters():
            if c['cn_name'] and c['cn_name'].strip():
                translation_dict[c['display_name']] = c['cn_name']
        glossary = self.db.get_glossary()
        for en, cn in glossary.items():
            if cn and cn.strip():
                translation_dict[en] = cn
        return translation_dict

    def export(self, project_name: str, log, progress,
               export_dir: Path = None, cancel_event=None) -> dict:
        """执行导出。log(str) 写日志；progress(0~1, 阶段文本) 报进度。

        export_dir: 导出目标目录（调用方给临时目录，打包后清理）；
        缺省为项目 output/（兼容旧调用）。
        cancel_event: threading.Event，置位时长循环抛 ExportCancelled。
        Returns: {'success': bool, 'message': str}
        """
        self._cancel_event = cancel_event
        try:
            self._check_cancel()
            project_dir = self.project_manager.project_dir(project_name)
            game_work_dir = project_dir / 'game'
            export_dir = Path(export_dir) if export_dir else project_dir / 'output'

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
                    self._check_cancel()
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
                    decompiled_rels = _json.loads(decompiled_meta)
                    # 例外保留（其余反编译产物照删）：
                    # 1. 可注入语言按钮的反编译 screens.rpy（C2）
                    # 2. 含内嵌标记 _() 的反编译文件——删掉它们游戏就跑原始
                    #    rpyc，_() 查找不存在，内嵌译文永远不生效
                    # 保留的文件由导出后编译校验兜底：报错则按文件放弃并重导出
                    keep = self._pick_kept_decompiled(export_dir, decompiled_rels)
                    self.db.set_meta('kept_decompiled_files',
                                     _json.dumps(keep))

                    removed = 0
                    for rel in decompiled_rels:
                        if rel in keep:
                            continue
                        target = export_dir / rel
                        if target.exists():
                            target.unlink()
                            removed += 1
                    if removed:
                        log(f'已移除 {removed} 个反编译产生的 .rpy（游戏将运行原始 .rpyc）')
                    if keep:
                        log(f'保留 {len(keep)} 个反编译文件'
                            f'（语言切换/内嵌译文载体，编译校验兜底）: '
                            f'{", ".join(keep)}')
                except Exception as e:
                    log(f'清理反编译文件失败: {e}')
            else:
                self.db.set_meta('kept_decompiled_files', '[]')

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

            # 默认以中文启动（rpyc-only 游戏没有切换按钮也无需手动切换）
            self._set_default_language(export_dir, log)

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

        except ExportCancelled:
            # 取消优先于兜底：不能吞成导出失败
            raise
        except Exception as e:
            log(f'导出异常: {str(e)}')
            return {'success': False, 'message': str(e)}

    def _rewrite_tl_files(self, tl_dir: Path, line_handler, progress_cb=None) -> int:
        """填充骨架：rglob 所有 .rpy → 读入 → 行扫描状态机改写 → 写回 → 进度回调。

        line_handler(lines) -> (new_lines, 本文件填充条数)：行匹配规则由
        调用方提供（对话发言 / strings old-new 两种）。
        progress_cb(已处理文件数, 总文件数) 回报进度。返回总填充条数。
        """
        filled = 0
        files = list(tl_dir.rglob('*.rpy'))
        total = len(files) or 1
        for file_idx, tl_file in enumerate(files, 1):
            self._check_cancel()
            try:
                with open(tl_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                new_lines, file_filled = line_handler(content.split('\n'))
                filled += file_filled

                with open(tl_file, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(new_lines))

            except Exception as e:
                self.logger.error(f'填充失败 {tl_file.name}: {e}', panel='export')

            if progress_cb:
                progress_cb(file_idx, total)

        return filled

    def _fill_dialogue(self, tl_dir: Path, translation_dict: dict, progress_cb=None) -> int:
        """填充对话翻译，progress_cb(已处理文件数, 总文件数) 回报进度"""

        def _handle(lines):
            filled = 0
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

            return new_lines, filled

        return self._rewrite_tl_files(tl_dir, _handle, progress_cb)

    def _fill_strings(self, tl_dir: Path, translation_dict: dict, progress_cb=None) -> int:
        """填充字符串翻译，progress_cb(已处理文件数, 总文件数) 回报进度"""

        def _handle(lines):
            filled = 0
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

            return new_lines, filled

        return self._rewrite_tl_files(tl_dir, _handle, progress_cb)

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

    _ANCHOR_NULL_HEIGHT_RE = re.compile(r'^\s*null height\b')
    _ANCHOR_HBOX_RE = re.compile(r'^\s*hbox:\s*(?:#.*)?$')
    _SCREEN_PREFERENCES_RE = re.compile(r'^screen\s+preferences\s*\(')

    def _screens_candidates(self, export_dir: Path) -> list:
        """screens.rpy 候选路径：常见位置优先，game/** 下其余同名文件兜底
        （排除 tl/ 翻译目录）"""
        game = export_dir / 'game'
        candidates = [game / 'screens.rpy', game / 'scripts' / 'screens.rpy']
        if game.exists():
            candidates += sorted(
                p for p in game.rglob('screens.rpy')
                if 'tl' not in p.relative_to(game).parts
                and p not in candidates)
        return [p for p in candidates if p.is_file()]

    def _find_injectable_screens(self, export_dir: Path):
        """找可注入语言按钮的 screens.rpy。

        返回 (path, lines, insert_index)：lines 为 None 表示该文件已注入过；
        所有候选都不可注入（无 screen preferences 块或无锚点）返回 None。
        """
        for path in self._screens_candidates(export_dir):
            try:
                lines = path.read_text(encoding='utf-8').split('\n')
            except OSError:
                continue
            if any('Language("chinese")' in l for l in lines):
                return path, None, None

            # 定位 screen preferences 块：顶级语句到下一个顶级语句之间
            start = next((i for i, l in enumerate(lines)
                          if self._SCREEN_PREFERENCES_RE.match(l)), None)
            if start is None:
                continue
            end = next(
                (i for i in range(start + 1, len(lines))
                 if lines[i] and not lines[i].startswith((' ', '\t', '#'))),
                len(lines))
            block = lines[start:end]

            # 语义锚点：模板里 radio/check 与 slider 两组设置之间的间隔
            anchor = next((i for i, l in enumerate(block)
                           if self._ANCHOR_NULL_HEIGHT_RE.match(l)
                           and 'pref_spacing' in l), None)
            if anchor is None:
                anchor = next(
                    (i for i, l in enumerate(block)
                     if self._ANCHOR_HBOX_RE.match(l)
                     and any('slider' in b and 'style_prefix' in b
                             for b in block[i + 1:i + 4])), None)
            if anchor is None:
                continue
            return path, lines, start + anchor
        return None

    def _pick_kept_decompiled(self, export_dir: Path,
                              decompiled_rels: list) -> list:
        """从反编译产物中挑出必须随导出保留的文件，返回相对路径列表。

        保留两类：
        - 可注入语言按钮的 screens.rpy（rpyc-only 游戏的切换入口，C2）
        - 含内嵌标记 _() 的文件——删除后游戏跑原始 rpyc，_() 查找不存在，
          内嵌译文全部失效

        dropped_decompiled_files 记录被自愈判定编译失败而放弃的文件，
        永久排除。保留的文件由导出后编译校验兜底。
        """
        rels_set = set(decompiled_rels)
        dropped = set(_json.loads(
            self.db.get_meta('dropped_decompiled_files') or '[]'))

        keep = set()

        found = self._find_injectable_screens(export_dir)
        if found and found[1] is not None:
            rel = found[0].relative_to(export_dir).as_posix()
            if rel in rels_set:
                keep.add(rel)

        # 内嵌候选的 rel_file 以 game/game/ 为基准（resolve_source_root），
        # 反编译清单以项目 game/ 为基准，差一层 'game/' 前缀
        for row in self.db.get_marked_embedded():
            rel = row['rel_file']
            if not rel.startswith('game/'):
                rel = 'game/' + rel
            if rel in rels_set:
                keep.add(rel)

        return sorted(keep - dropped)

    def _add_language_selector(self, export_dir: Path, log):
        """添加语言选择界面。

        结构化注入：定位 screen preferences 块，在块内找语义锚点
        （radio/check 与 slider 两组设置之间的 null height 间隔，
        或 slider hbox 本身），继承锚点缩进插入。比整行精确匹配
        耐模板改动；找不到可注入目标时响亮告警。
        """
        found = self._find_injectable_screens(export_dir)
        if found is None:
            log('警告：未找到可注入的 screen preferences，'
                '跳过语言切换按钮（游戏将以默认语言启动）')
            return

        source, lines, insert_at = found
        if lines is None:
            log('语言选择已存在')
            return

        anchor_line = lines[insert_at]
        indent = anchor_line[:len(anchor_line) - len(anchor_line.lstrip())]
        insert = [
            f'{indent}vbox:',
            f'{indent}    label _("Language")',
            f'{indent}    textbutton "English" action Language(None)',
            f'{indent}    textbutton "中文" action Language("chinese")',
            '',
        ]
        lines[insert_at:insert_at] = insert

        with open(source, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        log(f'已添加语言选择（{source.relative_to(export_dir)}）')

    def _set_default_language(self, export_dir: Path, log):
        """默认以中文启动（追加到已有的 tl/chinese 骨架文件，不新增文件）。

        仅首次生效：玩家之后用 Language 按钮切换的选择会被保留
        （persistent 标记记录是否已应用过默认值；Language(None) 会把
        _preferences.language 重置为 None，不能仅靠它判断是否首次）。
        """
        tl_dir = export_dir / 'game' / 'tl' / 'chinese'
        if not tl_dir.exists():
            log('警告：没有 tl/chinese 目录，跳过默认语言设置')
            return

        files = list(tl_dir.rglob('*.rpy'))
        if not files:
            log('警告：tl/chinese 下没有骨架文件，跳过默认语言设置')
            return

        marker = '_rt_default_lang'
        for p in files:
            try:
                if marker in p.read_text(encoding='utf-8', errors='ignore'):
                    log('默认语言已设置')
                    return
            except OSError:
                pass

        target = next(
            (p for p in files if p.name == 'common.rpy'),
            next((p for p in files if p.name == 'screens.rpy'), files[0]))
        block = (
            '\n\n# 默认以中文启动（导出工具自动生成）。仅首次生效，\n'
            '# 玩家之后在设置界面切换的语言选择会被保留。\n'
            'init -10 python:\n'
            f'    if not getattr(persistent, "{marker}", False):\n'
            f'        persistent.{marker} = True\n'
            '        if _preferences.language is None:\n'
            '            _preferences.language = "chinese"\n'
        )
        with open(target, 'a', encoding='utf-8') as f:
            f.write(block)

        log(f'已设置默认中文启动（追加到 {target.name}）')

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
        # 全局替换，覆盖所有写死的引用。游戏自带字体（通常不含 CJK 字形，
        # 不映射中文直接不显示）按 相对路径+文件名 两种写法一起映射。
        tl_chinese_dir = export_dir / 'game' / 'tl' / 'chinese'
        if tl_chinese_dir.exists():
            font_keys = {
                'DejaVuSans.ttf', 'DejaVuSans-Bold.ttf',
                'DejaVuSans-Oblique.ttf', 'DejaVuSans-BoldOblique.ttf',
            }
            game_dir = export_dir / 'game'
            own_fonts = {f'fonts/{f.name}' for f in font_files}
            for f in sorted(game_dir.rglob('*')):
                if f.suffix.lower() not in ('.ttf', '.ttc', '.otf'):
                    continue
                rel = f.relative_to(game_dir).as_posix()
                if rel in own_fonts or rel.startswith('tl/'):
                    continue
                font_keys.add(rel)    # 代码里写相对路径的引用
                font_keys.add(f.name)  # 只写文件名的引用

            override_file = tl_chinese_dir / 'font_override.rpy'
            lines = [
                '# 中文字体映射（导出工具自动生成）',
                "# 将 Ren'Py 内置 DejaVuSans 系列与游戏自带字体映射到中文字体，",
                '# 游戏自带字体通常不含 CJK 字形，不映射中文会渲染成空白。',
                'init python:',
            ]
            for key in sorted(font_keys):
                for bold in (False, True):
                    for italic in (False, True):
                        lines.append(
                            f'    config.font_replacement_map["{key}", {bold}, {italic}] = '
                            f'("{font_path}", False, False)'
                        )
            with open(override_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
            log(f'已写入字体映射: tl/chinese/{override_file.name}'
                f'（{len(font_keys)} 个字体引用）')
