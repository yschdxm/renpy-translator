"""内容仓库：dialogues 与 ui_texts 两表（对话翻译 / UI 字符串翻译）"""

import sqlite3
from typing import Optional

from .base import _auto_reconnect


class ContentRepo:
    """dialogues + ui_texts 两表的读写（两表结构高度同构）"""

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
                           search: str = '',
                           sort_by: str = '',
                           sort_order: str = 'asc') -> tuple[list[dict], int]:
        """分页查询对话（可选排序：sort_by 白名单外保持默认按 id）"""
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

        # ORDER BY 只拼白名单列名，防注入；空值/非法值回落默认 id 排序
        sort_col = sort_by if sort_by in {'id', 'character', 'original_text',
                                          'is_translated'} else 'id'
        order = 'DESC' if sort_order.lower() == 'desc' else 'ASC'

        offset = page * page_size
        rows = self._conn.execute(
            f"SELECT * FROM dialogues{where_sql} ORDER BY {sort_col} {order}, id LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()

        items = [self._row_to_dialogue_dict(row) for row in rows]
        return items, total

    @_auto_reconnect
    def get_untranslated_dialogues(self, limit: int = None) -> list[dict]:
        """获取未翻译的对话（按剧情书写顺序：文件 + 行号）"""
        sql = "SELECT * FROM dialogues WHERE is_translated=0 ORDER BY file_path, line_number, id"
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

    @_auto_reconnect
    def get_all_dialogues(self) -> list[dict]:
        """全量对话（剧情书写顺序：文件 + 行号）。版本更新快照用"""
        rows = self._conn.execute(
            "SELECT * FROM dialogues ORDER BY file_path, line_number, id"
        ).fetchall()
        return [self._row_to_dialogue_dict(row) for row in rows]

    @_auto_reconnect
    def replace_dialogues(self, items: list[dict]) -> list[int]:
        """单事务清空并重建对话表，返回与 items 对齐的新 id 列表"""
        ids = []
        with self._transaction():
            self._conn.execute("DELETE FROM dialogues")
            for d in items:
                cur = self._conn.execute(
                    """INSERT INTO dialogues
                       (file_path, line_number, label, character, original_text,
                        translated_text, is_translated)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        d.get("file_path", ""),
                        d.get("line_number", 0),
                        d.get("label", ""),
                        d.get("character", ""),
                        d.get("original_text", ""),
                        d.get("translated_text", ""),
                        1 if d.get("is_translated") else 0,
                    )
                )
                ids.append(cur.lastrowid)
        return ids

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
    def insert_ui_texts_new_only(self, items: list[dict]) -> int:
        """只插入库中不存在的 UI 字符串（按 original_text 判重），返回插入条数

        与 insert_ui_texts 的区别：预过滤已有项（避免重复建行），
        且写入 context_hint 列。用于内嵌文本提取后的合并入库。
        """
        if not items:
            return 0
        existing = {r[0] for r in self._conn.execute(
            "SELECT original_text FROM ui_texts").fetchall()}
        new_items = [d for d in items
                     if d.get("original_text", "") and d["original_text"] not in existing]
        if not new_items:
            return 0
        with self._transaction():
            self._conn.executemany(
                """INSERT INTO ui_texts
                   (file_path, line_number, label, original_text, translated_text,
                    is_translated, context_hint)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [(
                    d.get("file_path", ""),
                    d.get("line_number", 0),
                    d.get("label", ""),
                    d.get("original_text", ""),
                    d.get("translated_text", ""),
                    1 if d.get("is_translated") else 0,
                    d.get("context_hint", ""),
                ) for d in new_items]
            )
        return len(new_items)

    @_auto_reconnect
    def find_dialogue_by_original(self, original_text: str) -> Optional[dict]:
        """按原文查对话行（导出校验定位报错条目）"""
        row = self._conn.execute(
            "SELECT id, original_text, translated_text FROM dialogues "
            "WHERE original_text = ? LIMIT 1", (original_text,)
        ).fetchone()
        return dict(row) if row else None

    @_auto_reconnect
    def find_ui_text_by_original(self, original_text: str) -> Optional[dict]:
        """按原文查 UI 字符串行（导出校验定位报错条目）"""
        row = self._conn.execute(
            "SELECT id, original_text, translated_text FROM ui_texts "
            "WHERE original_text = ? LIMIT 1", (original_text,)
        ).fetchone()
        return dict(row) if row else None

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
                          search: str = '',
                          sort_by: str = '',
                          sort_order: str = 'asc') -> tuple[list[dict], int]:
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

        # ORDER BY 只拼白名单列名，防注入；空值/非法值回落默认 id 排序
        sort_col = sort_by if sort_by in {'id', 'original_text',
                                          'is_translated'} else 'id'
        order = 'DESC' if sort_order.lower() == 'desc' else 'ASC'

        offset = page * page_size
        rows = self._conn.execute(
            f"SELECT * FROM ui_texts{where_sql} ORDER BY {sort_col} {order}, id LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()

        items = [self._row_to_ui_dict(row) for row in rows]
        return items, total

    @_auto_reconnect
    def get_untranslated_ui_texts(self, limit: int = None) -> list[dict]:
        """获取未翻译的 UI 字符串（按文件 + 行号顺序，同文件字符串相邻成批）"""
        sql = "SELECT * FROM ui_texts WHERE is_translated=0 ORDER BY file_path, line_number, id"
        if limit:
            sql += f" LIMIT {limit}"
        rows = self._conn.execute(sql).fetchall()
        return [self._row_to_ui_dict(row) for row in rows]

    @_auto_reconnect
    def update_ui_hints(self, hints: dict) -> int:
        """批量写回 UI 字符串的出处上下文（按原文匹配），返回命中条数"""
        matched = 0
        with self._transaction():
            for original, hint in hints.items():
                cur = self._conn.execute(
                    "UPDATE ui_texts SET context_hint=? WHERE original_text=?",
                    (hint, original)
                )
                matched += cur.rowcount
        return matched

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
            "context_hint": row["context_hint"] if "context_hint" in row.keys() else "",
        }

    @_auto_reconnect
    def get_all_ui_texts(self) -> list[dict]:
        """全量 UI 字符串（文件 + 行号序）。版本更新快照用"""
        rows = self._conn.execute(
            "SELECT * FROM ui_texts ORDER BY file_path, line_number, id"
        ).fetchall()
        return [self._row_to_ui_dict(row) for row in rows]

    @_auto_reconnect
    def replace_ui_texts(self, items: list[dict]) -> list[int]:
        """单事务清空并重建 UI 字符串表，返回与 items 对齐的新 id 列表"""
        ids = []
        with self._transaction():
            self._conn.execute("DELETE FROM ui_texts")
            for d in items:
                cur = self._conn.execute(
                    """INSERT INTO ui_texts
                       (file_path, line_number, label, original_text,
                        translated_text, is_translated, context_hint)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        d.get("file_path", ""),
                        d.get("line_number", 0),
                        d.get("label", ""),
                        d.get("original_text", ""),
                        d.get("translated_text", ""),
                        1 if d.get("is_translated") else 0,
                        d.get("context_hint", ""),
                    )
                )
                ids.append(cur.lastrowid)
        return ids

    # ========== 全量查询（绕分页的专用读取） ==========

    @_auto_reconnect
    def iter_translated_pairs(self) -> list[tuple[str, str]]:
        """已翻译条目的 (原文, 译文) 对（对话 + UI，各按 id 序）。

        导出构建翻译字典用：只取两列，替代 get_*_page(0, 999999) 绕分页
        拉全量再逐行筛 translated_text 的做法。dict() 化时同原文后者
        覆盖前者（对话在前 UI 在后，与原逐表循环语义一致）。
        """
        pairs = []
        for table in ('dialogues', 'ui_texts'):
            rows = self._conn.execute(
                f"SELECT original_text, translated_text FROM {table} "
                "WHERE is_translated=1 AND translated_text != '' ORDER BY id"
            ).fetchall()
            pairs.extend(
                (r["original_text"], r["translated_text"]) for r in rows)
        return pairs

    @_auto_reconnect
    def get_dialogues_by_character(self, variable: str) -> list[dict]:
        """某角色（speaker 变量名）的全部台词（人名翻译加载台词用，
        绕开分页拿全量）"""
        rows = self._conn.execute(
            "SELECT * FROM dialogues WHERE character=? ORDER BY id",
            (variable,)
        ).fetchall()
        return [self._row_to_dialogue_dict(row) for row in rows]

    @_auto_reconnect
    def sample_dialogue_texts(self, limit: int = 30000) -> str:
        """抽样对话文本用于风格分析（原 server 层 _sample_dialogue_text
        的自定义 SQL 下沉到本层，锁纪律由 @_auto_reconnect 保证）。

        每个 label 取前 3 句 + 随机 50 句，总量限 limit 字符。
        返回 "角色: 台词" 逐行拼接的文本；无对话返回空串。
        """
        import random
        rows = self._conn.execute(
            "SELECT label, character, original_text FROM dialogues "
            "WHERE length(original_text) > 5 ORDER BY file_path, line_number"
        ).fetchall()
        if not rows:
            return ""

        # 每个 label 前 3 句
        samples, seen_labels = [], set()
        for r in rows:
            label = r['label'] or ''
            if label not in seen_labels:
                seen_labels.add(label)
                label_rows = [x for x in rows if (x['label'] or '') == label][:3]
                samples.extend(label_rows)

        # 随机补 50 句
        pool = [r for r in rows if r not in samples]
        if pool:
            samples.extend(random.sample(pool, min(50, len(pool))))

        # 拼装，限 limit 字符
        lines, total = [], 0
        for r in samples:
            char = r['character'] or '旁白'
            line = f"{char}: {r['original_text']}"
            if total + len(line) > limit:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)
