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
import markup_check

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

# 旧版内嵌包装 bug 的遗留：f"..." 被在引号处插入 _() 变成 f_("...")。
# 这在语法上是"调用函数 f_"，编译校验拦不住，运行时才 NameError。
# 扫描端已修（前缀字面量不再成为候选），但旧工作副本里已写入的坏包装
# 会随复制带进导出包——导出前按模式机械还原（f_("abc") -> f"abc"）。
_LEGACY_WRAP_FIND_RE = re.compile(r'\b[frbuFRBU]{1,2}_\(')
_LEGACY_WRAP_FIX_RE = re.compile(
    r'\b([frbuFRBU]{1,2})_\(('
    r'"(?:[^"\\\n]|\\.)*"' r'|'
    r"'(?:[^'\\\n]|\\.)*'"
    r')\)'
)

# tl 文件 say 行的说话人（who）形态：普通名字 janitor、点分属性 mc.name、
# 下标 the_group[0]、字符串字面量 "Janitor"（动态角色名）。
# 旧实现只认 \w+，mc.name 这类动态说话人整行填充被静默跳过
# （LR2 系游戏上万条），游戏内显示英文。
_SAY_CONTENT_RE = re.compile(
    r'^\s+([\w.\[\]]+|"(?:[^"\\]|\\.)*")\s+"(.*)"')
# 无说话人的旁白行：整行恰好一个字符串字面量（宽松写法会误吞
# "Janitor" "text" 这类带引号说话人的行，把 who 也并进 text）
_SAY_NARRATION_RE = re.compile(r'^\s+"((?:[^"\\]|\\.)*)"\s*$')


def escape_translation(text: str, percent: str = 'say') -> str:
    """转义译文中的特殊字符，保证写入 .rpy 后是合法且可运行的字符串

    - 真实换行符 → \\n 转义序列（Ren'Py 字符串不支持跨行，换行会破坏解析）
    - 裸 % → %%（Ren'Py 对台词/菜单选项做 % 格式化，裸 % 会 ValueError）
      percent='say'：台词，仅保留 %% 与 %(name)s 变量
      percent='string'：UI 字符串，额外保留 strftime/%s 等代码格式符
      percent='none'：不转义 %——.format 模板专用（{1:+.0%} 的 % 是格式
      规格不是文本，双写会把 .format 炸掉）
    - 双引号 → \\"
    译文里已有的 \\n（反斜杠+n 两字符）、%%、%(name)s 变量不受影响。
    """
    text = text.replace('\r', '').replace('\n', '\\n')

    if percent == 'none':
        return text.replace('"', '\\"')

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


def iter_markup_issues(db):
    """逐条产出库内已译条目的标记一致性问题（导出闸门同一套校验）

    每条 {'kind', 'id', 'file', 'line', 'original', 'translation',
    'reasons': [...]}。scan_markup_issues（导出页前置警告）与
    markup-issues 修订接口共用。
    """
    for kind, rows in (('dialogue', db.get_all_dialogues()),
                       ('ui', db.get_all_ui_texts())):
        for r in rows:
            orig = r.get('original_text') or ''
            trans = r.get('translated_text') or ''
            if not orig or not trans:
                continue
            probs = markup_check.check_pair(orig, trans)
            if probs:
                yield {'kind': kind, 'id': r['id'],
                       'file': r.get('file_path', ''),
                       'line': r.get('line_number', 0),
                       'original': orig, 'translation': trans,
                       'reasons': probs}


