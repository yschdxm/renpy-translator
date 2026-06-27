"""SQLite 数据库层

每个项目一个独立的 .db 文件，使用 WAL 模式支持读写并发。
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional
from contextlib import contextmanager
from functools import wraps


def _auto_reconnect(method):
    """装饰器：方法执行前确保数据库连接有效"""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
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
    is_translated INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ui_label ON ui_texts(label);
CREATE INDEX IF NOT EXISTS idx_ui_translated ON ui_texts(is_translated);
CREATE INDEX IF NOT EXISTS idx_ui_file ON ui_texts(file_path);

-- 角色表（合并 characters + char_dict + char_profiles）
CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variable TEXT DEFAULT '',
    display_name TEXT NOT NULL,
    cn_name TEXT DEFAULT '',
    lines_count INTEGER DEFAULT 0,
    profile_json TEXT DEFAULT '',
    is_placeholder INTEGER DEFAULT 0,
    created_at TEXT DEFAULT '',
    UNIQUE(display_name)
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
"""


class ProjectDatabase:
    """项目 SQLite 数据库"""

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
        self._conn.execute("PRAGMA cache_size=-64000")
        self._conn.executescript(_SCHEMA_SQL)
        # 迁移：给已有表补上缺失的列
        self._migrate_columns()
        self._conn.commit()

    def _migrate_columns(self):
        """给已有表补上缺失的列（兼容旧数据库）"""
        for table, col, col_def in [
            ('dialogues', 'label', "TEXT DEFAULT ''"),
            ('ui_texts', 'label', "TEXT DEFAULT ''"),
        ]:
            existing = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @contextmanager
    def _transaction(self):
        assert self._conn, "数据库未连接"
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

    # ========== 对话翻译 ==========

    @_auto_reconnect
    def insert_dialogues(self, items: list[dict]):
        """批量插入对话"""
        with self._transaction():
            self._conn.executemany(
                """INSERT INTO dialogues
                   (file_path, line_number, label, character, original_text,
                    translated_text, is_translated)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [(
                    d.get("file_path", ""),
                    d.get("line_number", 0),
                    d.get("label", ""),
                    d.get("character", ""),
                    d.get("original_text", ""),
                    d.get("translated_text", ""),
                    1 if d.get("is_translated") else 0,
                ) for d in items]
            )

    @_auto_reconnect
    def update_dialogue(self, item_id: int, translated_text: str):
        """翻译单条对话后保存"""
        self._conn.execute(
            "UPDATE dialogues SET translated_text=?, is_translated=1 WHERE id=?",
            (translated_text, item_id)
        )
        self._conn.commit()

    @_auto_reconnect
    def update_dialogues_batch(self, updates: list[tuple[int, str]]):
        """批量更新对话翻译"""
        with self._transaction():
            self._conn.executemany(
                "UPDATE dialogues SET translated_text=?, is_translated=1 WHERE id=?",
                [(text, id_) for id_, text in updates]
            )

    @_auto_reconnect
    def get_dialogues_page(self, page: int = 0, page_size: int = 50,
                           filter_mode: str = 'all',
                           character: str = '',
                           search: str = '') -> tuple[list[dict], int]:
        """分页查询对话"""
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

        count_row = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM dialogues{where_sql}", params
        ).fetchone()
        total = count_row["cnt"]

        offset = page * page_size
        rows = self._conn.execute(
            f"SELECT * FROM dialogues{where_sql} ORDER BY id LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()

        items = [self._row_to_dialogue_dict(row) for row in rows]
        return items, total

    @_auto_reconnect
    def get_untranslated_dialogues(self, limit: int = None) -> list[dict]:
        """获取未翻译的对话"""
        sql = "SELECT * FROM dialogues WHERE is_translated=0 ORDER BY id"
        if limit:
            sql += f" LIMIT {limit}"
        rows = self._conn.execute(sql).fetchall()
        return [self._row_to_dialogue_dict(row) for row in rows]

    @_auto_reconnect
    def get_dialogue(self, item_id: int) -> Optional[dict]:
        """获取单条对话"""
        row = self._conn.execute(
            "SELECT * FROM dialogues WHERE id=?", (item_id,)
        ).fetchone()
        return self._row_to_dialogue_dict(row) if row else None

    @_auto_reconnect
    def get_dialogue_context(self, item_id: int, content_type: str,
                              count: int = 5) -> tuple[list[dict], list[dict]]:
        """按 label 获取上下文（前后 N 条）

        已翻译的返回 original_text + translated_text + character
        未翻译的只返回 original_text + character
        """
        table = 'dialogues' if content_type == 'dialogue' else 'ui_texts'

        # 获取当前条目的 label 和 line_number
        current = self._conn.execute(
            f"SELECT label, file_path, line_number FROM {table} WHERE id=?",
            (item_id,)
        ).fetchone()

        if not current:
            return ([], [])

        label = current['label']
        line_number = current['line_number']

        # ui_texts 表没有 character 列
        char_col = "character" if content_type == "dialogue" else "'' as character"

        if label:
            before_rows = self._conn.execute(
                f"""SELECT original_text, translated_text, {char_col}
                    FROM {table}
                    WHERE label=? AND line_number < ?
                    ORDER BY line_number DESC LIMIT ?""",
                (label, line_number, count)
            ).fetchall()

            after_rows = self._conn.execute(
                f"""SELECT original_text, translated_text, {char_col}
                    FROM {table}
                    WHERE label=? AND line_number > ?
                    ORDER BY line_number ASC LIMIT ?""",
                (label, line_number, count)
            ).fetchall()
        else:
            before_rows = self._conn.execute(
                f"""SELECT original_text, translated_text, {char_col}
                    FROM {table}
                    WHERE id < ? ORDER BY id DESC LIMIT ?""",
                (item_id, count)
            ).fetchall()

            after_rows = self._conn.execute(
                f"""SELECT original_text, translated_text, {char_col}
                    FROM {table}
                    WHERE id > ? ORDER BY id ASC LIMIT ?""",
                (item_id, count)
            ).fetchall()

        def _rows_to_list(rows):
            return [
                {
                    'original_text': r['original_text'],
                    'translated_text': r['translated_text'] or '',
                    'character': r['character'] or '',
                }
                for r in rows
            ]

        return (_rows_to_list(list(reversed(before_rows))), _rows_to_list(after_rows))

    @_auto_reconnect
    def get_dialogue_count(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) as total, SUM(is_translated) as translated FROM dialogues"
        ).fetchone()
        total = row["total"] or 0
        translated = row["translated"] or 0
        return {"total": total, "translated": translated, "untranslated": total - translated}

    @_auto_reconnect
    def get_dialogue_characters(self) -> list[str]:
        """获取所有出现的角色（去重）"""
        rows = self._conn.execute(
            "SELECT DISTINCT character FROM dialogues WHERE character != '' ORDER BY character"
        ).fetchall()
        return [r["character"] for r in rows]

    @staticmethod
    def _row_to_dialogue_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "file_path": row["file_path"],
            "line_number": row["line_number"],
            "label": row["label"],
            "character": row["character"],
            "original_text": row["original_text"],
            "translated_text": row["translated_text"],
            "is_translated": bool(row["is_translated"]),
        }

    # ========== UI 字符串翻译 ==========

    @_auto_reconnect
    def insert_ui_texts(self, items: list[dict]):
        """批量插入 UI 字符串"""
        with self._transaction():
            self._conn.executemany(
                """INSERT INTO ui_texts
                   (file_path, line_number, label, original_text, translated_text, is_translated)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(
                    d.get("file_path", ""),
                    d.get("line_number", 0),
                    d.get("label", ""),
                    d.get("original_text", ""),
                    d.get("translated_text", ""),
                    1 if d.get("is_translated") else 0,
                ) for d in items]
            )

    @_auto_reconnect
    def update_ui_text(self, item_id: int, translated_text: str):
        self._conn.execute(
            "UPDATE ui_texts SET translated_text=?, is_translated=1 WHERE id=?",
            (translated_text, item_id)
        )
        self._conn.commit()

    @_auto_reconnect
    def update_ui_texts_batch(self, updates: list[tuple[int, str]]):
        with self._transaction():
            self._conn.executemany(
                "UPDATE ui_texts SET translated_text=?, is_translated=1 WHERE id=?",
                [(text, id_) for id_, text in updates]
            )

    @_auto_reconnect
    def get_ui_texts_page(self, page: int = 0, page_size: int = 50,
                          filter_mode: str = 'all',
                          search: str = '') -> tuple[list[dict], int]:
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

    @_auto_reconnect
    def get_untranslated_ui_texts(self, limit: int = None) -> list[dict]:
        sql = "SELECT * FROM ui_texts WHERE is_translated=0 ORDER BY id"
        if limit:
            sql += f" LIMIT {limit}"
        rows = self._conn.execute(sql).fetchall()
        return [self._row_to_ui_dict(row) for row in rows]

    @_auto_reconnect
    def get_ui_text(self, item_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM ui_texts WHERE id=?", (item_id,)
        ).fetchone()
        return self._row_to_ui_dict(row) if row else None

    @_auto_reconnect
    def get_ui_text_count(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) as total, SUM(is_translated) as translated FROM ui_texts"
        ).fetchone()
        total = row["total"] or 0
        translated = row["translated"] or 0
        return {"total": total, "translated": translated, "untranslated": total - translated}

    @staticmethod
    def _row_to_ui_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "file_path": row["file_path"],
            "line_number": row["line_number"],
            "label": row["label"],
            "original_text": row["original_text"],
            "translated_text": row["translated_text"],
            "is_translated": bool(row["is_translated"]),
        }

    # ========== 角色表（合并后） ==========

    @_auto_reconnect
    def insert_characters(self, characters: list[dict]):
        """批量插入角色（已存在则更新 variable）"""
        with self._transaction():
            for c in characters:
                display_name = c.get("display_name", c.get("name", ""))
                if not display_name:
                    continue
                existing = self._conn.execute(
                    "SELECT id FROM characters WHERE display_name=?",
                    (display_name,)
                ).fetchone()
                if existing:
                    if c.get("variable"):
                        self._conn.execute(
                            "UPDATE characters SET variable=? WHERE display_name=?",
                            (c["variable"], display_name)
                        )
                else:
                    self._conn.execute(
                        """INSERT INTO characters
                           (variable, display_name, cn_name, lines_count,
                            profile_json, is_placeholder, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            c.get("variable", ""),
                            display_name,
                            c.get("cn_name", ""),
                            c.get("lines_count", 0),
                            c.get("profile_json", ""),
                            1 if c.get("is_placeholder") else 0,
                            c.get("created_at", ""),
                        )
                    )

    @_auto_reconnect
    def get_characters(self) -> list[dict]:
        """获取所有角色"""
        rows = self._conn.execute("SELECT * FROM characters ORDER BY id").fetchall()
        return [self._row_to_character_dict(r) for r in rows]

    @_auto_reconnect
    def get_character_by_name(self, display_name: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM characters WHERE display_name=?", (display_name,)
        ).fetchone()
        return self._row_to_character_dict(row) if row else None

    @_auto_reconnect
    def update_character_cn_name(self, display_name: str, cn_name: str):
        """更新角色中文名"""
        existing = self._conn.execute(
            "SELECT id FROM characters WHERE display_name=?", (display_name,)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE characters SET cn_name=? WHERE display_name=?",
                (cn_name, display_name)
            )
        else:
            self._conn.execute(
                "INSERT INTO characters (display_name, cn_name) VALUES (?, ?)",
                (display_name, cn_name)
            )
        self._conn.commit()

    @_auto_reconnect
    def update_character_profile(self, display_name: str, profile: dict):
        """更新角色分析档案"""
        profile_json = json.dumps(profile, ensure_ascii=False)
        existing = self._conn.execute(
            "SELECT id FROM characters WHERE display_name=?", (display_name,)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE characters SET profile_json=? WHERE display_name=?",
                (profile_json, display_name)
            )
        else:
            self._conn.execute(
                "INSERT INTO characters (display_name, profile_json) VALUES (?, ?)",
                (display_name, profile_json)
            )
        self._conn.commit()

    @_auto_reconnect
    def update_character_lines_count(self, display_name: str, count: int):
        """更新角色台词数"""
        self._conn.execute(
            "UPDATE characters SET lines_count=? WHERE display_name=?",
            (count, display_name)
        )
        self._conn.commit()

    @_auto_reconnect
    def get_characters_for_prompt(self) -> str:
        """获取人名翻译词典文本（用于提示词，供 AI 参考）"""
        rows = self._conn.execute(
            "SELECT display_name, cn_name FROM characters "
            "WHERE cn_name != '' AND cn_name IS NOT NULL AND is_placeholder=0"
        ).fetchall()
        if not rows:
            return ""
        lines = ["人名对照表（翻译时请使用以下中文名，保持一致性）："]
        for r in rows:
            lines.append(f"  {r['display_name']} → {r['cn_name']}")
        return "\n".join(lines)

    @_auto_reconnect
    def get_variable_map(self) -> dict[str, str]:
        """获取变量名 -> 显示名映射"""
        rows = self._conn.execute(
            "SELECT variable, display_name FROM characters WHERE variable != ''"
        ).fetchall()
        return {r["variable"]: r["display_name"] for r in rows}

    @_auto_reconnect
    def get_untranslated_characters(self) -> list[dict]:
        """获取未翻译的角色"""
        rows = self._conn.execute(
            "SELECT * FROM characters WHERE (cn_name='' OR cn_name IS NULL) AND is_placeholder=0"
        ).fetchall()
        return [self._row_to_character_dict(r) for r in rows]

    @_auto_reconnect
    def get_char_dict_count(self) -> dict:
        """统计角色翻译"""
        row = self._conn.execute(
            """SELECT COUNT(*) as total,
               SUM(CASE WHEN cn_name != '' AND cn_name IS NOT NULL THEN 1 ELSE 0 END) as translated
               FROM characters WHERE is_placeholder=0"""
        ).fetchone()
        total = row["total"] or 0
        translated = row["translated"] or 0
        return {"total": total, "translated": translated, "untranslated": total - translated}

    @_auto_reconnect
    def get_profile(self, display_name: str) -> Optional[dict]:
        """获取角色分析档案"""
        row = self._conn.execute(
            "SELECT profile_json FROM characters WHERE display_name=?",
            (display_name,)
        ).fetchone()
        if row and row["profile_json"]:
            return json.loads(row["profile_json"])
        return None

    @_auto_reconnect
    def save_profile(self, display_name: str, profile: dict):
        """保存角色分析档案"""
        self.update_character_profile(display_name, profile)

    @_auto_reconnect
    def get_all_profiles(self) -> dict[str, dict]:
        """获取所有角色分析档案"""
        rows = self._conn.execute(
            "SELECT display_name, profile_json FROM characters WHERE profile_json != ''"
        ).fetchall()
        result = {}
        for r in rows:
            try:
                result[r["display_name"]] = json.loads(r["profile_json"])
            except json.JSONDecodeError:
                pass
        return result

    @staticmethod
    def _row_to_character_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "variable": row["variable"],
            "display_name": row["display_name"],
            "cn_name": row["cn_name"],
            "lines_count": row["lines_count"],
            "profile_json": row["profile_json"],
            "is_placeholder": bool(row["is_placeholder"]),
            "created_at": row["created_at"],
        }

    # ========== 术语表 ==========

    @_auto_reconnect
    def get_glossary(self, term_type: str = None) -> dict[str, str]:
        """获取术语表"""
        if term_type:
            rows = self._conn.execute(
                "SELECT en_term, cn_term FROM glossary WHERE term_type=?",
                (term_type,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT en_term, cn_term FROM glossary"
            ).fetchall()
        return {r["en_term"]: r["cn_term"] for r in rows}

    @_auto_reconnect
    def add_glossary_term(self, en: str, cn: str, term_type: str = 'other',
                           source: str = 'manual'):
        """添加术语"""
        from datetime import datetime
        self._conn.execute(
            """INSERT OR REPLACE INTO glossary
               (en_term, cn_term, term_type, source, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (en, cn, term_type, source, datetime.now().isoformat())
        )
        self._conn.commit()

    @_auto_reconnect
    def add_glossary_batch(self, terms: list[dict]):
        """批量添加术语（去重，不覆盖已有）"""
        from datetime import datetime
        now = datetime.now().isoformat()
        with self._transaction():
            for t in terms:
                en = t.get("en_term", "").strip()
                cn = t.get("cn_term", "").strip()
                if not en or not cn:
                    continue
                # 大小写不敏感去重
                existing = self._conn.execute(
                    "SELECT en_term, cn_term FROM glossary WHERE LOWER(en_term)=LOWER(?)",
                    (en,)
                ).fetchone()
                if existing:
                    continue
                self._conn.execute(
                    """INSERT OR REPLACE INTO glossary
                       (en_term, cn_term, term_type, source, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (en, cn, t.get("term_type", "other"),
                     t.get("source", "auto"), now)
                )

    # 常见游戏 UI 标准翻译（静态参考，不存入数据库）
    UI_GLOSSARY = {
        # 菜单
        "Start Game": "开始游戏", "New Game": "新游戏", "Load Game": "读取游戏",
        "Save Game": "保存游戏", "Main Menu": "主菜单", "Options": "选项",
        "Settings": "设置", "Preferences": "偏好设置", "Quit": "退出",
        "Exit": "退出", "About": "关于", "Help": "帮助",
        # 存档
        "Save": "保存", "Load": "读取", "Delete": "删除",
        "Auto Save": "自动保存", "Quick Save": "快速保存", "Quick Load": "快速读取",
        "Save Slot": "存档位", "Save Page": "存档页", "Load Page": "读取页",
        "No saves found.": "未找到存档。", "Save your game?": "保存游戏？",
        "Load your game?": "读取游戏？",
        # 通用按钮
        "OK": "确定", "Yes": "是", "No": "否", "Cancel": "取消",
        "Back": "返回", "Next": "下一页", "Previous": "上一页",
        "Close": "关闭", "Confirm": "确认", "Apply": "应用",
        "Reset": "重置", "Default": "默认",
        # 显示设置
        "Display": "显示", "Window": "窗口", "Fullscreen": "全屏",
        "Resolution": "分辨率", "Text Speed": "文字速度",
        "Auto-Forward Time": "自动前进时间", "Skip": "跳过",
        "Unseen Text": "未读文本", "After Choices": "选项后",
        "Transitions": "转场效果",
        # 音量
        "Music Volume": "音乐音量", "Sound Volume": "音效音量",
        "Voice Volume": "语音音量", "Mute All": "全部静音",
        # 对话
        "History": "历史", "Auto": "自动", "Quick": "快速",
        "Click to continue": "点击继续", "Click to dismiss": "点击关闭",
        # 辅助功能
        "Self-voicing": "自动朗读", "Self-voicing disabled": "自动朗读已禁用",
        "Self-voicing enabled": "自动朗读已启用",
        # 其他
        "Are you sure?": "确定吗？", "Loading...": "加载中...",
        "Please wait": "请稍候", "Error": "错误", "Warning": "警告",
        "Language": "语言", "Rollback Side": "回滚方向",
        "Disable": "禁用", "Enable": "启用",
        "Left": "左", "Right": "右",
    }

    @_auto_reconnect
    def get_glossary_for_prompt(self) -> str:
        """获取术语表文本（用于提示词，供 AI 参考）"""
        # 数据库中的术语（用户手动添加/自动提取）
        db_rows = self._conn.execute(
            "SELECT en_term, cn_term, term_type FROM glossary WHERE cn_term != '' AND cn_term IS NOT NULL"
        ).fetchall()

        lines = ["已有术语表（以下术语已有翻译，请直接使用，不要重复提取）："]

        # 静态 UI 术语
        lines.append("")
        lines.append("【UI/菜单文字】")
        for en, cn in self.UI_GLOSSARY.items():
            lines.append(f"  {en} → {cn}")

        # 数据库中的游戏术语
        game_terms = [r for r in db_rows if r["term_type"] != "ui"]
        if game_terms:
            lines.append("")
            lines.append("【游戏术语】")
            for r in game_terms:
                lines.append(f"  {r['en_term']} → {r['cn_term']}")

        return "\n".join(lines)

    # ========== JSON 导出（兼容） ==========

    @_auto_reconnect
    def to_json_dict(self) -> dict:
        """导出为 JSON 字典"""
        meta = self.get_all_meta()
        dialogues = self._conn.execute("SELECT * FROM dialogues ORDER BY id").fetchall()
        ui_texts = self._conn.execute("SELECT * FROM ui_texts ORDER BY id").fetchall()
        characters = self.get_characters()
        glossary_rows = self._conn.execute("SELECT * FROM glossary").fetchall()

        char_dict = {}
        for c in characters:
            if c["cn_name"]:
                char_dict[c["display_name"]] = c["cn_name"]

        return {
            "name": meta.get("name", ""),
            "game_dir": meta.get("game_dir", ""),
            "model_config_name": meta.get("model_config_name", ""),
            "dialogues": [self._row_to_dialogue_dict(r) for r in dialogues],
            "ui_texts": [self._row_to_ui_dict(r) for r in ui_texts],
            "characters": characters,
            "char_dict": char_dict,
            "glossary": [
                {"en_term": r["en_term"], "cn_term": r["cn_term"],
                 "term_type": r["term_type"], "source": r["source"]}
                for r in glossary_rows
            ],
            "last_position": json.loads(meta.get("last_position", "{}")),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
        }
