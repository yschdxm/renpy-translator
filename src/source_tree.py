"""源码树缓存：一次扫描/读取，全树复用

AI 预筛（粗筛代码片段、精审 read_code/search_code 工具）与静态用途分析
（UsageAnalyzer 出现点搜索）都要反复读游戏 .rpy 源码：逐候选整文件读、
每次工具调用全树 rglob+全量读。本类把 rel_path -> 行列表 缓存起来，
带 mtime 校验失效（apply_wrapping 改写源码后下次读取自动重载）。
"""
from pathlib import Path

_EXCLUDE_DIRS = {'renpy', 'lib', 'saves', 'cache', 'tl', 'output',
                 'audio', 'sound', 'images', 'image', 'fonts', 'font',
                 'video', 'movies'}


class SourceTree:
    """游戏 .rpy 源码树的懒加载缓存

    - files(): 全树 .rpy 相对路径（posix，排除引擎/资源目录），首次调用扫描
    - lines(rel): 行列表（\\n 切分），带 mtime 校验，文件变更后自动重载
    - search(query): 全树子串搜索，复用已缓存内容
    """

    def __init__(self, game_root: str):
        """game_root: 与 find_candidates 的 rel_file 基准一致的源码根目录"""
        self.root = Path(game_root)
        self._file_list = None   # list[str] | None（懒扫描）
        self._cache = {}         # rel -> (mtime, lines)

    def files(self) -> list:
        """全树 .rpy 相对路径列表（排序、去重）"""
        if self._file_list is None:
            out = []
            for rpy in sorted(self.root.rglob('*.rpy')):
                if _EXCLUDE_DIRS & set(rpy.parts):
                    continue
                out.append(rpy.relative_to(self.root).as_posix())
            self._file_list = out
        return self._file_list

    def lines(self, rel: str) -> list:
        """读文件行列表（缓存，mtime 变化自动重载）；读不到返回 []"""
        rel = str(rel).replace('\\', '/')
        path = self.root / rel
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        entry = self._cache.get(rel)
        if entry is not None and entry[0] == mtime:
            return entry[1]
        try:
            lines = path.read_text(
                encoding='utf-8', errors='ignore').split('\n')
        except OSError:
            return []
        self._cache[rel] = (mtime, lines)
        return lines

    def search(self, query: str):
        """全树子串搜索，yield (rel, 行号(1-based), 行文本)"""
        for rel in self.files():
            for line_no, line in enumerate(self.lines(rel), 1):
                if query in line:
                    yield rel, line_no, line

    def as_dict(self) -> dict:
        """物化 {rel: 行列表}（UsageAnalyzer 的 files 注入用）

        与缓存共享同一批 list 对象，后续 lines() 的 mtime 失效重载
        不会影响已物化的字典（单次分析调用内可接受）。
        """
        return {rel: self.lines(rel) for rel in self.files()}
