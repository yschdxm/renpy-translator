"""SQLite 数据库层 - 替代 JSON 文件存储项目数据

每个项目一个独立的 .db 文件，使用 WAL 模式支持读写并发。
单条翻译后保存只需 UPDATE 单行（毫秒级），不再重写整个文件。
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional
from contextlib import contextmanager


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
    character TEXT DEFAULT '',
    original_text TEXT NOT NULL,
    translated_text TEXT DEFAULT '',
    is_translated INTEGER DEFAULT 0,
    context_before TEXT DEFAULT '[]',
    context_after TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_dialogues_translated ON dialogues(is_translated);
CREATE INDEX IF NOT EXISTS idx_dialogues_character ON dialogues(character);
CREATE INDEX IF NOT EXISTS idx_dialogues_file ON dialogues(file_path);

-- UI 字符串翻译
CREATE TABLE IF NOT EXISTS ui_texts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL DEFAULT '',
    line_number INTEGER DEFAULT 0,
    original_text TEXT NOT NULL,
    translated_text TEXT DEFAULT '',
    is_translated INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ui_translated ON ui_texts(is_translated);
CREATE INDEX IF NOT EXISTS idx_ui_file ON ui_texts(file_path);

-- 人名词典
CREATE TABLE IF NOT EXISTS char_dict (
    en_name TEXT PRIMARY KEY,
    cn_name TEXT DEFAULT ''
);

-- 角色信息
CREATE TABLE IF NOT EXISTS characters (
    variable TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    chinese_name TEXT DEFAULT ''
);

-- 角色分析档案
CREATE TABLE IF NOT EXISTS char_profiles (
    name TEXT PRIMARY KEY,
    profile_json TEXT NOT NULL
);
"""


