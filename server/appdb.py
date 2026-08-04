"""应用级 SQLite（data/app.db）：settings / jobs / job_events

与项目库（projects/<name>/project.db）分离——任务可不属于任何项目（如建项目）。
任务与事件持久化：服务重启后 running/waiting_input 标为 interrupted，可断点续跑。
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'running',
    progress      REAL NOT NULL DEFAULT 0,
    stage         TEXT NOT NULL DEFAULT '',
    payload_json  TEXT NOT NULL DEFAULT '{}',
    question_json TEXT,
    result_json   TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_events (
    job_id    TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

# 每任务保留的最近事件条数
EVENT_KEEP = 2000


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


class AppDatabase:
    """线程安全（内锁）的应用级数据库；所有方法都是阻塞式，调用方自行放 executor"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    # ---- settings ----

    def get_setting(self, key: str, default: str = '') -> str:
        with self._lock:
            row = self._conn.execute(
                'SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def set_setting(self, key: str, value: str):
        with self._lock:
            self._conn.execute(
                'INSERT INTO settings(key,value) VALUES(?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, value))
            self._conn.commit()

    # ---- jobs ----

    def create_job(self, kind: str, label: str, payload: dict) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                'INSERT INTO jobs(id,kind,label,status,payload_json,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?)',
                (job_id, kind, label, 'running', json.dumps(payload, ensure_ascii=False),
                 _now(), _now()))
            self._conn.commit()
        return job_id

    def update_job(self, job_id: str, **fields):
        """可更新字段: status/progress/stage/payload/question/result/error"""
        col_map = {
            'status': 'status', 'progress': 'progress', 'stage': 'stage',
            'payload': 'payload_json', 'question': 'question_json',
            'result': 'result_json', 'error': 'error',
        }
        sets, vals = [], []
        for k, v in fields.items():
            col = col_map[k]
            sets.append(f'{col}=?')
            if col.endswith('_json') and v is not None and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False)
            vals.append(v)
        sets.append('updated_at=?')
        vals.append(_now())
        vals.append(job_id)
        with self._lock:
            self._conn.execute(
                f'UPDATE jobs SET {", ".join(sets)} WHERE id=?', vals)
            self._conn.commit()

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                'SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        return self._job_row(row) if row else None

    def list_jobs(self, active_only: bool = False, limit: int = 50) -> list:
        sql = 'SELECT * FROM jobs'
        if active_only:
            # interrupted 是终态（历史记录），不算活跃——
            # 前端恢复对话框只接管真正还在跑/等输入的任务
            sql += " WHERE status IN ('running','waiting_input')"
        sql += ' ORDER BY created_at DESC LIMIT ?'
        with self._lock:
            rows = self._conn.execute(sql, (limit,)).fetchall()
        return [self._job_row(r) for r in rows]

    def mark_interrupted(self) -> int:
        """启动时调用：所有 running/waiting_input → interrupted"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status='interrupted', updated_at=? "
                "WHERE status IN ('running','waiting_input')", (_now(),))
            self._conn.commit()
            return cur.rowcount

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        for col in ('payload_json', 'question_json', 'result_json'):
            raw = d.pop(col)
            key = col[:-5]  # 去掉 _json
            d[key] = json.loads(raw) if raw else None
        return d

    # ---- job_events ----

    def add_event(self, job_id: str, kind: str, data: dict) -> int:
        """追加事件，返回 seq；超出 EVENT_KEEP 裁掉最旧的"""
        with self._lock:
            row = self._conn.execute(
                'SELECT COALESCE(MAX(seq),0)+1 AS seq FROM job_events WHERE job_id=?',
                (job_id,)).fetchone()
            seq = row['seq']
            self._conn.execute(
                'INSERT INTO job_events(job_id,seq,kind,data_json) VALUES(?,?,?,?)',
                (job_id, seq, kind, json.dumps(data, ensure_ascii=False)))
            self._conn.execute(
                'DELETE FROM job_events WHERE job_id=? AND seq <= ?',
                (job_id, seq - EVENT_KEEP))
            self._conn.commit()
        return seq

    def get_events(self, job_id: str, after_seq: int = 0) -> list:
        with self._lock:
            rows = self._conn.execute(
                'SELECT seq,kind,data_json FROM job_events '
                'WHERE job_id=? AND seq>? ORDER BY seq',
                (job_id, after_seq)).fetchall()
        return [{'seq': r['seq'], 'kind': r['kind'],
                 'data': json.loads(r['data_json'])} for r in rows]
