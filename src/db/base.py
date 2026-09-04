"""数据库基类：连接管理、锁纪律、schema 建表与迁移、项目元数据"""

import sqlite3
import threading
from pathlib import Path
from typing import Optional
from contextlib import contextmanager
from functools import wraps


def _auto_reconnect(method):
    """装饰器：方法执行前确保数据库连接有效，并全程持有实例锁

    全库共享一条 sqlite 连接（check_same_thread=False），多线程并发
    execute/commit 会互相破坏事务（别的线程的 commit 会把本线程未完成的
    事务提前提交），跨线程同时 execute 还可能抛递归游标 ProgrammingError。
    因此把锁纪律收敛到本层：所有公共读写方法都经此装饰器串行化，
    绕过 HTTP 层 db_lock 的调用方（如 TranslationService._save_all）也安全。
    RLock 允许方法内重入调用其他带锁方法（如 to_json_dict → get_all_meta）。
    """
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            if self._conn is None:
                self.connect()
            return method(self, *args, **kwargs)
    return wrapper


# 建表 SQL
_SCHEMA_SQL = """
-- 项目元数据
CREATE TABLE IF NOT EXISTS project_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- 对话翻译
CREATE TABLE IF NOT EXISTS dialogues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL DEFAULT '',
    line_number INTEGER DEFAULT 0,
    label TEXT DEFAULT '',
    character TEXT DEFAULT '',
    original_text TEXT NOT NULL,
    translated_text TEXT DEFAULT '',
    is_translated INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dialogues_label ON dialogues(label);
CREATE INDEX IF NOT EXISTS idx_dialogues_translated ON dialogues(is_translated);
CREATE INDEX IF NOT EXISTS idx_dialogues_character ON dialogues(character);
CREATE INDEX IF NOT EXISTS idx_dialogues_file ON dialogues(file_path);

-- UI 字符串翻译
CREATE TABLE IF NOT EXISTS ui_texts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL DEFAULT '',
    line_number INTEGER DEFAULT 0,
    label TEXT DEFAULT '',
    original_text TEXT NOT NULL,
    translated_text TEXT DEFAULT '',
    is_translated INTEGER DEFAULT 0,
    context_hint TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ui_label ON ui_texts(label);
CREATE INDEX IF NOT EXISTS idx_ui_translated ON ui_texts(is_translated);
CREATE INDEX IF NOT EXISTS idx_ui_file ON ui_texts(file_path);

-- 角色表（合并 characters + char_dict + char_profiles）
-- 主键身份是 variable（Character() 变量名）；display_name 允许重复
-- （不同角色可故意同名，如两个 "Unknown"）
CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variable TEXT DEFAULT '',
    display_name TEXT NOT NULL,
    cn_name TEXT DEFAULT '',
    lines_count INTEGER DEFAULT 0,
    profile_json TEXT DEFAULT '',
    is_placeholder INTEGER DEFAULT 0,
    created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_characters_display ON characters(display_name);
CREATE INDEX IF NOT EXISTS idx_characters_cn ON characters(cn_name);

-- 术语表
CREATE TABLE IF NOT EXISTS glossary (
    en_term TEXT PRIMARY KEY,
    cn_term TEXT DEFAULT '',
    term_type TEXT DEFAULT 'other',
    source TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_glossary_type ON glossary(term_type);

-- 内嵌文本候选（AI 判断持久化：重开不丢，支持重判）
CREATE TABLE IF NOT EXISTS embedded_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_file TEXT DEFAULT '',
    line INTEGER DEFAULT 0,
    col_start INTEGER DEFAULT 0,
    raw TEXT DEFAULT '',
    text TEXT DEFAULT '',
    kind TEXT DEFAULT '',
    hint TEXT DEFAULT '',
    confidence TEXT DEFAULT '',
    ai_keep INTEGER DEFAULT -1,
    ai_reason TEXT DEFAULT '',
    ai_danger INTEGER DEFAULT 0,
    ai_evidence TEXT DEFAULT '',
    apply_path TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    updated_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_embedded_status ON embedded_candidates(status);

-- 判定历史（翻转审计链：灰区复核目标来源）
CREATE TABLE IF NOT EXISTS embedded_verdict_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id INTEGER DEFAULT 0,
    ai_keep INTEGER DEFAULT -1,
    ai_reason TEXT DEFAULT '',
    stage TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_verdict_log_row ON embedded_verdict_log(row_id);

-- 版本更新后失效的旧译文（新版游戏中已不存在的原文）
CREATE TABLE IF NOT EXISTS obsolete_translations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT DEFAULT '',
    file_path TEXT DEFAULT '',
    character TEXT DEFAULT '',
    original_text TEXT NOT NULL,
    translated_text TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);

-- 批次解析失败（句数不匹配）的暂存批次，供翻译页分批人工核验
CREATE TABLE IF NOT EXISTS failed_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_type TEXT NOT NULL,
    items_json TEXT NOT NULL,
    error TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_failed_batches_type ON failed_batches(content_type);

-- 版本更新时的模糊匹配复核（微改句子的旧译文待人工确认/审计）
CREATE TABLE IF NOT EXISTS update_review (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT DEFAULT '',
    target_id INTEGER DEFAULT 0,
    new_original TEXT DEFAULT '',
    old_original TEXT DEFAULT '',
    old_translation TEXT DEFAULT '',
    ratio REAL DEFAULT 0,
    status TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_update_review_status ON update_review(status);

-- 剧情图节点（派生数据，构建时全量重算）
CREATE TABLE IF NOT EXISTS story_nodes (
    label TEXT PRIMARY KEY,
    file_path TEXT DEFAULT '',
    line_start INTEGER DEFAULT 0,
    line_end INTEGER DEFAULT 0,
    speakers_json TEXT DEFAULT '[]',
    dialogue_count INTEGER DEFAULT 0,
    first_text TEXT DEFAULT '',
    first_text_cn TEXT DEFAULT '',
    is_entry INTEGER DEFAULT 0,
    has_return INTEGER DEFAULT 0,
    is_terminal INTEGER DEFAULT 0,
    thumb_file TEXT DEFAULT ''
);

-- 剧情图边（target 为 NULL 表示动态跳转未决）
CREATE TABLE IF NOT EXISTS story_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT DEFAULT '',
    target TEXT,
    kind TEXT DEFAULT 'jump',
    branch TEXT DEFAULT '',
    text TEXT DEFAULT '',
    text_cn TEXT DEFAULT '',
    expr TEXT DEFAULT '',
    line INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_story_edges_source ON story_edges(source);
CREATE INDEX IF NOT EXISTS idx_story_edges_target ON story_edges(target);

-- 人物关系（source=ai 随重算替换；source=manual 人工编辑保留）
CREATE TABLE IF NOT EXISTS char_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_var TEXT DEFAULT '',
    target_var TEXT DEFAULT '',
    relation TEXT DEFAULT '',
    category TEXT DEFAULT 'other',
    polarity TEXT DEFAULT '',
    description TEXT DEFAULT '',
    cooccurrence INTEGER DEFAULT 0,
    source TEXT DEFAULT 'ai',
    updated_at TEXT DEFAULT ''
);

-- 角色立绘（派生数据，随剧情图构建重算）
CREATE TABLE IF NOT EXISTS char_avatars (
    variable TEXT PRIMARY KEY,
    image_path TEXT DEFAULT '',
    source TEXT DEFAULT 'auto'
);

-- 剧情场景（label 链聚合的故事段落，派生数据，构建时全量重算）
CREATE TABLE IF NOT EXISTS story_scenes (
    scene_id TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    labels_json TEXT DEFAULT '[]',
    speakers_json TEXT DEFAULT '[]',
    first_text TEXT DEFAULT '',
    first_text_cn TEXT DEFAULT '',
    dialogue_count INTEGER DEFAULT 0,
    translated_count INTEGER DEFAULT 0,
    thumb_file TEXT DEFAULT '',
    is_entry INTEGER DEFAULT 0,
    is_ending INTEGER DEFAULT 0,
    is_return INTEGER DEFAULT 0,
    file_path TEXT DEFAULT '',
    line_start INTEGER DEFAULT 0
);

-- 剧情场景间边（texts_json/texts_cn_json 为选项文本列表）
CREATE TABLE IF NOT EXISTS story_scene_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT DEFAULT '',
    target TEXT DEFAULT '',
    texts_json TEXT DEFAULT '[]',
    texts_cn_json TEXT DEFAULT '[]',
    branch TEXT DEFAULT '',
    has_call INTEGER DEFAULT 0,
    unresolved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scene_edges_source ON story_scene_edges(source);
"""


