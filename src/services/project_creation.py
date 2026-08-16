"""项目创建服务（从 project_panel 抽取，无 UI 依赖）

管线：建库 → 复制游戏文件 → 解包 rpa/反编译 rpyc → 官中检测(需用户确认)
→ SDK 生成模板 → 解析 tl → 解析角色 → 定位 UI 上下文 → 清理冲突。
与更新管线共用的编排步骤在 services/game_pipeline.py。

用法:
    creator = ProjectCreator(project_manager, logger, get_sdk_path)
    result = await creator.create(
        name, game_dir,
        progress=lambda pct, text: ...,                    # 同步回调
        confirm_official_chinese=async def (file_count) -> bool,
    )
返回 {'success': True, 'dialogues': n, 'ui_texts': m} 或 {'cancelled': True}。
异常：抛出前已关闭 db（否则 Windows 上清理目录 WinError 32）。
UnrpycMissingError 原样上抛（调用方负责提示安装方法并清理项目目录）。
"""
import asyncio
import json as _json
import re
import shutil
import zipfile
from datetime import datetime
from functools import partial
from pathlib import Path

from logger import TranslationLogger
from project_manager import ProjectManager
from services.game_pipeline import (  # noqa: F401（_unrpyc_python_exe/copy_game_files 为兼容旧导入再导出）
    _unrpyc_python_exe, copy_game_files, copy_with_progress,
    locate_ui_hints, refresh_characters, resolve_sdk_or_raise,
    unpack_and_decompile,
)

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


async def generate_tl_templates(sdk_path: str, game_work_dir: Path,
                                decompiled_rel: list, db,
                                logger: TranslationLogger,
                                progress, run_in_executor,
                                cancel_event=None) -> list:
    """SDK 生成翻译模板（自愈重试：隔离损坏的反编译文件后重试）。

    阻塞调用通过 run_in_executor(fn, *args) 调度（调用方传入绑定 loop 与
    None executor 的偏函数）。db 用于回写 decompiled_rpy_files meta；
    progress(pct, text)。返回隔离的 .broken 相对路径列表。
    cancel_event: 可选 threading.Event，置位时终止 SDK 子进程并抛异常。
    失败抛异常（含 SDK 输出尾部）。
    """
    from sdk_manager import SDKManager
    sdk = SDKManager()
    sdk.sdk_path = Path(sdk_path)

    def _sdk():
        return sdk.generate_translations(str(game_work_dir), 'chinese',
                                         cancel_event=cancel_event)

    # 反编译产物可能有语法瑕疵（unrpyc 对部分语句还原不完美，
    # 如空 scene 块），translate 解析到就整体失败。把报错的
    # 反编译文件隔离（改名 .rpy.broken）后重试：损失该文件的
    # 模板，但保住整项目创建。游戏自带源文件报错则不降级，原样抛。
    err_re = re.compile(r'File "([^"]+\.rpy)", line \d+')
    decompiled_set = set(decompiled_rel)
    quarantined = []
    for attempt in range(5):
        sdk_result = await run_in_executor(_sdk)
        if sdk_result['success']:
            break
        if sdk_result.get('cancelled'):
            raise Exception('已取消：SDK 生成翻译模板被中止')
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
        logger.warning(
            f'反编译文件语法错误，已隔离（该文件的对话将缺失）: {bad}',
            panel='projects')
        progress(0.55, '已隔离损坏的反编译文件，重试生成...')
    else:
        raise Exception(
            'SDK 生成翻译文件失败：损坏的反编译文件过多'
            f'（已隔离 {len(quarantined)} 个仍有报错）')
    if quarantined:
        # 隔离文件加入导出清理清单，并给用户一条汇总
        await run_in_executor(
            db.set_meta, 'decompiled_rpy_files',
            _json.dumps(decompiled_rel + quarantined))
        logger.warning(
            f'共隔离 {len(quarantined)} 个损坏的反编译文件，'
            '对应对话不会出现在翻译列表中', panel='projects')

    # 只有 common.rpy = 游戏脚本一条都没进模板（典型原因：
    # 游戏引擎与 SDK 大版本不匹配，SDK 读不了游戏的 .rpyc），
    # 继续走只会得到一个没有任何对话的空项目。
    # 注意模板目录镜像游戏脚本的子目录结构（脚本在 game/scripts/ 下时
    # 模板落在 tl/chinese/scripts/），必须递归查找。
    tl_out = game_work_dir / 'game' / 'tl' / 'chinese'
    has_templates = tl_out.exists() and any(
        p.name != 'common.rpy' for p in tl_out.rglob('*.rpy'))
    if not has_templates:
        raise Exception(
            "SDK 未为游戏脚本生成任何翻译模板（只有 common.rpy）。\n"
            "通常是游戏引擎版本与 SDK 不匹配——请检查游戏的 Ren'Py "
            "版本，在模型配置页下载对应大版本的 SDK 后重新创建项目")
    return quarantined


def parse_tl_dir(tl_dir: Path, game_work_dir: Path,
                 logger: TranslationLogger) -> dict:
    """解析 SDK 生成的 tl/chinese 目录（同步，调用方放 executor）"""
    from tl_parser import parse_translation_files
    return parse_translation_files(tl_dir, str(game_work_dir), logger=logger)


