"""角色仓库：characters 表（含分析档案 profile 与变量名映射）"""

import json
import sqlite3
from typing import Optional

from .base import _auto_reconnect


class CharacterRepo:
    """characters 表（合并 characters + char_dict + char_profiles）"""

    def _find_character_id(self, key: str) -> Optional[int]:
        """按变量名或显示名查找角色 id（变量名优先，精确匹配身份）"""
        row = self._conn.execute(
            "SELECT id FROM characters WHERE variable=?", (key,)
        ).fetchone()
        if not row:
            row = self._conn.execute(
                "SELECT id FROM characters WHERE display_name=?", (key,)
            ).fetchone()
        return row["id"] if row else None

    @_auto_reconnect
    def insert_characters(self, characters: list[dict]):
        """批量插入角色（按变量名去重——角色身份是 Character() 变量；
        无变量名时按显示名去重。同名显示名不再互相吞并）"""
        with self._transaction():
            for c in characters:
                display_name = c.get("display_name", c.get("name", ""))
                variable = c.get("variable", "")
                if not display_name:
                    continue
                if variable:
                    existing = self._conn.execute(
                        "SELECT id FROM characters WHERE variable=?",
                        (variable,)
                    ).fetchone()
                else:
                    existing = self._conn.execute(
                        "SELECT id FROM characters WHERE display_name=? AND (variable='' OR variable IS NULL)",
                        (display_name,)
                    ).fetchone()
                if existing:
                    # 更新显示名（源码可能改名），译名/档案保留
                    self._conn.execute(
                        "UPDATE characters SET display_name=? WHERE id=?",
                        (display_name, existing["id"])
                    )
                else:
                    self._conn.execute(
                        """INSERT INTO characters
                           (variable, display_name, cn_name, lines_count,
                            profile_json, is_placeholder, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            variable,
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
        char_id = self._find_character_id(display_name)
        if char_id is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM characters WHERE id=?", (char_id,)
        ).fetchone()
        return self._row_to_character_dict(row) if row else None

    @_auto_reconnect
    def update_character_cn_name(self, display_name: str, cn_name: str,
                                 variable: str = None):
        """更新角色中文名（优先按变量名定位，避免同名角色互相覆盖）"""
        if variable:
            row = self._conn.execute(
                "SELECT id FROM characters WHERE variable=?", (variable,)
            ).fetchone()
            char_id = row["id"] if row else None
        else:
            char_id = self._find_character_id(display_name)
        if char_id is not None:
            self._conn.execute(
                "UPDATE characters SET cn_name=? WHERE id=?",
                (cn_name, char_id)
            )
        else:
            self._conn.execute(
                "INSERT INTO characters (variable, display_name, cn_name) VALUES (?, ?, ?)",
                (variable or "", display_name, cn_name)
            )
        self._conn.commit()

    @_auto_reconnect
    def update_character_profile(self, display_name: str, profile: dict,
                                 variable: str = None):
        """更新角色分析档案（优先按变量名定位）"""
        profile_json = json.dumps(profile, ensure_ascii=False)
        if variable:
            row = self._conn.execute(
                "SELECT id FROM characters WHERE variable=?", (variable,)
            ).fetchone()
            char_id = row["id"] if row else None
        else:
            char_id = self._find_character_id(display_name)
        if char_id is not None:
            self._conn.execute(
                "UPDATE characters SET profile_json=? WHERE id=?",
                (profile_json, char_id)
            )
        else:
            self._conn.execute(
                "INSERT INTO characters (variable, display_name, profile_json) VALUES (?, ?, ?)",
                (variable or "", display_name, profile_json)
            )
        self._conn.commit()

    @_auto_reconnect
    def update_character_lines_count(self, display_name: str, count: int):
        """更新角色台词数（调用方传入的是对话 speaker 变量名，双键查找）"""
        char_id = self._find_character_id(display_name)
        if char_id is not None:
            self._conn.execute(
                "UPDATE characters SET lines_count=? WHERE id=?",
                (count, char_id)
            )
        self._conn.commit()

    @_auto_reconnect
    def reset_character_lines_count(self):
        """版本更新重算台词数前清零（update_character_lines_count 只 set 不清零）"""
        self._conn.execute("UPDATE characters SET lines_count=0")
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
        """获取角色分析档案

        人名表/分析面板按显示名（Ria）存取，对话翻译按说话者变量名
        （ria/mc/neto）查询，两种键都要能命中。
        """
        row = self._conn.execute(
            "SELECT profile_json FROM characters WHERE display_name=? OR variable=?",
            (display_name, display_name)
        ).fetchone()
        if row and row["profile_json"]:
            return json.loads(row["profile_json"])
        return None

    @_auto_reconnect
    def save_profile(self, display_name: str, profile: dict, variable: str = None):
        """保存角色分析档案"""
        self.update_character_profile(display_name, profile, variable=variable)

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
