"""Ren'Py SDK 管理器 - 负责调用 Ren'Py SDK 功能"""

import os
import re
import sys
import subprocess
import time
from pathlib import Path

# 7.x 及更早：__init__.py 里字面量 version_tuple = (7, 4, 9, vc_version)
_VER_TUPLE_RE = re.compile(r'version_tuple\s*=\s*\((\d+),\s*(\d+),\s*(\d+)')
# 8.x 的 __init__.py 版本是动态计算的，改从 SDK 目录名解析
_DIR_NAME_RE = re.compile(r'renpy-(\d+)\.(\d+)\.(\d+)-sdk')


def detect_engine_version(base) -> tuple | None:
    """探测 Ren'Py 引擎版本 (major, minor, patch)。

    base: 游戏目录（读取其内置 renpy/__init__.py；7.x 可直接解析，
    8.x 游戏读不到则返回 None）或 SDK 目录（从目录名解析）。
    """
    base = Path(base)
    text = ''
    try:
        text = (base / 'renpy' / '__init__.py').read_text(
            encoding='utf-8', errors='ignore')
    except OSError:
        pass
    m = _VER_TUPLE_RE.search(text)
    if m:
        return tuple(int(g) for g in m.groups())
    m = _DIR_NAME_RE.search(base.name)
    if m:
        return tuple(int(g) for g in m.groups())
    return None


# 常备 SDK 默认版本：8.x 跟随当前稳定版；7.x 为 7.4 系列最终维护版
# （Ren'Py 7 已停更，读 7.x 游戏的 .rpyc 必须用它）
DEFAULT_SDK_8 = '8.5.3'
DEFAULT_SDK_7 = '7.4.11'


def find_installed_sdks() -> list:
    """扫描默认目录下已安装的 SDK，返回 [(version_tuple, path)]。

    覆盖：数据根与 exe 目录下的 tools/renpy-*-sdk、exe 目录下的
    renpy-*-sdk（开发态仓库根）。不支持自定义目录。
    """
    from rt_home import resources
    mgr = SDKManager()
    found, seen = [], set()

    def _add(path):
        path = Path(path)
        key = str(path.resolve())
        if key in seen or not mgr.is_valid_sdk(path):
            return
        v = detect_engine_version(path)
        if v:
            seen.add(key)
            found.append((v, path))

    for base in resources():
        tools = base / 'tools'
        if tools.is_dir():
            for cand in sorted(tools.glob('renpy-*-sdk')):
                _add(cand)
        for cand in sorted(base.glob('renpy-*-sdk')):
            _add(cand)
    return found


class SDKManager:
    """Ren'Py SDK 管理器"""

    def __init__(self, sdk_path: str = ""):
        self.sdk_path = Path(sdk_path) if sdk_path else None

    def is_valid_sdk(self, path: Path) -> bool:
        """检查是否是有效的 Ren'Py SDK"""
        # 检查必要的文件是否存在
        renpy_exe = self.get_renpy_exe(path)
        return renpy_exe.exists()

    def get_renpy_exe(self, sdk_path: Path = None) -> Path:
        """获取 renpy 可执行文件路径（多候选：Linux SDK 根的 renpy.sh、
        macOS .app 内脚本/二进制等）"""
        if sdk_path is None:
            sdk_path = self.sdk_path

        if sys.platform == 'win32':
            return sdk_path / 'renpy.exe'

        for rel in ('renpy.sh',
                    'renpy.app/Contents/MacOS/renpy.sh',
                    'renpy.app/Contents/MacOS/renpy'):
            p = sdk_path / rel
            if p.exists():
                return p
        # 都不存在时返回默认路径（is_valid_sdk 判定失败，错误信息含路径）
        return sdk_path / 'renpy.sh'

    def generate_translations(self, game_dir: str, language: str = "chinese",
                              cancel_event=None) -> dict:
        """调用 Ren'Py 生成翻译文件

        Args:
            game_dir: 游戏目录路径
            language: 目标语言
            cancel_event: 可选 threading.Event，置位时终止子进程并返回
                {'success': False, 'cancelled': True, ...}

        Returns:
            {'success': bool, 'message': str, 'output': str}
            取消时额外含 'cancelled': True
        """
        if not self.sdk_path:
            return {'success': False, 'message': '未配置 Ren\'Py SDK 路径', 'output': ''}

        renpy_exe = self.get_renpy_exe()
        if not renpy_exe.exists():
            return {'success': False, 'message': f'找不到 {renpy_exe}', 'output': ''}

        proc = None
        try:
            # 构建命令 - 使用正确的 translate 命令
            cmd = [str(renpy_exe), str(game_dir), "translate", language]

            print(f'[SDK] 执行命令: {" ".join(cmd)}')

            # 执行命令
            # cwd 必须用 SDK 目录而非游戏目录：游戏发行目录自带 renpy/
            # 引擎包，cwd 在游戏目录时它会抢占 sys.path——Python 代码用
            # 游戏的、原生模块（librenpython.dll 里的 render）用 SDK 的，
            # 大版本不一致即崩溃（如 'Cache' object has no attribute
            # 'get_renders'）。cwd 在 SDK 目录时两处都来自同一 SDK，自洽。
            #
            # Popen + 轮询而非 subprocess.run(timeout=...)：run 阻塞期间
            # 任务无法取消（最长 1 小时），轮询让 cancel_event 能及时生效
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(self.sdk_path)
            )

            deadline = time.monotonic() + 3600
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    # 取消：先 terminate 给子进程退出机会，不行再强杀
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    return {'success': False, 'cancelled': True,
                            'message': '已取消', 'output': ''}
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    return {'success': False, 'message': '执行超时', 'output': ''}
                time.sleep(0.5)

            output = proc.stdout.read() if proc.stdout else ''
            print(f'[SDK] 输出:\n{output}')

            if proc.returncode == 0:
                return {
                    'success': True,
                    'message': f'成功生成 {language} 翻译文件',
                    'output': output
                }
            else:
                return {
                    'success': False,
                    'message': f'生成失败 (返回码: {proc.returncode})',
                    'output': output
                }

        except Exception as e:
            if proc is not None and proc.poll() is None:
                proc.kill()
            return {'success': False, 'message': str(e), 'output': ''}
        finally:
            if proc is not None and proc.stdout:
                proc.stdout.close()

    def list_languages(self, game_dir: str) -> list:
        """列出已有的翻译语言"""
        tl_dir = Path(game_dir) / 'game' / 'tl'
        if not tl_dir.exists():
            return []

        languages = []
        for item in tl_dir.iterdir():
            if item.is_dir() and not item.name.startswith('_'):
                languages.append(item.name)

        return languages
