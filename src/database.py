"""SQLite 数据库层（兼容门面）

每个项目一个独立的 .db 文件，使用 WAL 模式支持读写并发。

实现已按领域拆分到 db 包（base / content_repo / character_repo /
glossary_repo / embedded_repo / update_repo，多继承组合）；
本模块仅做再导出，所有 `from database import ProjectDatabase`
的调用方零改动。
"""

from db import ProjectDatabase

__all__ = ["ProjectDatabase"]
