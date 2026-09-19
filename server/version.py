"""当前版本号：发版单一来源是 pyproject.toml（CI tag 与之同步 bump）"""
from functools import lru_cache


@lru_cache(maxsize=1)
def app_version() -> str:
    """版本号：importlib.metadata 优先（uv/pip 安装态），解析 pyproject.toml
    兜底（源码直接运行 / PyInstaller 冻结——spec 把 pyproject 打进 _MEIPASS，
    否则冻结态 dist 元数据不可用只能得到 unknown）。

    lru_cache：/health 是高频探测端点，版本号进程内不变，避免每次重复
    importlib 查询/磁盘 IO。
    """
    try:
        from importlib.metadata import version
        return version('renpy-translator')
    except Exception:
        pass
    try:
        import sys
        import tomllib
        from pathlib import Path
        base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
        with (base / 'pyproject.toml').open('rb') as f:
            return tomllib.load(f)['project']['version']
    except Exception:
        return 'unknown'
