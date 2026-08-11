"""数据库包：按领域拆分后多继承组合回 ProjectDatabase

- base.py          连接管理、锁纪律、schema 建表与迁移、项目元数据
- content_repo.py  dialogues + ui_texts 两表
- character_repo.py characters 表（含 profile、variable_map）
- glossary_repo.py glossary 表
- embedded_repo.py embedded_candidates 表
- update_repo.py   obsolete/update_review 表 + JSON 导出

公共 API 与拆分前 src/database.py 的 ProjectDatabase 完全一致；
__init__ 只定义在 Base（MRO 首位），各 mixin 直接使用 self._conn/_lock。
"""

from .base import Base
from .content_repo import ContentRepo
from .character_repo import CharacterRepo
from .glossary_repo import GlossaryRepo
from .embedded_repo import EmbeddedRepo
from .update_repo import UpdateRepo


class ProjectDatabase(Base, ContentRepo, CharacterRepo, GlossaryRepo,
                      EmbeddedRepo, UpdateRepo):
    """项目 SQLite 数据库（多继承组合，公共 API 不变）"""


__all__ = ["ProjectDatabase"]
