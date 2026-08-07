"""用户数据根目录（projects/config/logs/data/exports 等的基准）

解析顺序（home()）：
1. 环境变量 RT_HOME（开发/调试覆盖）
2. exe 旁指针文件 .rt_home（便携模式自定义 / 安装程序写入）
3. 平台配置目录指针文件（只读安装后自定义）
4. 冻结 & exe 目录可写 → exe 目录（便携）
5. 冻结 & 不可写 → 平台数据目录（%APPDATA%/... 等）
6. 开发态 → 仓库根

资源（tools/unrpyc、tools/python-embed、fonts）用 resources() 搜索序：
数据根 → exe 目录 → 仓库根（开发）。安装包把 tools 装在 exe 旁（只读），
用户在数据根的同名目录可覆盖。

平台目录：
- Windows: %APPDATA%/renpy-translator
- macOS:   ~/Library/Application Support/renpy-translator
- Linux:   ~/.local/share/renpy-translator
"""
import os
import sys
from pathlib import Path

POINTER_NAME = '.rt_home'
APP_DIR_NAME = 'renpy-translator'


def exe_dir() -> Path:
    """程序所在目录（冻结=exe 目录，开发=仓库根）"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def platform_dir() -> Path:
    """平台默认数据目录（始终可写）"""
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))
    return base / APP_DIR_NAME


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / '.write_test'
        probe.write_text('x')
        probe.unlink()
        return True
    except OSError:
        return False


def _read_pointer(p: Path) -> Path | None:
    try:
        text = p.read_text(encoding='utf-8').strip()
        return Path(text) if text else None
    except OSError:
        return None


def home() -> Path:
    """当前数据根目录

    平台指针优先于 exe 旁指针：只读安装（Program Files）时安装程序会写
    exe 旁指针，应用内迁移只能写平台指针——平台指针必须赢，否则迁移失效。
    set_home 每次会清掉另一侧指针，常态下两侧不会同时存在。
    """
    rt = os.environ.get('RT_HOME')
    if rt:
        return Path(rt)
    p = _read_pointer(platform_dir() / POINTER_NAME)
    if p:
        return p
    p = _read_pointer(exe_dir() / POINTER_NAME)
    if p:
        return p
    if getattr(sys, 'frozen', False):
        if _writable(exe_dir()):
            return exe_dir()
        return platform_dir()
    return exe_dir()


def set_home(new_home: Path):
    """写自定义数据根指针（exe 旁可写则写 exe 旁，否则写平台配置目录）"""
    target_exe_pointer = exe_dir() / POINTER_NAME
    if _writable(exe_dir()):
        target_exe_pointer.write_text(str(new_home), encoding='utf-8')
        # 清掉平台侧指针，避免两处不一致
        (platform_dir() / POINTER_NAME).unlink(missing_ok=True)
    else:
        platform_dir().mkdir(parents=True, exist_ok=True)
        (platform_dir() / POINTER_NAME).write_text(str(new_home), encoding='utf-8')
        # exe 旁指针（安装程序写入的）可能仍存在且只读删不掉——
        # 没关系，home() 解析时平台指针优先


def resources() -> list[Path]:
    """资源搜索路径（tools/fonts 等）：数据根 → exe 目录 → 去重"""
    seen, out = [], []
    for p in (home(), exe_dir()):
        rp = str(p.resolve())
        if rp not in seen:
            seen.append(rp)
            out.append(p)
    return out


def find_resource(rel: str) -> Path | None:
    """在资源搜索序中找第一个存在的 rel 路径"""
    for base in resources():
        p = base / rel
        if p.exists():
            return p
    return None


def temp_root() -> Path:
    """临时文件根（数据根/temp）

    所有临时文件统一落这里：避免删除失败时系统 TEMP 目录被撑爆。
    服务启动时会清空该目录（见 AppState.startup）。
    """
    p = home() / 'temp'
    p.mkdir(parents=True, exist_ok=True)
    return p
