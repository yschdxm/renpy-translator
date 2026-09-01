"""内嵌候选合并入库的判重幂等测试

背景：同一行里同一字面量出现多次时（如 ("Back","Back")、dict 键与
首元素同名），扫描会产出两个 (rel_file, line, raw) 相同的候选——
merge 必须只插一行，否则每轮任务重复积累且新行未判就被连坐标记。
"""
import sqlite3
from types import SimpleNamespace

from embedded_strings import Candidate
from database import ProjectDatabase


def _make_db(tmp_path):
    db = ProjectDatabase(str(tmp_path / 't.db'))
    return db


def _cand(rel='a.rpy', line=1, col=0, raw='"Back"'):
    return Candidate(
        file='game/' + rel, rel_file=rel, line=line,
        col_start=col, col_end=col + len(raw), raw=raw, text=raw[1:-1],
        kind='python', hint='', confidence='high')


def test_same_key_twice_inserts_once(tmp_path):
    """同一轮 merge 里同 key 候选出现两次只插一行"""
    db = _make_db(tmp_path)
    rows = db.merge_embedded_candidates(
        [_cand(col=22), _cand(col=40)])
    assert len(rows) == 2          # 两个候选都返回（各自带位置）
    conn = sqlite3.connect(str(tmp_path / 't.db'))
    n = conn.execute(
        "SELECT COUNT(*) FROM embedded_candidates "
        "WHERE rel_file='a.rpy' AND line=1 AND raw='\"Back\"'").fetchone()[0]
    assert n == 1                  # 但库里只有一行
    # 第二个候选复用第一行的 id（同 key 同一实体）
    assert rows[0]['id'] == rows[1]['id']


def test_second_round_no_duplicate(tmp_path):
    """第二轮 merge（候选未处理仍在源码）不产生重复行"""
    db = _make_db(tmp_path)
    db.merge_embedded_candidates([_cand()])
    db.merge_embedded_candidates([_cand()])
    conn = sqlite3.connect(str(tmp_path / 't.db'))
    n = conn.execute("SELECT COUNT(*) FROM embedded_candidates").fetchone()[0]
    assert n == 1


def test_marked_row_not_resurrected(tmp_path):
    """table 路径已标记的行源码未动（不写 _()），扫描每次都会重现——
    merge 必须认出并跳过，不当新候选重复插入、不带未决状态回到筛选"""
    db = _make_db(tmp_path)
    rid = db.merge_embedded_candidates([_cand()])[0]['id']
    db.update_embedded_ai(rid, 1, 'x', apply_path='table')
    db.set_embedded_status([rid], 'marked')
    rows2 = db.merge_embedded_candidates([_cand()])
    assert rows2 == []
    conn = sqlite3.connect(str(tmp_path / 't.db'))
    n = conn.execute("SELECT COUNT(*) FROM embedded_candidates").fetchone()[0]
    assert n == 1


# ---- P3：迁移、判定历史、apply_path 分流 ----

def test_migration_adds_new_columns(tmp_path):
    """旧库幂等迁移：ai_evidence/apply_path 列与 verdict_log 表自动补齐"""
    path = tmp_path / 'old.db'
    conn = sqlite3.connect(str(path))
    # 旧版 schema（无 ai_evidence/apply_path）
    conn.execute("""CREATE TABLE embedded_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rel_file TEXT DEFAULT '', line INTEGER DEFAULT 0,
        col_start INTEGER DEFAULT 0, raw TEXT DEFAULT '',
        text TEXT DEFAULT '', kind TEXT DEFAULT '', hint TEXT DEFAULT '',
        confidence TEXT DEFAULT '', ai_keep INTEGER DEFAULT -1,
        ai_reason TEXT DEFAULT '', ai_danger INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending', updated_at TEXT DEFAULT '')""")
    conn.commit()
    conn.close()

    db = ProjectDatabase(str(path))
    db.merge_embedded_candidates([_cand()])  # 触发连接与迁移
    cols = {r[1] for r in db._conn.execute(
        "PRAGMA table_info(embedded_candidates)")}
    assert 'ai_evidence' in cols and 'apply_path' in cols
    n = db._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE name='embedded_verdict_log'").fetchone()[0]
    assert n == 1
    db.close()


def test_verdict_log_and_flip_history(tmp_path):
    """stage 非空同事务写 verdict_log；出现过不同判定即翻转历史"""
    db = _make_db(tmp_path)
    rows = db.merge_embedded_candidates([_cand()])
    rid = rows[0]['id']
    db.update_embedded_ai(rid, 1, '界面文本', False, 'a.rpy:1', 'table',
                          'coarse')
    db.update_embedded_ai(rid, 0, '复核翻转', False, 'a.rpy:2', '', 'recheck')

    # 行字段同步更新
    rec = db.get_embedded_candidate(rid)
    assert rec['ai_evidence'] == 'a.rpy:2'
    assert rec['apply_path'] == ''

    db.update_embedded_ai(rid, 0, '无 stage 不写 log')  # 静默更新
    logs = db._conn.execute(
        "SELECT * FROM embedded_verdict_log WHERE row_id=? ORDER BY id",
        (rid,)).fetchall()
    assert len(logs) == 2
    assert [l['stage'] for l in logs] == ['coarse', 'recheck']
    assert db.get_embedded_flip_history() == [rid]
    db.close()


def test_no_flip_not_in_history(tmp_path):
    """同向判定重复写入不算翻转"""
    db = _make_db(tmp_path)
    rid = db.merge_embedded_candidates([_cand()])[0]['id']
    db.update_embedded_ai(rid, 1, 'a', stage='coarse')
    db.update_embedded_ai(rid, 1, 'b', stage='recheck')
    assert db.get_embedded_flip_history() == []
    db.close()


def test_marked_excludes_table_path(tmp_path):
    """get_marked_embedded 只返回 _() 包裹路径的行（table 行无源码标记可寻）"""
    db = _make_db(tmp_path)
    rows = db.merge_embedded_candidates(
        [_cand(col=22), _cand(col=40, raw='"Other"')])
    wrap_id, table_id = rows[0]['id'], rows[1]['id']
    db.update_embedded_ai(wrap_id, 1, 'x', apply_path='wrap')
    db.update_embedded_ai(table_id, 1, 'y', apply_path='table')
    db.set_embedded_status([wrap_id, table_id], 'marked')
    assert [m['id'] for m in db.get_marked_embedded()] == [wrap_id]
    assert [m['id'] for m in db.get_table_marked_embedded()] == [table_id]
    db.close()


# ---- 灰区复核目标 ----

def test_grayzone_rows():
    from services.embedded_pipeline import _grayzone_rows

    def _row(rid, keep, danger=0, text='x'):
        return {'id': rid, 'ai_keep': keep, 'ai_danger': danger,
                'candidate': SimpleNamespace(text=text)}

    rows = [
        _row(1, 1, danger=1),      # keep×danger → 灰区（危险方向必须核实）
        _row(2, 1),                # keep 无危险 → 否
        _row(3, 0, text='This is a fairly long natural sentence'),
        _row(4, 0, text='OK'),     # drop 短文本 → 否
        _row(5, 0),                # drop 普通 → 仅因翻转史入选
        _row(6, -1),               # 未决 → 否
    ]
    targets = _grayzone_rows(rows, {5})
    assert [r['id'] for r in targets] == [1, 3, 5]
