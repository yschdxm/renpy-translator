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
        status='marked' 的历史行匹配到直接跳过不返回——table 路径的
        已标记行源码未动（不写 _()），扫描每次都会重现它们，认不出
        就会当新候选重复插入、带着未决状态重新进入 AI 筛选。
        """
        rows = self._conn.execute(
            "SELECT * FROM embedded_candidates"
        ).fetchall()
        existing = {(r["rel_file"], r["line"], r["raw"]): dict(r) for r in rows}

        now = datetime.now().isoformat()
        result = []
        with self._transaction():
            for c in candidates:
                key = (c.rel_file, c.line, c.raw)
                row = existing.get(key)
                if row and row['status'] == 'marked':
                    continue  # 已标记（含 table 路径重现的）：不再返回
                if row:
                    # 模板形态演进（如插值补 !t）：raw 未变则 text 更新是
                    # 机械改写（判定不受影响），不同步会让旧模板与新导出
                    # 改写失配（运行模板带 !t、zz 旧条目不带→查不中）
                    if row.get('text') != c.text:
                        self._conn.execute(
                            "UPDATE embedded_candidates SET text=?, "
                            "updated_at=? WHERE id=?",
                            (c.text, now, row['id']))
                    result.append({
                        'id': row['id'], 'candidate': c,
                        'ai_keep': row['ai_keep'], 'ai_reason': row['ai_reason'],
                        'ai_danger': row['ai_danger'], 'status': row['status'],
                        'ai_evidence': row['ai_evidence'] or '',
                        'apply_path': row['apply_path'] or '',
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
                    new = {
                        'id': cur.lastrowid, 'candidate': c,
                        'ai_keep': -1, 'ai_reason': '', 'ai_danger': 0,
                        'status': 'pending', 'ai_evidence': '',
                        'apply_path': '',
                    }
                    result.append(new)
                    # 判重字典同步补插：同一行里同一字面量出现多次时
                    # （如 ("Back","Back")），扫描会产出两个同 key 候选，
                    # 不补插会让第二个被当新行重复插入
                    existing[key] = {
                        'id': new['id'], 'rel_file': c.rel_file,
                        'line': c.line, 'raw': c.raw,
                        'ai_keep': -1, 'ai_reason': '', 'ai_danger': 0,
                        'status': 'pending', 'ai_evidence': '',
                        'apply_path': '',
                    }
        return result

    @_auto_reconnect
    def update_embedded_ai(self, row_id: int, ai_keep, ai_reason: str,
                           ai_danger: bool = False, ai_evidence: str = '',
                           apply_path: str = '', stage: str = ''):
        """更新候选的 AI 判定（ai_keep: 1/0/-1；ai_danger: 静态分析发现非显示用途）

        ai_evidence: 判决引用的代码位置（file:line）；apply_path: table/wrap。
        stage 非空时同事务写 embedded_verdict_log（翻转历史与审计链：
        灰区复核的目标来源，COUNT(DISTINCT ai_keep)>1 即翻转过）。
        """
        keep_val = -1 if ai_keep is None else (1 if ai_keep else 0)
        now = datetime.now().isoformat()
        with self._transaction():
            self._conn.execute(
                "UPDATE embedded_candidates SET ai_keep=?, ai_reason=?, "
                "ai_danger=?, ai_evidence=?, apply_path=?, updated_at=? "
                "WHERE id=?",
                (keep_val, ai_reason or '', 1 if ai_danger else 0,
                 ai_evidence or '', apply_path or '', now, row_id)
            )
            if stage:
                self._conn.execute(
                    "INSERT INTO embedded_verdict_log "
                    "(row_id, ai_keep, ai_reason, stage, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row_id, keep_val, ai_reason or '', stage, now)
                )

    @_auto_reconnect
    def get_embedded_flip_history(self) -> list:
        """历史翻转过的行 id（verdict_log 中出现过不同 ai_keep 判定）"""
        rows = self._conn.execute(
            "SELECT row_id FROM embedded_verdict_log "
            "GROUP BY row_id HAVING COUNT(DISTINCT ai_keep) > 1"
        ).fetchall()
        return [r['row_id'] for r in rows]

    @_auto_reconnect
    def get_embedded_candidate(self, row_id: int) -> Optional[dict]:
        """按 id 取单条内嵌候选（refine 单句精判用）"""
        row = self._conn.execute(
            "SELECT * FROM embedded_candidates WHERE id=?", (row_id,)
        ).fetchone()
        return dict(row) if row else None

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
    def get_marked_embedded(self, apply_path: str = None) -> list:
        """已标记且需在导出副本做源码变换的内嵌候选

        apply_path='table' 的行没有源码变换（条目活在 tl 翻译表里），
        必须排除。apply_path 参数可进一步收窄：
        - 'wrap'：仅 _() 包裹（导出 wrap 应用）
        - 'fstring'：仅 f-string 模板化改写（导出变换）
        - None（默认）：全部非 table（healer 定位/keep-list——wrap 与
          fstring 都是导出副本的源码变换消费方）
        """
        sql = ("SELECT id, rel_file, line, col_start, raw, text, kind, hint "
               "FROM embedded_candidates WHERE status = 'marked' "
               "AND (apply_path IS NULL OR apply_path != 'table')")
        if apply_path:
            sql = ("SELECT id, rel_file, line, col_start, raw, text, kind, hint "
                   "FROM embedded_candidates WHERE status = 'marked' "
                   "AND apply_path = ?")
            rows = self._conn.execute(sql, (apply_path,)).fetchall()
        else:
            rows = self._conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    @_auto_reconnect
    def get_all_marked_embedded(self) -> list:
        """全部已标记候选（table + wrap 两条路径）

        strings 表与 _() 共用 old/new 存储，wrap 行的译文条目同样
        走 zz 表合成——regen_embedded_table 按全部 marked 行重写。
        """
        rows = self._conn.execute(
            "SELECT id, rel_file, line, col_start, raw, text, kind, hint "
            "FROM embedded_candidates WHERE status = 'marked'"
        ).fetchall()
        return [dict(r) for r in rows]

    @_auto_reconnect
    def get_table_marked_embedded(self) -> list:
        """已标记且走 strings 表路径的内嵌候选（兼容保留；
        regen 用 get_all_marked_embedded 覆盖 wrap 行）"""
        rows = self._conn.execute(
            "SELECT id, rel_file, line, col_start, raw, text, kind, hint "
            "FROM embedded_candidates WHERE status = 'marked' "
            "AND apply_path = 'table'"
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
