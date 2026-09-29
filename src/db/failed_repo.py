"""失败批次仓库：批次解析失败（句数不匹配）的暂存

批次翻译解析失败时不再中断整个任务，而是把该批条目暂存到
failed_batches 表；翻译页可据此弹窗分批重试、人工核验。
"""

import json
from datetime import datetime
from typing import Optional

from .base import _auto_reconnect


def _slim_items(items: list[dict]) -> list[dict]:
    """只保留重试/展示所需字段，避免整行入库"""
    return [{'id': it['id'],
             'original_text': it.get('original_text', ''),
             'character': it.get('character', ''),
             'reason': it.get('reason', ''),
             'rejected': it.get('rejected', '')} for it in items]


class FailedRepo:
    """failed_batches 表：暂存解析失败的批次条目"""

    @_auto_reconnect
    def count_failed_items(self, content_type: str) -> int:
        """刷新计数不回传全部原文/被拒译文，也不逐批执行清理写入。"""
        if content_type not in ('dialogue', 'ui'):
            raise ValueError('未知内容类型')
        table = 'dialogues' if content_type == 'dialogue' else 'ui_texts'
        # 固定连接顺序：只展开失败记录，再按 ID 查台词。普通 JOIN 可能
        # 先遍历全部未译台词，为每一句重复解析所有失败 JSON，阻塞数据库锁。
        row = self._conn.execute(
            f'''SELECT COUNT(DISTINCT t.id) AS cnt
                FROM failed_batches AS b
                CROSS JOIN json_each(b.items_json) AS item
                CROSS JOIN {table} AS t
                WHERE b.content_type=? AND t.is_translated=0
                  AND t.id=json_extract(item.value, '$.id')''',
            (content_type,)).fetchone()
        return row['cnt']

    @_auto_reconnect
    def add_failed_batch(self, content_type: str, items: list[dict],
                         error: str = '') -> int:
        cur = self._conn.execute(
            """INSERT INTO failed_batches (content_type, items_json, error, created_at)
               VALUES (?, ?, ?, ?)""",
            (content_type,
             json.dumps(_slim_items(items), ensure_ascii=False),
             error,
             datetime.now().isoformat(timespec='seconds'))
        )
        self._conn.commit()
        return cur.lastrowid

    def _row_to_dict(self, row) -> dict:
        return {
            'id': row['id'],
            'content_type': row['content_type'],
            'items': json.loads(row['items_json']),
            'error': row['error'],
            'created_at': row['created_at'],
        }

    @_auto_reconnect
    def list_failed_batches(self, content_type: str = None) -> list[dict]:
        if content_type:
            rows = self._conn.execute(
                "SELECT * FROM failed_batches WHERE content_type=? ORDER BY id",
                (content_type,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM failed_batches ORDER BY id"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @_auto_reconnect
    def get_failed_batch(self, batch_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM failed_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    @_auto_reconnect
    def update_failed_batch_items(self, batch_id: int, items: list[dict]):
        """重试后回写仍未译出的条目（空列表请改用 delete_failed_batch）"""
        self._conn.execute(
            "UPDATE failed_batches SET items_json=? WHERE id=?",
            (json.dumps(_slim_items(items), ensure_ascii=False), batch_id)
        )
        self._conn.commit()

    @_auto_reconnect
    def delete_failed_batch(self, batch_id: int):
        self._conn.execute("DELETE FROM failed_batches WHERE id=?", (batch_id,))
        self._conn.commit()

    @_auto_reconnect
    def clear_failed_batches(self, content_type: str):
        """清空某内容类型的全部暂存（条目保持未翻译）"""
        self._conn.execute(
            "DELETE FROM failed_batches WHERE content_type=?", (content_type,))
        self._conn.commit()

    @_auto_reconnect
    def count_failed_batches(self, content_type: str = None) -> int:
        if content_type:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM failed_batches WHERE content_type=?",
                (content_type,)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM failed_batches"
            ).fetchone()
        return row['cnt']

    @_auto_reconnect
    def filter_untranslated_items(self, content_type: str,
                                  items: list[dict]) -> list[dict]:
        """从暂存条目中筛出仍未翻译的（已译/已删的剔除）"""
        if not items:
            return []
        table = 'dialogues' if content_type == 'dialogue' else 'ui_texts'
        untranslated: set[int] = set()
        ids = [it['id'] for it in items]
        # SQLite 变量上限 999，分批查询
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ','.join('?' * len(chunk))
            rows = self._conn.execute(
                f"SELECT id FROM {table} WHERE is_translated=0 AND id IN ({marks})",
                chunk
            ).fetchall()
            untranslated.update(r['id'] for r in rows)
        return [it for it in items if it['id'] in untranslated]
