"""rpyc 反编译器 - 调用 unrpyc 将 .rpyc 反编译为 .rpy

只发布编译版（.rpyc）的游戏没有 .rpy 源码，
解析对话/UI 字符串前需要先反编译。
unrpyc v2.x 要求 Python 3.9+，支持 Ren'Py 8 ~ 6.18。
"""

import subprocess
from pathlib import Path

from rt_home import find_resource as _find_resource
UNRPYC = _find_resource('tools/unrpyc/unrpyc.py')

UNRPYC_RELEASE_URL = 'https://github.com/CensoredUsername/unrpyc/releases/tag/v2.0.4'


class UnrpycMissingError(RuntimeError):
    """检测到 rpyc-only 脚本但未安装 unrpyc，中断项目创建"""


# 与 renpy_parser 一致的排除规则（只反编译游戏脚本，不碰引擎和翻译目录）
_EXCLUDE_DIRS = {'renpy', 'lib', 'saves', 'cache', 'tl'}


def find_undecompiled_rpyc(game_subdir) -> list:
    """找出缺少同名 .rpy 的 .rpyc 文件"""
    game_subdir = Path(game_subdir)
    if not game_subdir.exists():
        return []
    result = []
    for rpyc in game_subdir.rglob('*.rpyc'):
        if _EXCLUDE_DIRS & set(rpyc.parts):
            continue
        if not rpyc.with_suffix('.rpy').exists():
            result.append(rpyc)
    return result


def decompile_game_rpyc(game_subdir, python_exe: str, log=print, chunk_size: int = 100) -> dict:
    """反编译游戏目录下所有缺少 .rpy 的 .rpyc

    Args:
        game_subdir: Ren'Py 项目的 game/ 子目录
        python_exe: 运行 unrpyc 的 Python 解释器（要求 3.9+）
        log: 日志回调
        chunk_size: 每次子进程调用处理的文件数（避免 Windows 命令行长度限制）

    Returns:
        {'success': [Path...], 'failed': [Path...]}（以实际产出的 .rpy 为准）
    """
    candidates = find_undecompiled_rpyc(game_subdir)
    if not candidates:
        return {'success': [], 'failed': [], 'decompiled': []}

    log(f'检测到 {len(candidates)} 个只有 .rpyc 的脚本，开始反编译...')

    if UNRPYC is None:
        raise UnrpycMissingError(
            f'检测到 {len(candidates)} 个只有 .rpyc 的脚本，'
            '需要 unrpyc 反编译，但未安装（tools/unrpyc/unrpyc.py 不存在）'
        )

    for i in range(0, len(candidates), chunk_size):
        chunk = candidates[i:i + chunk_size]
        cmd = [python_exe, str(UNRPYC)] + [str(p) for p in chunk]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=600,
            )
            # unrpyc 末尾输出汇总，提取关键行
            for line in (proc.stdout or '').splitlines():
                if 'summary' in line or 'decompiled' in line or 'fail' in line.lower():
                    log(line.strip())
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or '').strip().splitlines()[-3:]
                log(f'unrpyc 返回码 {proc.returncode}: ' + ' | '.join(tail))
        except subprocess.TimeoutExpired:
            log(f'反编译超时（第 {i // chunk_size + 1} 批），跳过该批')
        except Exception as e:
            log(f'反编译执行异常: {e}')

    # 以实际产出为准统计成功/失败；decompiled 记录 .rpy 产物路径（供导出时清理）
    success = [p for p in candidates if p.with_suffix('.rpy').exists()]
    failed = [p for p in candidates if not p.with_suffix('.rpy').exists()]
    decompiled = [p.with_suffix('.rpy') for p in success]
    log(f'反编译完成: {len(success)} 成功, {len(failed)} 失败')
    return {'success': success, 'failed': failed, 'decompiled': decompiled}