def dedupe_string_tables(export_dir: Path, log) -> int:
    """导出副本 tl 的 strings 表去重清扫（重复 old 是 Ren'Py 加载硬错误）

    重复的三个来源，全部只在导出副本运行时才会合流：
    - SDK 校验重新生成模板：源码中任何 _()（导出时包的 wrap、游戏
      原生 _()、游戏补丁引入的 _()）被提取成 old 条目，与 zz 撞车
    - 同一文本在游戏多处原生 _()：SDK 提取进多个模板文件
    - zz 写条目的时刻早于某些模板生成（去重只对当时存在的文件）

    保留策略：非 zz 文件优先于 zz（SDK 模板是"官方"出处）；同优先级
    有译文优先。被弃条目若带译文而保留者为空，译文回填到保留者——
    只删行与填 new，不动任何其他结构（空 strings 块/空文件对 Ren'Py
    无害；下轮 SDK 校验也不会把 zz 没有的条目塞回 zz）。

    返回清理的重复组数。
    """
    from services.embedded_table import ZZ_NAME, _OLD_LINE_RE

    tl_dir = Path(export_dir) / 'game' / 'tl' / 'chinese'
    if not tl_dir.is_dir():
        return 0

    _STRINGS_HEAD_RE = re.compile(r'^translate\s+\w+\s+strings\s*:')
    _TRANSLATE_HEAD_RE = re.compile(r'^translate\s+\w+\s+\w+\s*:')
    _NEW_LINE_RE = re.compile(r'^\s+new\s+"(.*)"\s*$')

    entries = []
    for rpy in sorted(tl_dir.rglob('*.rpy')):
        try:
            lines = rpy.read_text(encoding='utf-8', errors='ignore').split('\n')
        except OSError:
            continue
        in_strings = False
        last_old = None  # (入库形态 old, 行号)
        for i, line in enumerate(lines):
            if _STRINGS_HEAD_RE.match(line):
                in_strings = True
                continue
            if _TRANSLATE_HEAD_RE.match(line):
                in_strings = False
                continue
            if not in_strings:
                continue
            m = _OLD_LINE_RE.match(line)
            if m:
                last_old = (m.group(1).replace('\\"', '"'), i)
                continue
            m = _NEW_LINE_RE.match(line)
            if m and last_old is not None:
                entries.append({'file': rpy, 'old': last_old[0],
                                'old_idx': last_old[1], 'new': m.group(1),
                                'new_idx': i})
                last_old = None

    groups: dict = {}
    for e in entries:
        groups.setdefault(e['old'], []).append(e)

    removals: dict = {}   # path -> 待删行号集合
    fills = []            # (path, new 行号, 译文)
    cleaned = 0
    for old, es in groups.items():
        if len(es) < 2:
            continue
        cleaned += 1
        # 保留者：非 zz 优先，再有译文优先
        es_sorted = sorted(es, key=lambda e: (e['file'].name == ZZ_NAME,
                                              not e['new']))
        keeper = es_sorted[0]
        for e in es_sorted[1:]:
            if not keeper['new'] and e['new']:
                fills.append((keeper['file'], keeper['new_idx'], e['new']))
                keeper = {**keeper, 'new': e['new']}
            removals.setdefault(e['file'], set()).update(
                (e['old_idx'], e['new_idx']))

    if not removals:
        return 0

    # 按文件应用：删行（倒序无关，集合一次性滤除）+ 填 new
    by_file: dict = {}
    for path, idx in removals.items():
        by_file.setdefault(path, {'drop': set(), 'fill': {}})
        by_file[path]['drop'] |= idx
    for path, idx, text in fills:
        by_file.setdefault(path, {'drop': set(), 'fill': {}})
        by_file[path]['fill'][idx] = text

    for path, ops in by_file.items():
        lines = path.read_text(encoding='utf-8', errors='ignore').split('\n')
        for idx, text in ops['fill'].items():
            if idx < len(lines):
                lines[idx] = re.sub(
                    r'^(\s*new\s+")(.*)("\s*)$',
                    lambda m: m.group(1) + text + m.group(3), lines[idx])
        lines = [l for i, l in enumerate(lines) if i not in ops['drop']]
        path.write_text('\n'.join(lines), encoding='utf-8')

    log(f'已清理 {cleaned} 组重复 old 条目（保留各组一条，译文不丢失）')
    return cleaned