class ProjectDatabase:
    """项目 SQLite 数据库

    使用 WAL 模式，支持读写并发。
    单条翻译后保存只需 UPDATE 单行（毫秒级）。
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self):
        """连接数据库，启用 WAL 模式，创建表结构"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-64000")  # 64MB 缓存
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    def close(self):
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @contextmanager
    def _transaction(self):
        """事务上下文管理器"""
        assert self._conn, "数据库未连接"
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ========== 项目元数据 ==========

    def get_meta(self, key: str, default: str = "") -> str:
        """获取元数据"""
        row = self._conn.execute(
            "SELECT value FROM project_meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str):
        """设置元数据"""
        self._conn.execute(
            "INSERT OR REPLACE INTO project_meta (key, value) VALUES (?, ?)",
            (key, value)
        )
        self._conn.commit()

    def get_all_meta(self) -> dict:
        """获取所有元数据"""
        rows = self._conn.execute("SELECT key, value FROM project_meta").fetchall()
        return {row["key"]: row["value"] for row in rows}

    # ========== 对话操作 ==========

    def insert_dialogues(self, items: list[dict]):
        """批量插入对话（创建项目时使用）"""
        with self._transaction():
            self._conn.executemany(
                """INSERT INTO dialogues
                   (file_path, line_number, character, original_text,
                    translated_text, is_translated, context_before, context_after)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(
                    d.get("file_path", ""),
                    d.get("line_number", 0),
                    d.get("character", ""),
                    d.get("original_text", ""),
                    d.get("translated_text", ""),
                    1 if d.get("is_translated") else 0,
                    json.dumps(d.get("context_before", []), ensure_ascii=False),
                    json.dumps(d.get("context_after", []), ensure_ascii=False),
                ) for d in items]
            )

    def update_dialogue(self, item_id: int, translated_text: str):
        """翻译单条对话后保存（毫秒级）"""
        self._conn.execute(
            "UPDATE dialogues SET translated_text=?, is_translated=1 WHERE id=?",
            (translated_text, item_id)
        )
        self._conn.commit()

    def update_dialogues_batch(self, updates: list[tuple[int, str]]):
        """批量更新对话翻译（单事务提交）

        Args:
            updates: [(id, translated_text), ...]
        """
        with self._transaction():
            self._conn.executemany(
                "UPDATE dialogues SET translated_text=?, is_translated=1 WHERE id=?",
                [(text, id_) for id_, text in updates]
            )

    def get_dialogues_page(self, page: int = 0, page_size: int = 50,
                           filter_mode: str = 'all',
                           character: str = '',
                           search: str = '') -> tuple[list[dict], int]:
        """分页查询对话（带筛选）

        Returns:
            (items, total_count)
        """
        where_clauses = []
        params = []

        if filter_mode == 'untranslated':
            where_clauses.append("is_translated=0")
        elif filter_mode == 'translated':
            where_clauses.append("is_translated=1")

        if character:
            where_clauses.append("character=?")
            params.append(character)

        if search:
            where_clauses.append("(original_text LIKE ? OR translated_text LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # 总数
        count_row = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM dialogues{where_sql}", params
        ).fetchone()
        total = count_row["cnt"]

        # 分页数据
        offset = page * page_size
        rows = self._conn.execute(
            f"SELECT * FROM dialogues{where_sql} ORDER BY id LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()

        items = [self._row_to_dialogue_dict(row) for row in rows]
        return items, total

    def get_untranslated_dialogues(self, limit: int = None) -> list[dict]:
        """获取未翻译的对话"""
        sql = "SELECT * FROM dialogues WHERE is_translated=0 ORDER BY id"
        if limit:
            sql += f" LIMIT {limit}"
        rows = self._conn.execute(sql).fetchall()
        return [self._row_to_dialogue_dict(row) for row in rows]

    def get_dialogue(self, item_id: int) -> Optional[dict]:
        """获取单条对话"""
        row = self._conn.execute(
            "SELECT * FROM dialogues WHERE id=?", (item_id,)
        ).fetchone()
        return self._row_to_dialogue_dict(row) if row else None

    def get_dialogue_neighbors(self, item_id: int, count: int = 3) -> tuple[list[str], list[str]]:
        """获取对话的上下文（前后的对话文本）"""
        before_rows = self._conn.execute(
            "SELECT original_text FROM dialogues WHERE id < ? ORDER BY id DESC LIMIT ?",
            (item_id, count)
        ).fetchall()
        after_rows = self._conn.execute(
            "SELECT original_text FROM dialogues WHERE id > ? ORDER BY id ASC LIMIT ?",
            (item_id, count)
        ).fetchall()
        return (
            [r["original_text"] for r in reversed(before_rows)],
            [r["original_text"] for r in after_rows]
        )

    def get_dialogue_count(self) -> dict:
        """统计对话数量"""
        row = self._conn.execute(
            "SELECT COUNT(*) as total, SUM(is_translated) as translated FROM dialogues"
        ).fetchone()
        total = row["total"] or 0
        translated = row["translated"] or 0
        return {"total": total, "translated": translated, "untranslated": total - translated}

    def get_dialogue_characters(self) -> list[str]:
        """获取所有出现的角色（去重）"""
        rows = self._conn.execute(
            "SELECT DISTINCT character FROM dialogues WHERE character != '' ORDER BY character"
        ).fetchall()
        return [r["character"] for r in rows]

    @staticmethod
    def _row_to_dialogue_dict(row: sqlite3.Row) -> dict:
        """将数据库行转为字典"""
        return {
            "id": row["id"],
            "file_path": row["file_path"],
            "line_number": row["line_number"],
            "character": row["character"],
            "original_text": row["original_text"],
            "translated_text": row["translated_text"],
            "is_translated": bool(row["is_translated"]),
            "context_before": json.loads(row["context_before"]) if row["context_before"] else [],
            "context_after": json.loads(row["context_after"]) if row["context_after"] else [],
        }

    # ========== UI 字符串操作 ==========

    def insert_ui_texts(self, items: list[dict]):
        """批量插入 UI 字符串"""
        with self._transaction():
            self._conn.executemany(
                """INSERT INTO ui_texts
                   (file_path, line_number, original_text, translated_text, is_translated)
                   VALUES (?, ?, ?, ?, ?)""",
                [(
                    d.get("file_path", ""),
                    d.get("line_number", 0),
                    d.get("original_text", ""),
                    d.get("translated_text", ""),
                    1 if d.get("is_translated") else 0,
                ) for d in items]
            )

    def update_ui_text(self, item_id: int, translated_text: str):
        """翻译单条 UI 字符串后保存（毫秒级）"""
        self._conn.execute(
            "UPDATE ui_texts SET translated_text=?, is_translated=1 WHERE id=?",
            (translated_text, item_id)
        )
        self._conn.commit()

    def update_ui_texts_batch(self, updates: list[tuple[int, str]]):
        """批量更新 UI 字符串翻译（单事务提交）"""
        with self._transaction():
            self._conn.executemany(
                "UPDATE ui_texts SET translated_text=?, is_translated=1 WHERE id=?",
                [(text, id_) for id_, text in updates]
            )

    def get_ui_texts_page(self, page: int = 0, page_size: int = 50,
                          filter_mode: str = 'all',
                          search: str = '') -> tuple[list[dict], int]:
        """分页查询 UI 字符串"""
        where_clauses = []
        params = []

        if filter_mode == 'untranslated':
            where_clauses.append("is_translated=0")
        elif filter_mode == 'translated':
            where_clauses.append("is_translated=1")

        if search:
            where_clauses.append("(original_text LIKE ? OR translated_text LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        count_row = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM ui_texts{where_sql}", params
        ).fetchone()
        total = count_row["cnt"]

        offset = page * page_size
        rows = self._conn.execute(
            f"SELECT * FROM ui_texts{where_sql} ORDER BY id LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()

        items = [self._row_to_ui_dict(row) for row in rows]
        return items, total

    def get_untranslated_ui_texts(self, limit: int = None) -> list[dict]:
        """获取未翻译的 UI 字符串"""
        sql = "SELECT * FROM ui_texts WHERE is_translated=0 ORDER BY id"
        if limit:
            sql += f" LIMIT {limit}"
        rows = self._conn.execute(sql).fetchall()
        return [self._row_to_ui_dict(row) for row in rows]

    def get_ui_text(self, item_id: int) -> Optional[dict]:
        """获取单条 UI 字符串"""
        row = self._conn.execute(
            "SELECT * FROM ui_texts WHERE id=?", (item_id,)
        ).fetchone()
        return self._row_to_ui_dict(row) if row else None

    def get_ui_text_count(self) -> dict:
        """统计 UI 字符串数量"""
        row = self._conn.execute(
            "SELECT COUNT(*) as total, SUM(is_translated) as translated FROM ui_texts"
        ).fetchone()
        total = row["total"] or 0
        translated = row["translated"] or 0
        return {"total": total, "translated": translated, "untranslated": total - translated}

    @staticmethod
    def _row_to_ui_dict(row: sqlite3.Row) -> dict:
        """将数据库行转为字典"""
        return {
            "id": row["id"],
            "file_path": row["file_path"],
            "line_number": row["line_number"],
            "original_text": row["original_text"],
            "translated_text": row["translated_text"],
            "is_translated": bool(row["is_translated"]),
        }

    # ========== 人名词典 ==========

    def get_char_dict(self) -> dict:
        """获取人名词典（按插入顺序）"""
        rows = self._conn.execute("SELECT en_name, cn_name FROM char_dict ORDER BY rowid").fetchall()
        return {row["en_name"]: row["cn_name"] for row in rows}

    def update_char_name(self, en_name: str, cn_name: str):
        """更新单个人名翻译（已存在则 UPDATE，不存在则 INSERT）"""
        existing = self._conn.execute(
            "SELECT 1 FROM char_dict WHERE en_name=?", (en_name,)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE char_dict SET cn_name=? WHERE en_name=?",
                (cn_name, en_name)
            )
        else:
            self._conn.execute(
                "INSERT INTO char_dict (en_name, cn_name) VALUES (?, ?)",
                (en_name, cn_name)
            )
        self._conn.commit()

    def update_char_dict_batch(self, name_dict: dict[str, str]):
        """批量更新人名词典（已存在则 UPDATE，不存在则 INSERT）"""
        with self._transaction():
            for en_name, cn_name in name_dict.items():
                existing = self._conn.execute(
                    "SELECT 1 FROM char_dict WHERE en_name=?", (en_name,)
                ).fetchone()
                if existing:
                    self._conn.execute(
                        "UPDATE char_dict SET cn_name=? WHERE en_name=?",
                        (cn_name, en_name)
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO char_dict (en_name, cn_name) VALUES (?, ?)",
                        (en_name, cn_name)
                    )

    def get_untranslated_names(self) -> list[tuple[str, str]]:
        """获取未翻译的人名 -> [(en_name, cn_name), ...]"""
        rows = self._conn.execute(
            "SELECT en_name, cn_name FROM char_dict WHERE cn_name='' OR cn_name IS NULL"
        ).fetchall()
        return [(row["en_name"], row["cn_name"]) for row in rows]

    def get_char_dict_count(self) -> dict:
        """统计人名词典"""
        row = self._conn.execute(
            "SELECT COUNT(*) as total, SUM(CASE WHEN cn_name != '' AND cn_name IS NOT NULL THEN 1 ELSE 0 END) as translated FROM char_dict"
        ).fetchone()
        total = row["total"] or 0
        translated = row["translated"] or 0
        return {"total": total, "translated": translated, "untranslated": total - translated}

    # ========== 角色信息 ==========

    def insert_characters(self, characters: list[dict]):
        """批量插入角色信息"""
        with self._transaction():
            self._conn.executemany(
                "INSERT OR IGNORE INTO characters (variable, name, chinese_name) VALUES (?, ?, ?)",
                [(c.get("variable", ""), c.get("name", ""), c.get("chinese_name", ""))
                 for c in characters]
            )

    def get_characters(self) -> list[dict]:
        """获取所有角色信息"""
        rows = self._conn.execute("SELECT * FROM characters ORDER BY name").fetchall()
        return [{"variable": r["variable"], "name": r["name"],
                 "chinese_name": r["chinese_name"]} for r in rows]

    def get_variable_map(self) -> dict[str, str]:
        """获取变量名 -> 显示名映射"""
        rows = self._conn.execute("SELECT variable, name FROM characters").fetchall()
        return {r["variable"]: r["name"] for r in rows}

    # ========== 角色分析档案 ==========

    def get_profile(self, name: str) -> Optional[dict]:
        """获取角色分析档案"""
        row = self._conn.execute(
            "SELECT profile_json FROM char_profiles WHERE name=?", (name,)
        ).fetchone()
        return json.loads(row["profile_json"]) if row else None

    def save_profile(self, name: str, profile: dict):
        """保存角色分析档案"""
        self._conn.execute(
            "INSERT OR REPLACE INTO char_profiles (name, profile_json) VALUES (?, ?)",
            (name, json.dumps(profile, ensure_ascii=False))
        )
        self._conn.commit()

    def get_all_profiles(self) -> dict[str, dict]:
        """获取所有角色分析档案"""
        rows = self._conn.execute("SELECT name, profile_json FROM char_profiles").fetchall()
        return {row["name"]: json.loads(row["profile_json"]) for row in rows}

    # ========== JSON 兼容（导入导出） ==========

    def to_json_dict(self) -> dict:
        """导出为 JSON 字典（兼容旧格式）"""
        meta = self.get_all_meta()
        dialogues = self._conn.execute("SELECT * FROM dialogues ORDER BY id").fetchall()
        ui_texts = self._conn.execute("SELECT * FROM ui_texts ORDER BY id").fetchall()
        char_dict = self.get_char_dict()
        characters = self.get_characters()
        profiles = self.get_all_profiles()

        # 将 profiles 合并到 char_dict 中（兼容旧格式）
        char_dict_with_extras = dict(char_dict)
        char_dict_with_extras["__profiles__"] = profiles
        char_dict_with_extras["__variable_map__"] = self.get_variable_map()

        return {
            "name": meta.get("name", ""),
            "game_dir": meta.get("game_dir", ""),
            "model_config_name": meta.get("model_config_name", ""),
            "dialogues": [self._row_to_dialogue_dict(r) for r in dialogues],
            "ui_texts": [self._row_to_ui_dict(r) for r in ui_texts],
            "characters": characters,
            "char_dict": char_dict_with_extras,
            "last_position": json.loads(meta.get("last_position", "{}")),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
        }

    @classmethod
    def from_json_dict(cls, db_path: str, data: dict) -> "ProjectDatabase":
        """从 JSON 字典导入到 SQLite"""
        db = cls(db_path)
        db.connect()

        # 元数据
        from datetime import datetime
        db.set_meta("name", data.get("name", ""))
        db.set_meta("game_dir", data.get("game_dir", ""))
        db.set_meta("model_config_name", data.get("model_config_name", ""))
        db.set_meta("created_at", data.get("created_at", datetime.now().isoformat()))
        db.set_meta("updated_at", data.get("updated_at", datetime.now().isoformat()))
        db.set_meta("last_position", json.dumps(data.get("last_position", {}), ensure_ascii=False))

        # 对话
        db.insert_dialogues(data.get("dialogues", []))

        # UI 字符串
        db.insert_ui_texts(data.get("ui_texts", []))

        # 角色信息
        db.insert_characters(data.get("characters", []))

        # 人名词典
        char_dict = data.get("char_dict", {})
        name_dict = {k: v for k, v in char_dict.items() if not k.startswith("__")}
        db.update_char_dict_batch(name_dict)

        # 角色分析档案
        profiles = char_dict.get("__profiles__", {})
        for name, profile in profiles.items():
            db.save_profile(name, profile)

        return db