class Base:
    """连接管理、schema 与迁移、项目元数据"""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        # 实例级可重入锁：串行化对共享连接的一切读写（含事务），
        # 所有经 @_auto_reconnect 的公共方法、connect/close/_transaction 都持有它
        self._lock = threading.RLock()

    def connect(self):
        """连接数据库，启用 WAL 模式，创建表结构"""
        with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA cache_size=-64000")
            self._conn.executescript(_SCHEMA_SQL)
            # 迁移：给已有表补上缺失的列
            self._migrate_columns()
            # 迁移：拆除 characters.display_name 的 UNIQUE 约束
            self._migrate_characters_unique()
            # 迁移：旧库 marked 行的 apply_path=''（语义=table，但
            # get_table_marked_embedded 查不到，导致这类标记从未生效）
            self._migrate_embedded_apply_path()
            self._conn.commit()

    def _migrate_columns(self):
        """给已有表补上缺失的列（兼容旧数据库）"""
        for table, col, col_def in [
            ('dialogues', 'label', "TEXT DEFAULT ''"),
            ('ui_texts', 'label', "TEXT DEFAULT ''"),
            ('ui_texts', 'context_hint', "TEXT DEFAULT ''"),
            ('embedded_candidates', 'ai_danger', "INTEGER DEFAULT 0"),
            ('embedded_candidates', 'ai_evidence', "TEXT DEFAULT ''"),
            ('embedded_candidates', 'apply_path', "TEXT DEFAULT ''"),
            ('story_nodes', 'first_text_cn', "TEXT DEFAULT ''"),
            ('story_edges', 'text_cn', "TEXT DEFAULT ''"),
            ('char_relations', 'category', "TEXT DEFAULT 'other'"),
            ('char_relations', 'polarity', "TEXT DEFAULT ''"),
            ('characters', 'faction', "TEXT DEFAULT ''"),
        ]:
            existing = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")

    def _migrate_embedded_apply_path(self):
        """旧库 marked 行的 apply_path='' 归一为 'table'

        apply_selection 的 _path_of 本就默认 '' → table，但
        get_table_marked_embedded 只查 apply_path='table'，'' 行
        的译文条目因此从未生成。归一后 get_marked_embedded
        （apply_path != 'table'）即 wrap-only。"""
        self._conn.execute(
            "UPDATE embedded_candidates SET apply_path='table' "
            "WHERE status='marked' AND (apply_path IS NULL OR apply_path='')"
        )

    def _migrate_characters_unique(self):
        """拆除旧库 characters.display_name 的 UNIQUE 约束

        同名不同角色（如两个 "Unknown"）需要并存，旧约束会导致
        按变量名插入同名角色时报错。SQLite 不能直接删约束，重建表。
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='characters'"
        ).fetchone()
        if not row or 'UNIQUE' not in (row['sql'] or '').upper():
            return
        self._conn.executescript('''
            CREATE TABLE characters_mig (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                variable TEXT DEFAULT '',
                display_name TEXT NOT NULL,
                cn_name TEXT DEFAULT '',
                lines_count INTEGER DEFAULT 0,
                profile_json TEXT DEFAULT '',
                is_placeholder INTEGER DEFAULT 0,
                created_at TEXT DEFAULT ''
            );
            INSERT INTO characters_mig
                SELECT id, variable, display_name, cn_name, lines_count,
                       profile_json, is_placeholder, created_at FROM characters;
            DROP TABLE characters;
            ALTER TABLE characters_mig RENAME TO characters;
            CREATE INDEX IF NOT EXISTS idx_characters_display ON characters(display_name);
            CREATE INDEX IF NOT EXISTS idx_characters_cn ON characters(cn_name);
        ''')

    def close(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    @_auto_reconnect
    def checkpoint_wal(self):
        """备份 db 文件前调用：把 WAL 内容并入库文件并截断"""
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @contextmanager
    def _transaction(self):
        assert self._conn, "数据库未连接"
        # 事务全程持锁（RLock，调用方方法已持锁时可重入）：
        # 防止其他线程的 execute+commit 穿插进来把未完成的事务提前提交
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ========== 项目元数据 ==========

    @_auto_reconnect
    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM project_meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    @_auto_reconnect
    def set_meta(self, key: str, value: str):
        self._conn.execute(
            "INSERT OR REPLACE INTO project_meta (key, value) VALUES (?, ?)",
            (key, value)
        )
        self._conn.commit()

    @_auto_reconnect
    def get_all_meta(self) -> dict:
        rows = self._conn.execute("SELECT key, value FROM project_meta").fetchall()
        return {row["key"]: row["value"] for row in rows}
