"""版本更新仓库：obsolete_translations / update_review 表 + JSON 导出"""

import json
from datetime import datetime

from .base import _auto_reconnect


class UpdateRepo:
    """版本更新相关表（失效译文 / 模糊复核）与 JSON 导出"""

    @_auto_reconnect
    def save_obsolete(self, rows: list[dict]):
        """全量重建失效译文表（每轮版本更新重写）"""
        now = datetime.now().isoformat()
        with self._transaction():
            self._conn.execute("DELETE FROM obsolete_translations")
            self._conn.executemany(
                """INSERT INTO obsolete_translations
                   (kind, file_path, character, original_text, translated_text, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(
                    r.get("kind", ""),
                    r.get("file_path", ""),
                    r.get("character", ""),
                    r.get("original_text", ""),
                    r.get("translated_text", ""),
                    now,
                ) for r in rows]
            )

    @_auto_reconnect
    def get_obsolete(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM obsolete_translations ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    @_auto_reconnect
    def save_update_review(self, rows: list[dict]):
        """全量重建模糊复核表（每轮版本更新重写）"""
        with self._transaction():
            self._conn.execute("DELETE FROM update_review")
            self._conn.executemany(
                """INSERT INTO update_review
                   (target_kind, target_id, new_original, old_original,
                    old_translation, ratio, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [(
                    r.get("target_kind", ""),
                    r.get("target_id", 0),
                    r.get("new_original", ""),
                    r.get("old_original", ""),
                    r.get("old_translation", ""),
                    r.get("ratio", 0),
                    r.get("status", "pending"),
                ) for r in rows]
            )

    @_auto_reconnect
    def get_update_review(self, status: str = None) -> list[dict]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM update_review WHERE status=? ORDER BY ratio DESC, id",
                (status,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM update_review ORDER BY ratio DESC, id"
            ).fetchall()
        return [dict(r) for r in rows]

    @_auto_reconnect
    def set_review_status(self, row_id: int, status: str):
        self._conn.execute(
            "UPDATE update_review SET status=? WHERE id=?", (status, row_id)
        )
        self._conn.commit()

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
