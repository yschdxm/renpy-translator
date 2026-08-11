"""术语仓库：glossary 表（静态 UI 参考译表见 prompt_data.UI_GLOSSARY）"""

from .base import _auto_reconnect
from prompt_data import UI_GLOSSARY


class GlossaryRepo:
    """glossary 表"""

    # 常见游戏 UI 标准翻译（静态参考，不存入数据库）
    # 数据本体在 prompt_data.py（prompt 参考数据，不属于 DB 层），
    # 此处保留类属性别名以维持 ProjectDatabase.UI_GLOSSARY 公共面不变
    UI_GLOSSARY = UI_GLOSSARY

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

    # 排序列白名单：SQL ORDER BY 只拼接白名单内的列名，防注入
    _GLOSSARY_SORT_COLUMNS = {'en_term', 'cn_term', 'created_at', 'source'}

    @_auto_reconnect
    def get_glossary_page(self, page: int = 0, size: int = 50,
                          search: str = '', source: str = '',
                          sort_by: str = 'en_term',
                          sort_order: str = 'asc') -> tuple[list[dict], int]:
        """分页查询术语表（搜索/来源筛选/排序），返回 (行列表, 总数)"""
        where_clauses = []
        params = []

        if search:
            # LIKE 大小写不敏感（SQLite 对 ASCII 默认如此，显式 LOWER 保证一致）
            where_clauses.append(
                "(LOWER(en_term) LIKE LOWER(?) OR LOWER(cn_term) LIKE LOWER(?))")
            params.extend([f"%{search}%", f"%{search}%"])

        if source:
            where_clauses.append("source=?")
            params.append(source)

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        count_row = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM glossary{where_sql}", params
        ).fetchone()
        total = count_row["cnt"]

        # sort_by 不在白名单回落 en_term；sort_order 仅 asc/desc
        sort_col = sort_by if sort_by in self._GLOSSARY_SORT_COLUMNS else 'en_term'
        order = 'DESC' if sort_order.lower() == 'desc' else 'ASC'

        offset = page * size
        rows = self._conn.execute(
            f"SELECT en_term, cn_term, term_type, source, created_at "
            f"FROM glossary{where_sql} ORDER BY {sort_col} {order} LIMIT ? OFFSET ?",
            params + [size, offset]
        ).fetchall()
        return [dict(r) for r in rows], total

    @_auto_reconnect
    def get_glossary_term(self, en_term: str) -> dict | None:
        """按主键取单条术语（编辑时用于保留原 source）"""
        row = self._conn.execute(
            "SELECT en_term, cn_term, term_type, source, created_at "
            "FROM glossary WHERE en_term=?",
            (en_term,)
        ).fetchone()
        return dict(row) if row else None

    @_auto_reconnect
    def delete_glossary_term(self, en_term: str):
        """删除术语"""
        self._conn.execute(
            "DELETE FROM glossary WHERE en_term=?", (en_term,))
        self._conn.commit()

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