class ProjectCreator:
    """创建项目（异步编排；阻塞操作全部 run_in_executor）"""

    def __init__(self, project_manager: ProjectManager,
                 logger: TranslationLogger, get_sdk_path=None):
        self.project_manager = project_manager
        self.logger = logger
        # get_sdk_path(game_dir)：按游戏引擎大版本解析匹配的 SDK 路径
        self.get_sdk_path = get_sdk_path

    async def create(self, name: str, game_dir: str,
                     progress, confirm_official_chinese=None,
                     cancel_event=None) -> dict:
        """执行创建。progress(pct, text) 同步回调；confirm_official_chinese
        为 async (file_count) -> bool（True=删除官中继续，False=取消创建）。
        cancel_event: 可选 threading.Event，传给 SDK 子进程以便中止。

        用户取消：关闭 db、删除项目目录，返回 {'cancelled': True}。
        """
        loop = asyncio.get_event_loop()
        _rie = partial(loop.run_in_executor, None)
        db = None
        try:
            # 步骤1: 创建项目数据库
            progress(0.05, '正在初始化项目...')

            db = await _rie(
                self.project_manager.create_project, name, game_dir
            )
            project_dir = self.project_manager.project_dir(name)
            game_work_dir = project_dir / 'game'

            # 步骤2: 复制游戏文件（逐文件复制，带进度）
            progress(0.06, '正在复制游戏文件...')
            await copy_with_progress(_rie, game_dir, game_work_dir, progress,
                                     0.06, 0.10)
            progress(0.30, '游戏文件复制完成')

            # 步骤3: 解包 rpa & 反编译 rpyc
            rel_files = await unpack_and_decompile(
                _rie, game_work_dir, db, self.logger, progress)

            # 步骤3.5: 检测官方中文翻译，确认后删除（否则与 SDK 模板重复入库）
            # 必须在解包后（官中可能在 rpa 里）、SDK 生成前检测
            official_tl = await _rie(detect_official_chinese, game_work_dir)
            if official_tl:
                progress(0.50, '检测到游戏自带中文翻译...')
                if confirm_official_chinese is None:
                    raise RuntimeError('检测到官方中文翻译但未提供确认回调')
                use_sdk = await confirm_official_chinese(official_tl)
                if not use_sdk:
                    # 用户取消：关闭 db、删除已生成的项目目录
                    progress(0.50, '已取消创建')
                    await _rie(db.close)
                    db = None
                    await _rie(self.project_manager.delete_project, name)
                    self.logger.info('用户取消创建：检测到游戏自带中文翻译', panel='projects')
                    return {'cancelled': True}
                progress(0.50, '正在删除官方中文翻译...')
                await _rie(remove_official_chinese, game_work_dir, self.logger)
                self.logger.info(f'已删除官方中文翻译（{official_tl} 个文件）', panel='projects')

            # 步骤4: SDK 生成翻译文件（自愈重试）
            sdk_path = await resolve_sdk_or_raise(
                _rie, self.get_sdk_path, game_work_dir, '创建')
            if sdk_path:
                progress(0.55, '正在使用 SDK 生成翻译文件...')
                await generate_tl_templates(
                    sdk_path, game_work_dir, rel_files, db,
                    self.logger, progress, _rie, cancel_event=cancel_event)

            progress(0.60, 'SDK 模板就绪')

            # 步骤5: 解析翻译文件
            tl_dir = game_work_dir / 'game' / 'tl' / 'chinese'
            tl_exists = await _rie(tl_dir.exists)
            tl_result = {}
            if tl_exists:
                progress(0.70, '正在解析翻译文件...')
                tl_result = await _rie(
                    parse_tl_dir, tl_dir, game_work_dir, self.logger)

                def _save_tl():
                    db.insert_dialogues(tl_result.get('dialogues', []))
                    db.insert_ui_texts(tl_result.get('ui_texts', []))

                await _rie(_save_tl)

            # 步骤6: 解析角色 + 统计台词数（无模板时也保留角色清单）
            progress(0.80, '正在解析角色信息...')
            await _rie(refresh_characters, game_work_dir, db,
                       tl_result.get('dialogues', []))

            # 步骤6.5: 回扫源码定位 UI 字符串出处
            if tl_exists:
                progress(0.90, '正在定位字符串上下文...')
                await locate_ui_hints(
                    _rie, game_work_dir, db, self.logger, '建项')

            progress(0.95, '正在清理冲突文件...')

            # 步骤7: 清理冲突文件
            await _rie(cleanup_conflicts, game_work_dir, self.logger)

            # 更新时间戳和统计
            def _finalize():
                db.set_meta("updated_at", datetime.now().isoformat())
                d_count = db.get_dialogue_count()
                u_count = db.get_ui_text_count()
                db.close()
                return d_count, u_count

            d_count, u_count = await _rie(_finalize)
            db = None

            progress(1.0, f'✅ 创建成功！{d_count["total"]} 条对话, {u_count["total"]} 条字符串')
            return {'success': True,
                    'dialogues': d_count['total'], 'ui_texts': u_count['total']}

        except Exception:
            if db is not None:
                try:
                    await _rie(db.close)
                except Exception:
                    pass
            raise
