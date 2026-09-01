"""内嵌候选合并入库的判重幂等测试

背景：同一行里同一字面量出现多次时（如 ("Back","Back")、dict 键与
首元素同名），扫描会产出两个 (rel_file, line, raw) 相同的候选——
merge 必须只插一行，否则每轮任务重复积累且新行未判就被连坐标记。
"""
import sqlite3

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
