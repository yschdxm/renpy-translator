"""项目创建服务（从 project_panel 抽取，无 UI 依赖）

管线：建库 → 复制游戏文件 → 解包 rpa/反编译 rpyc → 官中检测(需用户确认)
→ SDK 生成模板 → 解析角色 → 解析 tl → 定位 UI 上下文 → 清理冲突。

用法:
    creator = ProjectCreator(project_manager, logger, get_sdk_path)
    result = await creator.create(
        name, game_dir, model,
        progress=lambda pct, text: ...,                    # 同步回调
        confirm_official_chinese=async def (file_count) -> bool,
    )
返回 {'success': True, 'dialogues': n, 'ui_texts': m} 或 {'cancelled': True}。
异常：抛出前已关闭 db（否则 Windows 上清理目录 WinError 32）。
UnrpycMissingError 原样上抛（调用方负责提示安装方法并清理项目目录）。
"""
import asyncio
import json as _json
import os
import re
import shutil
import sys
import zipfile


def _unrpyc_python_exe() -> str:
    """unrpyc 子进程的解释器：冻结 exe 无 sys.executable 可用解释器，
    回退到用户数据根的 tools/python-embed（缺失则响亮报错）"""
    if getattr(sys, 'frozen', False):
        from rt_home import find_resource
        for rel in ('tools/python-embed/python.exe',
                    'tools/python-embed/bin/python3'):
            embed = find_resource(rel)
            if embed:
                return str(embed)
        raise RuntimeError(
            '反编译需要 Python 解释器，但 tools/python-embed/ 不存在。'
            '请放置对应平台的 embeddable/standalone Python')
    return sys.executable
from datetime import datetime
from pathlib import Path

from logger import TranslationLogger
from project_manager import ProjectManager

# 中文相关语言目录名（小写匹配）
CHINESE_LANG_NAMES = {
    'chinese', 'zh', 'ch', 'chs', 'cht', 'zhs', 'zht',
    'schinese', 'tchinese', 'cn', 'zh-cn', 'zh-tw', 'zh-hans', 'zh-hant',
    'simplified_chinese', 'traditional_chinese',
}


def detect_official_chinese(game_work_dir: Path) -> int:
    """检测游戏 tl 目录下是否已有中文相关翻译目录

    解压后、SDK 生成前调用。tl 下若存在中文相关目录（Chinese/zh/ch/zhs 等）
    且其中有 .rpy/.rpyc，即判定存在官中/第三方汉化（.rpyc 为编译版汉化）。
    返回中文目录中的翻译文件数，0 表示无官中。
    """
    tl_root = game_work_dir / 'game' / 'tl'
    if not tl_root.exists():
        return 0
    count = 0
    for lang_dir in tl_root.iterdir():
        if not lang_dir.is_dir():
            continue
        if lang_dir.name.lower() in CHINESE_LANG_NAMES:
            count += sum(1 for _ in lang_dir.rglob('*.rpy'))
            count += sum(1 for _ in lang_dir.rglob('*.rpyc'))
    return count


def remove_official_chinese(game_work_dir: Path, logger: TranslationLogger = None):
    """删除 tl 下的中文相关翻译目录（含 .rpy/.rpyc/图片等）

    SDK 之后会重建干净的模板目录。
    """
    tl_root = game_work_dir / 'game' / 'tl'
    if not tl_root.exists():
        return
    for lang_dir in tl_root.iterdir():
        if not lang_dir.is_dir():
            continue
        if lang_dir.name.lower() in CHINESE_LANG_NAMES:
            try:
                shutil.rmtree(lang_dir)
                if logger:
                    logger.info(f'已删除中文翻译目录: tl/{lang_dir.name}', panel='projects')
            except OSError as e:
                if logger:
                    logger.warning(f'删除中文目录失败 {lang_dir.name}: {e}', panel='projects')


def cleanup_conflicts(game_dir: Path, logger: TranslationLogger = None):
    """清理冲突文件：.rpa 存档与自动提取的 .rpy"""
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
                    if logger:
                        logger.warning(f'清理冲突文件 {rpy_file.name}: {e}')
                    rpy_file.unlink()


