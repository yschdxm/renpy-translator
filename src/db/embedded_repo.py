"""内嵌文本仓库：embedded_candidates 表（AI 判断持久化）"""

from datetime import datetime
from typing import Optional

from .base import _auto_reconnect


class EmbeddedRepo:
    """embedded_candidates 表"""

    @staticmethod
    def _embedded_key(d: dict) -> tuple:
        return (d.get("rel_file", ""), d.get("line", 0), d.get("raw", ""))

    @_auto_reconnect
    def merge_embedded_candidates(self, candidates: list) -> list:
        """把新扫描到的候选合并入库（按 rel_file+line+raw 判重），
        返回合并后的完整待评审列表（含已有 AI 判定与状态）。

        已存在项保留 ai_keep/ai_reason/status 与 id；新项以 pending 插入。
        status='marked' 的历史项不再返回（源码已标记，不会重现）。
        """
        rows = self._conn.execute(
            "SELECT * FROM embedded_candidates WHERE status != 'marked'"
        ).fetchall()
        existing = {(r["rel_file"], r["line"], r["raw"]): r for r in rows}

        now = datetime.now().isoformat()
        result = []
        with self._transaction():
            for c in candidates:
                key = (c.rel_file, c.line, c.raw)
                row = existing.get(key)
                if row:
                    result.append({
                        'id': row['id'], 'candidate': c,
                        'ai_keep': row['ai_keep'], 'ai_reason': row['ai_reason'],
                        'ai_danger': row['ai_danger'], 'status': row['status'],
                    })
                else:
                    cur = self._conn.execute(
                        """INSERT INTO embedded_candidates
                           (rel_file, line, col_start, raw, text, kind, hint,
                            confidence, ai_keep, ai_reason, status, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, -1, '', 'pending', ?)""",
                        (c.rel_file, c.line, c.col_start, c.raw, c.text,
                         c.kind, c.hint, c.confidence, now)
                    )
                    result.append({
                        'id': cur.lastrowid, 'candidate': c,
                        'ai_keep': -1, 'ai_reason': '', 'ai_danger': 0,
                        'status': 'pending',
                    })
        return result

    @_auto_reconnect
    def update_embedded_ai(self, row_id: int, ai_keep, ai_reason: str,
                           ai_danger: bool = False):
        """更新候选的 AI 判定（ai_keep: 1/0/-1；ai_danger: 静态分析发现非显示用途）"""
        keep_val = -1 if ai_keep is None else (1 if ai_keep else 0)
        self._conn.execute(
            "UPDATE embedded_candidates SET ai_keep=?, ai_reason=?, ai_danger=?, "
            "updated_at=? WHERE id=?",
            (keep_val, ai_reason or '', 1 if ai_danger else 0,
             datetime.now().isoformat(), row_id)
        )
        self._conn.commit()

    @_auto_reconnect
    def get_embedded_candidate(self, row_id: int) -> Optional[dict]:
        """按 id 取单条内嵌候选（refine 单句精判用）"""
        row = self._conn.execute(
            "SELECT * FROM embedded_candidates WHERE id=?", (row_id,)
        ).fetchone()
        return dict(row) if row else None

    @_auto_reconnect
    def reset_embedded_ai(self, row_ids: list = None):
        """清空 AI 判定（全部重判前调用；row_ids=None 时清空全部非 marked）"""
        now = datetime.now().isoformat()
        if row_ids:
            self._conn.executemany(
                "UPDATE embedded_candidates SET ai_keep=-1, ai_reason='', ai_danger=0, "
                "updated_at=? WHERE id=?",
                [(now, rid) for rid in row_ids]
            )
        else:
            self._conn.execute(
                "UPDATE embedded_candidates SET ai_keep=-1, ai_reason='', ai_danger=0, "
                "updated_at=? WHERE status != 'marked'", (now,)
            )
        self._conn.commit()

    @_auto_reconnect
    def set_embedded_status(self, row_ids: list, status: str):
        """批量设置候选状态（pending/skipped/marked）"""
        if not row_ids:
            return
        now = datetime.now().isoformat()
        with self._transaction():
            self._conn.executemany(
                "UPDATE embedded_candidates SET status=?, updated_at=? WHERE id=?",
                [(status, now, rid) for rid in row_ids]
            )

    @_auto_reconnect
    def delete_embedded_stale(self, keep_ids: list):
        """清理已不存在于最新扫描的 pending/skipped 候选（源码已变化）"""
        if keep_ids:
            placeholders = ','.join('?' * len(keep_ids))
            self._conn.execute(
                f"DELETE FROM embedded_candidates WHERE status != 'marked' "
                f"AND id NOT IN ({placeholders})", keep_ids
            )
        self._conn.commit()

    @_auto_reconnect
    def get_marked_embedded(self) -> list:
        """全部已标记的内嵌候选（导出校验失败时定位需拆除的包裹）"""
        rows = self._conn.execute(
            "SELECT id, rel_file, line, col_start, raw, text, kind, hint "
            "FROM embedded_candidates WHERE status = 'marked'"
        ).fetchall()
        return [dict(r) for r in rows]

    @_auto_reconnect
    def update_embedded_position(self, row_id: int, line: int, col_start: int):
        """版本更新重定位后写回新坐标（保持 unwrap/导出校验定位有效）"""
        self._conn.execute(
            "UPDATE embedded_candidates SET line=?, col_start=?, updated_at=? "
            "WHERE id=?",
            (line, col_start, datetime.now().isoformat(), row_id)
        )
        self._conn.commit()