def scan_markup_issues(db, sample_limit: int = 20) -> dict:
    """扫描库内已译条目的标记一致性（导出预检前置）

    与导出闸门同一套校验：扫出的条目导出时会被拦截、保留英文原文。
    前置到导出页展示，让用户在导出前去翻译界面修订，而不是导出后
    才在日志里看到拦截清单。

    返回 {'count': 违规总数, 'samples': [{kind, original, translation,
    reason}]}（samples 截断到 sample_limit 条，够定位问题即可）。
    """
    samples = []
    total = 0
    for it in iter_markup_issues(db):
        total += 1
        if len(samples) < sample_limit:
            samples.append({
                'kind': it['kind'],
                'original': it['original'][:80],
                'translation': it['translation'][:80],
                'reason': '；'.join(it['reasons'])[:160],
            })
    return {'count': total, 'samples': samples}


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
        self._blocked = []  # [(原文, 原因)] 标记校验拦截的译文（导出闸门）

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
        self._blocked = []
        try:
            self._check_cancel()
            # 预检：中文字体由用户自行放置（软件不携带字体，版权原因），
            # 缺失时尽早终止，不要等复制/填充完才失败
            font_files = self._require_user_fonts()
            log(f'中文字体: {font_files[0].name}')

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

            self._repair_legacy_prefix_wraps(export_dir, log)
            self._apply_ttag_literals(export_dir, log)
            self._apply_marked_wraps(export_dir, log)
            self._transform_fstrings(export_dir, log)
            self._sweep_ttag(export_dir, log)
            from services.game_patches import apply_game_patches
            apply_game_patches(export_dir, log)

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

                tip_count = self._add_tooltip_title_entries(
                    tl_dir, translation_dict)
                if tip_count:
                    log(f'菜单 tooltip 拆分条目: {tip_count} 条')

                # 导出闸门汇总：破坏插值/标签的译文保留英文原文，
                # 宁可显示英文也不让游戏渲染时报错
                if self._blocked:
                    log(f'⚠ 标记校验拦截 {len(self._blocked)} 条译文'
                        f'（已保留英文原文，请在翻译界面修订后重新导出）:')
                    for text, reason in self._blocked[:10]:
                        log(f'  · {text[:40]} — {reason[:90]}')
                    if len(self._blocked) > 10:
                        log(f'  ...其余 {len(self._blocked) - 10} 条同理')

            # 添加语言选择
            progress(0.9, '正在添加语言选择界面...')
            log('添加语言选择界面...')
            self._add_language_selector(export_dir, log)

            # 默认以中文启动（rpyc-only 游戏没有切换按钮也无需手动切换）
            self._set_default_language(export_dir, log)

            # 运行时助手（__rt 安全立即翻译等，f-string 变换的实参依赖）
            self._write_rt_helpers(export_dir, log)

            # 添加中文字体
            progress(0.95, '正在添加中文字体支持...')
            log('添加中文字体支持...')
            self._add_chinese_font(export_dir, log, font_files)

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
                        content_match = _SAY_CONTENT_RE.match(lines[i + 1])
                        narration_match = _SAY_NARRATION_RE.match(lines[i + 1])

                        # old/new 是 strings 块的行，绝不能当对话发言处理
                        if content_match and content_match.group(1) not in ('old', 'new'):
                            text = content_match.group(2).replace('\\"', '"')
                            if text in translation_dict:
                                cn = translation_dict[text]
                                probs = markup_check.check_pair(text, cn)
                                if probs:
                                    # 译文破坏插值/标签会让游戏渲染该句时报错，
                                    # 保留英文原文（少一句翻译好过崩溃），记录清单
                                    self._blocked.append((text, '；'.join(probs)))
                                    new_lines.append(lines[i + 1])
                                else:
                                    translated = escape_translation(cn)
                                    new_lines.append(f'    {content_match.group(1)} "{translated}"')
                                    filled += 1
                            else:
                                new_lines.append(lines[i + 1])
                            i += 2
                            continue
                        elif narration_match:
                            text = narration_match.group(1).replace('\\"', '"')
                            if text in translation_dict:
                                cn = translation_dict[text]
                                probs = markup_check.check_pair(text, cn)
                                if probs:
                                    self._blocked.append((text, '；'.join(probs)))
                                    new_lines.append(lines[i + 1])
                                else:
                                    translated = escape_translation(cn)
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

    _TOOLTIP_MARK = ' (tooltip)'

    def _add_tooltip_title_entries(self, tl_dir: Path,
                                   translation_dict: dict) -> int:
        """为含 ' (tooltip)' 的菜单 caption 派生拆分条目（标题/tooltip 各一）

        LR2 的 MenuItem 在运行时把 caption 在 ' (tooltip)' 处拆成标题与
        tooltip 两部分显示——strings 表的整串条目对拆分后的部分串必然
        查不中（"10% Extra" 这类子文本）。为每个含标记的已译条目派生
        两条部分条目（译文同步拆分），追加进 zz_embedded.rpy
        （导出副本，全量重写安全）。
        """
        from services.embedded_table import ZZ_NAME

        zz = tl_dir / ZZ_NAME
        # 收集全部 tl 文件（含 zz）的现有 old，避免写出重复条目
        olds = set()
        for rpy in tl_dir.rglob('*.rpy'):
            try:
                text = rpy.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            olds.update(re.findall(r'^\s+old "(.*)"', text, re.M))

        additions = []
        seen = set(olds)
        for orig, trans in translation_dict.items():
            if self._TOOLTIP_MARK not in orig or not trans:
                continue
            title_o, tip_o = orig.split(self._TOOLTIP_MARK, 1)
            if self._TOOLTIP_MARK not in trans:
                continue   # 译文丢了标记（闸门会拦），派生无意义
            title_t, tip_t = trans.split(self._TOOLTIP_MARK, 1)
            for part_o, part_t in ((title_o, title_t), (tip_o, tip_t)):
                if not part_o or not part_t:
                    continue
                if part_o in seen:
                    continue
                seen.add(part_o)
                additions.append((part_o, part_t))

        if not additions:
            return 0
        parts = []
        if not zz.exists():
            parts.append('translate chinese strings:\n')
        for part_o, part_t in additions:
            # translation_dict 的 key/value 是入库形态（\n 字面、\ 双写、
            # 引号不转义）——写文件形态只需补引号转义。
            # 不做 % 转义：拆分条目只经 LR2 自定义菜单的 textbutton/!t
            # 显示（Text 直显，不走菜单选项的 % 格式化），%% 会原样显示
            o_file = part_o.replace('"', '\\"')
            t_file = part_t.replace('"', '\\"')
            parts.append(f'\n    # (tooltip) 拆分派生\n'
                         f'    old "{o_file}"\n'
                         f'    new "{t_file}"\n')
        with open(zz, 'a', encoding='utf-8') as f:
            f.write('\n'.join(parts) + '\n')
        return len(additions)

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
                                probs = markup_check.check_pair(old_text, translated)
                                if probs:
                                    self._blocked.append(
                                        (old_text, '；'.join(probs)))
                                    new_lines.append(lines[i + 1])
                                else:
                                    # .format 模板（含 {N} 占位）不做 % 转义：
                                    # {1:+.0%} 的 % 是格式规格，双写会把
                                    # .format 炸掉（生产事故）
                                    pct = ('none' if re.search(r'\{\d', old_text)
                                           else 'string')
                                    # ' (disabled)' 是游戏逻辑标记
                                    # （MenuItem 靠它判敏感），绝不可译：
                                    # 译文换了中文括注就恢复英文标记
                                    if (old_text.endswith(' (disabled)')
                                            and not translated.endswith(
                                                ' (disabled)')):
                                        translated = re.sub(
                                            r'\s*[（(][^）)]*[）)]\s*$', '',
                                            translated).rstrip() + ' (disabled)'
                                    escaped = escape_translation(
                                        translated, percent=pct)
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

    def _repair_legacy_prefix_wraps(self, export_dir: Path, log) -> None:
        """还原旧版包装 bug 写入的 f_("...") 等坏包装（详见常量区注释）。

        只对导出副本动手，不改工作副本；模式极窄（前缀字母 + _( + 单行
        字面量 + 紧邻右括号），误伤面可忽略。有残留（多行/三引号）时
        输出警告行号提示人工处理。
        """
        fixed = 0
        leftovers = []
        for path in export_dir.rglob('*.rpy'):
            if 'tl' in path.parts:
                continue
            try:
                text = path.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                continue
            if not _LEGACY_WRAP_FIND_RE.search(text):
                continue
            new_text, n = _LEGACY_WRAP_FIX_RE.subn(r'\1\2', text)
            for i, line in enumerate(new_text.split('\n'), 1):
                if _LEGACY_WRAP_FIND_RE.search(line):
                    leftovers.append((path, i, line.strip()[:80]))
            if n:
                path.write_text(new_text, encoding='utf-8')
                fixed += n
        if fixed:
            log(f'已还原 {fixed} 处旧版前缀字符串坏包装（f_("...") 等，'
                '运行时会 NameError）')
        for path, line_no, line in leftovers:
            log(f'警告: {path.relative_to(export_dir)}:{line_no} '
                f'疑似多行前缀坏包装未自动还原: {line}')

    def _apply_marked_wraps(self, export_dir: Path, log) -> None:
        """在导出副本上应用 wrap 路径的 _() 包裹

        源码只读原则：标记阶段不改工作副本，包裹推迟到此（导出副本
        一次性应用）。wrap 行的译文条目早已由 zz 表合成入库（regen
        覆盖全部 marked 行），_() 查找与 strings 表共用存储，无需
        SDK 重新提取。行号漂移由 relocate_wrap_candidates 同文件
        重定位（重定位成功回写库内坐标）。无法定位的行跳过——zz
        条目无害残留，该处显示英文。
        """
        from embedded_strings import (apply_wrapping,
                                      relocate_wrap_candidates,
                                      resolve_source_root)

        rows = self.db.get_marked_embedded(apply_path='wrap')
        if not rows:
            return
        # 导出副本结构与工作副本一致：export_dir/game/ 为游戏根
        source_root = resolve_source_root(export_dir / 'game')
        candidates, moved_ids, lost_ids = relocate_wrap_candidates(
            rows, str(source_root))
        for row_id, line, col in moved_ids:
            self.db.update_embedded_position(row_id, line, col)
        wrapped, skipped, _ok = apply_wrapping(candidates)
        if wrapped:
            log(f'已在导出副本应用 {wrapped} 处 _() 包裹'
                f'（wrap 路径内嵌标记）')
        if moved_ids:
            log(f'  其中 {len(moved_ids)} 处因行号漂移已重定位')
        if skipped or lost_ids:
            log(f'警告: {skipped + len(lost_ids)} 处包裹定位失败'
                '（源码可能已更新，请重新扫描内嵌文本）；'
                '对应位置将保持英文')
        # 编译校验失败时 healer 依赖行号匹配定位失败处，
        # skipped/lost 的行号可能已漂移——healer 侧另有文本匹配兜底

    def _transform_fstrings(self, export_dir: Path, log) -> None:
        """在导出副本上把已标记的 f-string 改写为模板翻译形态

        与 wrap 应用同一批：源码只读原则下 f-string 的模板化改写
        （_("...{0}...").format(...) / _("...[var]...")）只在导出副本
        发生；模板译文条目早已由 zz 表合成入库。行号漂移同文件重定位
        并回写库坐标；定位/派生失败跳过不阻断（该处保持英文）。
        """
        from embedded_strings import resolve_source_root
        from fstring_strings import transform_fstring_literals

        rows = self.db.get_marked_embedded(apply_path='fstring')
        if not rows:
            return
        source_root = resolve_source_root(export_dir / 'game')
        done, moved_ids, lost_ids = transform_fstring_literals(
            rows, str(source_root))
        for row_id, line, col in moved_ids:
            self.db.update_embedded_position(row_id, line, col)
        if done:
            log(f'已改写 {done} 处 f-string 为模板翻译形态'
                f'（_{"(...)"} 包裹静态模板）')
        if moved_ids:
            log(f'  其中 {len(moved_ids)} 处因行号漂移已重定位')
        if lost_ids:
            log(f'警告: {len(lost_ids)} 处 f-string 改写定位/派生失败'
                '（源码可能已更新，请重新扫描内嵌文本）；'
                '对应位置将保持英文')

    def _apply_ttag_literals(self, export_dir: Path, log) -> None:
        """含插值的已标记 table 行：源码字面量的插值补 !t（值通道翻译）

        Ren'Py 替换式"先译模板、后插值"——[x] 的值原样插入，[x!t] 的
        值先过 strings 表。zz 条目（regen 从行 text 生成）已带 !t，
        字面量必须同步改写才查得中。定位纪律与 wrap 应用一致
        （位置校验/同文件重定位/回写坐标/失败跳过告警）。

        必须在 _sweep_ttag 之前运行：先按 raw（无 !t）定位已标记行，
        再由幂等的清扫兜底未标记位置。
        """
        from embedded_strings import resolve_source_root
        from markup_check import add_tflag, extract_interps

        rows = [r for r in self.db.get_table_marked_embedded()
                if extract_interps(r['text'])]
        if not rows:
            return
        source_root = resolve_source_root(export_dir / 'game')

        # 逐行定位（raw 校验 → 同文件重定位），再按文件倒序改写
        file_lines: dict = {}
        located = []
        lost = 0
        for r in rows:
            rel = r['rel_file']
            if rel not in file_lines:
                try:
                    file_lines[rel] = (source_root / rel).read_text(
                        encoding='utf-8').split('\n')
                except OSError:
                    file_lines[rel] = None
            lines = file_lines[rel]
            if lines is None:
                lost += 1
                continue
            idx = r['line'] - 1
            hits = []
            if (0 <= idx < len(lines)
                    and lines[idx][r['col_start']:r['col_start'] + len(r['raw'])]
                    == r['raw']):
                hits.append((r['line'], r['col_start']))
            else:
                for ln, line in enumerate(lines, 1):
                    start = 0
                    while True:
                        col = line.find(r['raw'], start)
                        if col < 0:
                            break
                        hits.append((ln, col))
                        start = col + 1
            if not hits:
                lost += 1
                continue
            if len(hits) > 1:
                hits.sort(key=lambda h: abs(h[0] - r['line']))
            ln, col = hits[0]
            located.append((r, ln, col))
            if (ln, col) != (r['line'], r['col_start']):
                self.db.update_embedded_position(r['id'], ln, col)

        by_file: dict = {}
        for item in located:
            by_file.setdefault(item[0]['rel_file'], []).append(item)
        done = 0
        for rel, items in by_file.items():
            lines = file_lines[rel]
            changed = False
            for r, ln, col in sorted(items, key=lambda x: (x[1], x[2]),
                                     reverse=True):
                idx = ln - 1
                line = lines[idx]
                if line[col:col + len(r['raw'])] != r['raw']:
                    lost += 1
                    continue
                new_raw = add_tflag(r['raw'])
                if new_raw != r['raw']:
                    lines[idx] = line[:col] + new_raw + line[col + len(r['raw']):]
                    done += 1
                    changed = True
            if changed:
                (source_root / rel).write_text('\n'.join(lines),
                                               encoding='utf-8')
        if done:
            log(f'已为 {done} 处已标记界面文本的插值补 !t（值通道翻译）')
        if lost:
            log(f'警告: {lost} 处 !t 改写定位失败（该处值将保持英文）')

    # 值通道清扫的两类字面量：
    # 1) text/textbutton/label/tooltip 属性行的字符串（含纯插值模板
    #    "[item.title!i]"——从未成候选、无 zz 条目，!t 是唯一通道）
    # 2) _() 包裹的字符串（其 old 条目在下面同步补 !t）
    # 关键字必须是屏幕语句：前不可为标识符字符/点（tooltip.append 是方法
    # 调用不是属性行），后不可为点。
    _TTAG_PROP_RE = re.compile(
        r'(?<![\w.])(?:textbutton|text|label|tooltip)\b(?!\s*\.)')

    def _sweep_ttag(self, export_dir: Path, log) -> None:
        """导出副本全量值通道清扫：剩余插值统一补 !t

        两部分，全部幂等（add_tflag 跳过已含 t 的插值）：
        - 屏幕属性行/_() 包裹的源码字面量
        - 全部 tl 文件 strings 块的 old 行（SDK 模板里的原生 _() 条目）
        zz 条目在 regen 时已带 !t，此处自然跳过。
        """
        from embedded_strings import _PY_STRING_RE, _string_prefix
        from markup_check import add_tflag

        fixed_src = 0
        game_root = export_dir / 'game'
        for rpy in game_root.rglob('*.rpy'):
            if 'tl' in rpy.parts:
                continue
            try:
                lines = rpy.read_text(encoding='utf-8',
                                      errors='ignore').split('\n')
            except OSError:
                continue
            changed = False
            for i, line in enumerate(lines):
                is_prop = bool(self._TTAG_PROP_RE.search(line))
                if not is_prop and '_(' not in line:
                    continue
                # 注意：finditer 绑定的是原字符串——改写后位置会漂移，
                # 单行多字面量时用「逐次从偏移 0 起找一个改一个」的循环
                while True:
                    edited = False
                    for m in _PY_STRING_RE.finditer(line):
                        # 带前缀字面量：含 { 的 f-string 跳过（{} 里是 Python
                        # 表达式，下标会被 add_tflag 改成非法语法——由
                        # _transform_fstrings 专门处理）；不含 { 的
                        # f-string（如 f"[obedience_label]: [...]"，只有
                        # Ren'Py 插值）安全，照常补 !t
                        if _string_prefix(line, m.start()) \
                                and '{' in m.group(0):
                            continue
                        if not is_prop:
                            # _() 规则：字面量必须紧跟 _( 之后——但不能是
                            # __(（f-string 变换已写好的立即翻译输出，
                            # 再补 !t 会把 [{0}] 改成 [{0}!t] 二次破坏）
                            prefix = line[max(0, m.start() - 3):m.start()]
                            if (not prefix.endswith('_(')
                                    or prefix.endswith('__(')):
                                continue
                        new_lit = add_tflag(m.group(0))
                        if new_lit != m.group(0):
                            line = line[:m.start()] + new_lit + line[m.end():]
                            fixed_src += 1
                            changed = True
                            edited = True
                            break
                    if not edited:
                        break
                if changed:
                    lines[i] = line
            if changed:
                rpy.write_text('\n'.join(lines), encoding='utf-8')

        fixed_tl = 0
        tl_dir = game_root / 'tl' / 'chinese'
        if tl_dir.is_dir():
            for rpy in tl_dir.rglob('*.rpy'):
                try:
                    lines = rpy.read_text(encoding='utf-8',
                                          errors='ignore').split('\n')
                except OSError:
                    continue
                changed = False
                for i, line in enumerate(lines):
                    if not re.match(r'^\s+old\s+"', line):
                        continue
                    new_line = add_tflag(line)
                    if new_line != line:
                        lines[i] = new_line
                        fixed_tl += 1
                        changed = True
                if changed:
                    rpy.write_text('\n'.join(lines), encoding='utf-8')

        if fixed_src or fixed_tl:
            log(f'值通道清扫: 源码插值补 !t {fixed_src} 处, '
                f'模板 old 条目补 !t {fixed_tl} 处')

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

    def _write_rt_helpers(self, export_dir: Path, log) -> None:
        """写运行时助手文件（tl/chinese/zz_rt_helpers.rpy，每次全量重写）

        rt_translate(s)：安全立即翻译。__() 直调 translate_string，内部
        re.sub 收到 float/int 即 TypeError（显示层替换式会先 str()，直调
        必须自带防护）——f-string 变换的实参用它。
        注意命名不能以下划线开头：屏幕代码里 `_name` 会被 Ren'Py 改编成
        屏幕局部名（_m1_screenname__name）而 NameError。
        init python 在 tl 文件里不受语言门控（只有 translate 块受门控），
        任何语言下都可用。
        """
        tl_dir = export_dir / 'game' / 'tl' / 'chinese'
        tl_dir.mkdir(parents=True, exist_ok=True)
        content = (
            '# 导出工具运行时助手（自动生成，每次导出全量重写）\n'
            'init -10 python:\n'
            '    def rt_translate(s):\n'
            '        return __(s) if isinstance(s, str) else s\n'
        )
        (tl_dir / 'zz_rt_helpers.rpy').write_text(content, encoding='utf-8')
        log('已写入运行时助手（zz_rt_helpers.rpy）')

    @staticmethod
    def _require_user_fonts() -> list:
        """用户放置的中文字体列表；未放置时抛错终止导出。

        软件不携带字体（版权原因）：用户在 数据根/fonts/ 自行放置
        .ttf/.otf/.ttc（find_resource 搜索序：数据根优先，可覆盖 exe 旁）。
        """
        from rt_home import find_resource, home
        fonts_dir = find_resource('fonts')
        font_files = []
        if fonts_dir is not None:
            font_files = sorted(
                f for f in fonts_dir.iterdir()
                if f.suffix.lower() in ('.ttf', '.ttc', '.otf'))
        if not font_files:
            raise RuntimeError(
                '未找到中文字体，导出已终止。软件不自带字体：请将一个'
                '中文字体文件（.ttf/.otf/.ttc）放入 '
                f'{home() / "fonts"}/ 后重新导出'
                '（没有字体的导出包中文将无法显示）')
        return font_files

    def _add_chinese_font(self, export_dir: Path, log, font_files: list):
        """添加中文字体支持（字体清单由 _require_user_fonts 预检提供）"""
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
