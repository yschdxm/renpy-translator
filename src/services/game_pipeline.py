"""创建/更新管线的公共编排（creation 与 update 的重复骨架收敛于此）

两条管线只在数据落库策略上有差异（建库全新插入 vs 快照回滚 + merge
继承），其余步骤（复制、解包反编译、SDK 解析、角色统计、UI 定位）
完全一致，统一抽到本模块。

所有 async helper 接收 _rie（绑定 loop 与 None executor 的
run_in_executor 偏函数）调度阻塞调用，不自己拿事件循环。
"""
import asyncio
import json as _json
import os
import shutil
import sys
from pathlib import Path


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


def copy_game_files(src: Path, dst: Path, progress_state: dict):
    """逐文件复制游戏目录（同步，调用方放 executor 并轮询 progress_state）。

    progress_state: {'current': int, 'total': int, 'done': bool}
    """
    try:
        if dst.exists():
            shutil.rmtree(dst)
        src = Path(src)
        total = sum(1 for _ in src.rglob('*') if _.is_file())
        progress_state['total'] = total if total > 0 else 1
        dst.mkdir(parents=True, exist_ok=True)
        for root, dirs, files in os.walk(src):
            rel_root = Path(root).relative_to(src)
            dst_root = dst / rel_root
            dst_root.mkdir(parents=True, exist_ok=True)
            for f in files:
                shutil.copy2(Path(root) / f, dst_root / f)
                progress_state['current'] += 1
    finally:
        progress_state['done'] = True


async def copy_with_progress(_rie, src_dir, game_work_dir: Path, progress,
                             start_pct: float, end_pct: float,
                             label: str = '正在复制游戏文件...'):
    """复制游戏文件并轮询回报进度（两条管线共用骨架，
    仅进度区间 [start_pct, end_pct] 与文案不同）"""
    copy_progress = {'current': 0, 'total': 0, 'done': False}
    copy_task = _rie(copy_game_files, Path(src_dir), game_work_dir,
                     copy_progress)
    while not copy_progress['done']:
        total, current = copy_progress['total'], copy_progress['current']
        if total > 0:
            progress(start_pct + (current / total) * (end_pct - start_pct),
                     f'{label} ({current}/{total})')
        await asyncio.sleep(0.3)
    await copy_task


async def unpack_and_decompile(_rie, game_work_dir: Path, db, logger,
                               progress) -> list:
    """解包 rpa + 反编译 rpyc，反编译产物清单写 meta（供导出时清理）。

    反编译必须在 SDK 生成前：translate 只能从 .rpy 源生成模板
    （实测：同一脚本编译成 .rpyc 删掉 .rpy 后就不再产出模板），
    纯 .rpyc 游戏的反编译产物是模板生成的唯一来源。

    产物清单覆盖写：更新管线里旧版本/旧 .broken 条目已不存在，
    追加/保留会留垃圾；创建管线此时 meta 本来就为空，覆盖写等价。
    返回反编译产物的相对路径列表（供 SDK 自愈隔离判断）。
    """
    from renpy_parser import RenpyParser
    progress(0.30, '正在解包游戏资源...')
    parser = RenpyParser()

    def _parse():
        return parser.parse_directory(
            str(game_work_dir), extract_rpa=True,
            work_dir=str(game_work_dir),
            decompile_rpyc=True, python_exe=_unrpyc_python_exe()
        )

    result = await _rie(_parse)

    # rpyc 反编译结果：记录日志，产出清单存 meta 供导出时清理
    if result.get('decompiled_rpyc_ok') or result.get('decompiled_rpyc_fail'):
        logger.info(
            f'rpyc 反编译: {result["decompiled_rpyc_ok"]} 成功, '
            f'{result["decompiled_rpyc_fail"]} 失败', panel='projects')
    rel_files = [
        Path(p).relative_to(game_work_dir).as_posix()
        for p in result.get('decompiled_files', [])
    ]
    await _rie(db.set_meta, 'decompiled_rpy_files', _json.dumps(rel_files))
    progress(0.50, '解包完成')
    return rel_files


async def resolve_sdk_or_raise(_rie, get_sdk_path, game_work_dir: Path,
                               action: str) -> str:
    """按游戏引擎大版本解析匹配的 SDK 路径。

    游戏版本可探测但没有匹配大版本的 SDK（Ren'Py 7 与 8 的 .rpyc 互不
    兼容）时直接报错：继续走只会得到没有对话的空项目。
    action: '创建' 或 '更新'，仅用于报错文案。

    返回 '' 表示无 SDK 但允许继续（仅创建管线：版本都探测不到时按
    纯源码游戏处理，可以没有 SDK 模板）。
    """
    sdk_path = get_sdk_path(str(game_work_dir)) if get_sdk_path else ''
    if sdk_path:
        return sdk_path
    from sdk_manager import detect_engine_version
    gv = await _rie(detect_engine_version, game_work_dir)
    if not gv:
        if action == '更新':
            raise Exception("未配置 Ren'Py SDK 路径")
        return ''
    hint = '（7.x 建议 7.4.11）' if gv[0] == 7 else ''
    raise Exception(
        f"游戏引擎为 Ren'Py {'.'.join(map(str, gv))}，"
        f'但未安装 {gv[0]}.x 的 SDK。\n'
        '启动时的自动补装可能仍在下载（见任务列表），可稍候重试；'
        '若下载失败，请在模型配置页手动下载对应版本的 SDK'
        f'{hint}后重新{action}')


def refresh_characters(game_work_dir: Path, db, dialogues: list,
                       reset_counts: bool = False):
    """解析角色 + 统计各角色台词数（同步，调用方放 executor）。

    reset_counts: 更新管线重算前先清零（update_character_lines_count
    只 set 不清零）；创建管线全新插入无需清零。
    insert_characters 按变量名合并，保留 cn_name/profile；
    新版删除的角色保留不删。
    """
    from renpy_parser import RenpyParser
    char_result = RenpyParser().parse_directory(
        str(game_work_dir), extract_rpa=False)
    characters = [{"variable": c.variable, "display_name": c.name}
                  for c in char_result['characters']]
    if reset_counts:
        db.reset_character_lines_count()
    db.insert_characters(characters)

    # 统计每个角色的台词数并更新 characters 表
    line_counts = {}
    for d in dialogues:
        char = d.get('character', '')
        if char:
            line_counts[char] = line_counts.get(char, 0) + 1
    # variable_map: variable -> display_name
    var_map = db.get_variable_map()
    for var_name, count in line_counts.items():
        db.update_character_lines_count(var_map.get(var_name, var_name), count)


async def locate_ui_hints(_rie, game_work_dir: Path, db, logger,
                          action: str):
    """回扫源码定位 UI 字符串出处并写回 context_hint；失败仅告警不阻断。

    action: '建项' 或 '更新'，仅用于告警文案。
    """
    from renpy_parser import RenpyParser
    try:
        hints = await _rie(
            RenpyParser().locate_ui_string_contexts, str(game_work_dir))
        matched = await _rie(db.update_ui_hints, hints)
        logger.info(f'UI 上下文定位: {matched} 条命中', panel='projects')
    except Exception as e:
        logger.warning(f'UI 上下文定位失败（不影响{action}）: {e}',
                       panel='projects')