def extract_game_zip(zip_path, extract_dir: Path, progress_cb=None) -> str:
    """解压游戏 zip（同步，调用方放 executor）。progress_cb(current, total)。

    返回解压出的游戏目录（zip 内只有一个顶层目录时返回该目录）。
    失败抛异常。
    """
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        members = zf.namelist()
        total = len(members)
        for i, member in enumerate(members):
            zf.extract(member, str(extract_dir))
            if progress_cb:
                progress_cb(i + 1, total)
    entries = list(extract_dir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return str(entries[0])
    return str(extract_dir)


class ProjectCreator:
    """创建项目（异步编排；阻塞操作全部 run_in_executor）"""

    def __init__(self, project_manager: ProjectManager,
                 logger: TranslationLogger, get_sdk_path=None):
        self.project_manager = project_manager
        self.logger = logger
        # get_sdk_path(game_dir)：按游戏引擎大版本解析匹配的 SDK 路径
        self.get_sdk_path = get_sdk_path

    async def create(self, name: str, game_dir: str, model: str,
                     progress, confirm_official_chinese=None) -> dict:
        """执行创建。progress(pct, text) 同步回调；confirm_official_chinese
        为 async (file_count) -> bool（True=删除官中继续，False=取消创建）。

        用户取消：关闭 db、删除项目目录，返回 {'cancelled': True}。
        """
        loop = asyncio.get_event_loop()
        db = None
        try:
            # 步骤1: 创建项目数据库
            progress(0.05, '正在初始化项目...')

            db = await loop.run_in_executor(
                None, self.project_manager.create_project, name, game_dir, model or ''
            )
            project_dir = self.project_manager._get_project_dir(name)
            game_work_dir = project_dir / 'game'

            # 步骤2: 复制游戏文件（逐文件复制，带进度）
            progress(0.06, '正在复制游戏文件...')

            copy_progress = {'current': 0, 'total': 0, 'done': False}

            def _copy_game():
                try:
                    if game_work_dir.exists():
                        shutil.rmtree(game_work_dir)
                    src = Path(game_dir)
                    total = sum(1 for _ in src.rglob('*') if _.is_file())
                    copy_progress['total'] = total if total > 0 else 1
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
            while not copy_progress['done']:
                total = copy_progress['total']
                current = copy_progress['current']
                if total > 0:
                    progress(0.06 + (current / total) * 0.04,
                             f'正在复制游戏文件... ({current}/{total})')
                await asyncio.sleep(0.3)
            await copy_task
            progress(0.30, '游戏文件复制完成')

            # 步骤3: 解包 rpa & 反编译 rpyc
            # 反编译必须在 SDK 生成前：translate 只能从 .rpy 源生成模板
            # （实测：同一脚本编译成 .rpyc 删掉 .rpy 后就不再产出模板），
            # 纯 .rpyc 游戏的反编译产物是模板生成的唯一来源
            progress(0.30, '正在解包游戏资源...')

            from renpy_parser import RenpyParser
            parser = RenpyParser()

            def _parse():
                return parser.parse_directory(
                    str(game_work_dir), extract_rpa=True,
                    work_dir=str(game_work_dir),
                    decompile_rpyc=True, python_exe=_unrpyc_python_exe()
                )

            result = await loop.run_in_executor(None, _parse)
            progress(0.50, '解包完成')

            # rpyc 反编译结果：记录日志，产出清单存 meta 供导出时清理
            if result.get('decompiled_rpyc_ok') or result.get('decompiled_rpyc_fail'):
                self.logger.info(
                    f'rpyc 反编译: {result["decompiled_rpyc_ok"]} 成功, '
                    f'{result["decompiled_rpyc_fail"]} 失败', panel='projects')
            rel_files = [
                Path(p).relative_to(game_work_dir).as_posix()
                for p in result.get('decompiled_files', [])
            ]
            if rel_files:
                await loop.run_in_executor(
                    None, db.set_meta, 'decompiled_rpy_files', _json.dumps(rel_files)
                )

            # 步骤3.5: 检测官方中文翻译，确认后删除（否则与 SDK 模板重复入库）
            # 必须在解包后（官中可能在 rpa 里）、SDK 生成前检测
            official_tl = await loop.run_in_executor(
                None, detect_official_chinese, game_work_dir
            )
            if official_tl:
                progress(0.50, '检测到游戏自带中文翻译...')
                if confirm_official_chinese is None:
                    raise RuntimeError('检测到官方中文翻译但未提供确认回调')
                use_sdk = await confirm_official_chinese(official_tl)
                if not use_sdk:
                    # 用户取消：关闭 db、删除已生成的项目目录
                    progress(0.50, '已取消创建')
                    await loop.run_in_executor(None, db.close)
                    db = None
                    await loop.run_in_executor(
                        None, self.project_manager.delete_project, name
                    )
                    self.logger.info('用户取消创建：检测到游戏自带中文翻译', panel='projects')
                    return {'cancelled': True}
                progress(0.50, '正在删除官方中文翻译...')
                await loop.run_in_executor(
                    None, remove_official_chinese, game_work_dir, self.logger
                )
                self.logger.info(f'已删除官方中文翻译（{official_tl} 个文件）', panel='projects')

            # 步骤4: SDK 生成翻译文件（自愈重试）
            sdk_path = (self.get_sdk_path(str(game_work_dir))
                        if self.get_sdk_path else '')
            if not sdk_path:
                # 游戏版本可探测但没有匹配大版本的 SDK（Ren'Py 7 与 8 的
                # .rpyc 互不兼容），继续走只会得到没有对话的空项目
                from sdk_manager import detect_engine_version
                gv = detect_engine_version(game_work_dir)
                if gv:
                    hint = '（7.x 建议 7.4.11）' if gv[0] == 7 else ''
                    raise Exception(
                        f"游戏引擎为 Ren'Py {'.'.join(map(str, gv))}，"
                        f'但未安装 {gv[0]}.x 的 SDK。\n'
                        '启动时的自动补装可能仍在下载（见任务列表），'
                        '请稍候重新创建；若下载失败，请在模型配置页'
                        f'手动下载对应版本的 SDK{hint}')
            if sdk_path:
                progress(0.55, '正在使用 SDK 生成翻译文件...')

                from sdk_manager import SDKManager
                sdk = SDKManager()
                sdk.sdk_path = Path(sdk_path)

                def _sdk():
                    return sdk.generate_translations(str(game_work_dir), 'chinese')

                # 反编译产物可能有语法瑕疵（unrpyc 对部分语句还原不完美，
                # 如空 scene 块），translate 解析到就整体失败。把报错的
                # 反编译文件隔离（改名 .rpy.broken）后重试：损失该文件的
                # 模板，但保住整项目创建。游戏自带源文件报错则不降级，原样抛。
                err_re = re.compile(r'File "([^"]+\.rpy)", line \d+')
                decompiled_set = set(rel_files)
                quarantined = []
                for attempt in range(5):
                    sdk_result = await loop.run_in_executor(None, _sdk)
                    if sdk_result['success']:
                        break
                    bad = None
                    for m in err_re.finditer(sdk_result.get('output') or ''):
                        rel = m.group(1).replace('\\', '/')
                        if rel in decompiled_set:
                            bad = rel
                            break
                    if bad is None:
                        # 必须带上 SDK 输出：返回码本身无法区分是游戏脚本
                        # 解析失败还是 SDK 版本不匹配
                        tail = (sdk_result.get('output') or '')[-2000:].strip()
                        detail = f'\nSDK 输出:\n{tail}' if tail else ''
                        raise Exception(
                            f'SDK 生成翻译文件失败: {sdk_result["message"]}{detail}')
                    src = game_work_dir / bad
                    src.rename(src.with_name(src.name + '.broken'))
                    quarantined.append(bad + '.broken')
                    self.logger.warning(
                        f'反编译文件语法错误，已隔离（该文件的对话将缺失）: {bad}',
                        panel='projects')
                    progress(0.55, f'已隔离损坏的反编译文件，重试生成...')
                else:
                    raise Exception(
                        'SDK 生成翻译文件失败：损坏的反编译文件过多'
                        f'（已隔离 {len(quarantined)} 个仍有报错）')
                if quarantined:
                    # 隔离文件加入导出清理清单，并给用户一条汇总
                    await loop.run_in_executor(
                        None, db.set_meta, 'decompiled_rpy_files',
                        _json.dumps(rel_files + quarantined))
                    self.logger.warning(
                        f'共隔离 {len(quarantined)} 个损坏的反编译文件，'
                        '对应对话不会出现在翻译列表中', panel='projects')

                # 只有 common.rpy = 游戏脚本一条都没进模板（典型原因：
                # 游戏引擎与 SDK 大版本不匹配，SDK 读不了游戏的 .rpyc），
                # 继续走只会得到一个没有任何对话的空项目
                tl_out = game_work_dir / 'game' / 'tl' / 'chinese'
                has_templates = tl_out.exists() and any(
                    p.name != 'common.rpy' for p in tl_out.glob('*.rpy'))
                if not has_templates:
                    raise Exception(
                        "SDK 未为游戏脚本生成任何翻译模板（只有 common.rpy）。\n"
                        "通常是游戏引擎版本与 SDK 不匹配——请检查游戏的 Ren'Py "
                        "版本，在模型配置页下载对应大版本的 SDK 后重新创建项目")

            progress(0.60, 'SDK 模板就绪')

            # 步骤5: 解析角色信息
            progress(0.70, '正在解析角色信息...')

            fresh_parser = RenpyParser()

            def _parse_chars():
                return fresh_parser.parse_directory(
                    str(game_work_dir), extract_rpa=False
                )

            char_result = await loop.run_in_executor(None, _parse_chars)

            characters = [{"variable": c.variable, "display_name": c.name}
                          for c in char_result['characters']]

            def _save_chars():
                db.insert_characters(characters)

            await loop.run_in_executor(None, _save_chars)
            progress(0.80, '角色信息已保存')

            # 步骤6: 解析翻译文件
            tl_dir = game_work_dir / 'game' / 'tl' / 'chinese'

            def _check_tl_dir():
                return tl_dir.exists()

            tl_exists = await loop.run_in_executor(None, _check_tl_dir)
            if tl_exists:
                progress(0.80, '正在解析翻译文件...')

                def _parse_tl():
                    from tl_parser import parse_translation_files
                    return parse_translation_files(tl_dir, str(game_work_dir),
                                                   logger=self.logger)

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

                # 步骤6.5: 回扫源码定位 UI 字符串出处
                progress(0.90, '正在定位字符串上下文...')

                def _locate_hints():
                    p = RenpyParser()
                    return p.locate_ui_string_contexts(str(game_work_dir))

                try:
                    hints = await loop.run_in_executor(None, _locate_hints)
                    matched = await loop.run_in_executor(None, db.update_ui_hints, hints)
                    self.logger.info(f'UI 上下文定位: {matched} 条命中', panel='projects')
                except Exception as e:
                    self.logger.warning(f'UI 上下文定位失败（不影响建项）: {e}', panel='projects')

            progress(0.95, '正在清理冲突文件...')

            # 步骤7: 清理冲突文件
            await loop.run_in_executor(None, cleanup_conflicts, game_work_dir, self.logger)

            # 更新时间戳和统计
            def _finalize():
                db.set_meta("updated_at", datetime.now().isoformat())
                d_count = db.get_dialogue_count()
                u_count = db.get_ui_text_count()
                db.close()
                return d_count, u_count

            d_count, u_count = await loop.run_in_executor(None, _finalize)
            db = None

            progress(1.0, f'✅ 创建成功！{d_count["total"]} 条对话, {u_count["total"]} 条字符串')
            return {'success': True,
                    'dialogues': d_count['total'], 'ui_texts': u_count['total']}

        except Exception:
            if db is not None:
                try:
                    await loop.run_in_executor(None, db.close)
                except Exception:
                    pass
            raise
