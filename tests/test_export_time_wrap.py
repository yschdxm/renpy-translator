# -*- coding: utf-8 -*-
"""导出时 wrap 应用（_apply_marked_wraps）与位置重定位测试

源码只读化核心：wrap 包裹只在导出副本上发生，标记/更新都不写源码。
三种路径：位置命中 / 行号漂移同文件重定位（回写库坐标）/ 找不到跳过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from database import ProjectDatabase
from embedded_strings import Candidate, relocate_wrap_candidates
from services.game_export import GameExporter


def _mk_export_tree(tmp_path, with_subdir=True):
    """导出副本布局：out/game/game/scripts/x.rpy"""
    out = tmp_path / 'out'
    sub = out / 'game' / 'game' / 'scripts' if with_subdir else out / 'game'
    sub.mkdir(parents=True)
    return out, sub


def _mk_row(tmp_path, sub, raw='"Format frag"', text='Format frag',
            line=2, col=20, rel='scripts/x.rpy'):
    (sub / 'x.rpy').write_text(
        'label start:\n    $ msg = "pre" + ' + raw + ' + "post"\n',
        encoding='utf-8')
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    c = Candidate(file=str(sub / 'x.rpy'), rel_file=rel, line=line,
                  col_start=col, col_end=col + len(raw), raw=raw,
                  text=text, kind='python', hint='拼接片段',
                  confidence='low')
    rid = db.merge_embedded_candidates([c])[0]['id']
    db.update_embedded_ai(rid, 1, '拼接片段', False, 'x.rpy:2', 'wrap',
                          'refine')
    db.set_embedded_status([rid], 'marked')
    return db, rid


def _exporter(tmp_path, db):
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    ex.db = db
    return ex


# ---- relocate_wrap_candidates ----

def test_relocate_position_hit(tmp_path):
    out, sub = _mk_export_tree(tmp_path)
    db, _rid = _mk_row(tmp_path, sub)
    rows = db.get_marked_embedded()
    cands, moved, lost = relocate_wrap_candidates(rows, str(out / "game" / "game"))
    assert len(cands) == 1
    assert cands[0].line == 2
    assert moved == [] and lost == []
    db.close()


def test_relocate_line_drift(tmp_path):
    """行号漂移：同文件内容重定位，报告 moved（调用方回写坐标）"""
    out, sub = _mk_export_tree(tmp_path)
    db, rid = _mk_row(tmp_path, sub)
    db.update_embedded_position(rid, 5, 3)  # 记录坐标漂移
    rows = db.get_marked_embedded()
    cands, moved, lost = relocate_wrap_candidates(rows, str(out / "game" / "game"))
    assert len(cands) == 1
    assert cands[0].line == 2          # 实际位置
    assert moved == [(rid, 2, 20)]     # 需要回写
    assert lost == []
    db.close()


def test_relocate_missing_literal(tmp_path):
    """字面量在文件中不存在：丢弃并报告"""
    out, sub = _mk_export_tree(tmp_path)
    db, _rid = _mk_row(tmp_path, sub, raw='"Format frag"')
    (sub / 'x.rpy').write_text('label start:\n    pass\n', encoding='utf-8')
    rows = db.get_marked_embedded()
    cands, moved, lost = relocate_wrap_candidates(rows, str(out / "game" / "game"))
    assert cands == [] and moved == [] and lost != []
    db.close()


def test_relocate_file_missing(tmp_path):
    out, sub = _mk_export_tree(tmp_path)
    db, _rid = _mk_row(tmp_path, sub)
    (sub / 'x.rpy').unlink()
    rows = db.get_marked_embedded()
    cands, moved, lost = relocate_wrap_candidates(rows, str(out / "game" / "game"))
    assert cands == [] and lost != []
    db.close()


# ---- GameExporter._apply_marked_wraps ----

def test_export_apply_wraps(tmp_path):
    """命中路径：导出副本出现 _()，库坐标不动，工作区源（此处即副本
    源）其余内容不变"""
    out, sub = _mk_export_tree(tmp_path)
    db, rid = _mk_row(tmp_path, sub)
    logs = []
    _exporter(tmp_path, db)._apply_marked_wraps(out, logs.append)
    text = (sub / 'x.rpy').read_text(encoding='utf-8')
    assert '_("Format frag")' in text
    row = db.get_embedded_candidate(rid)
    assert (row['line'], row['col_start']) == (2, 20)  # 未漂移不回写
    assert any('已在导出副本应用' in l for l in logs)
    db.close()


def test_export_apply_wraps_relocates_and_writes_back(tmp_path):
    out, sub = _mk_export_tree(tmp_path)
    db, rid = _mk_row(tmp_path, sub)
    db.update_embedded_position(rid, 9, 0)  # 漂移
    logs = []
    _exporter(tmp_path, db)._apply_marked_wraps(out, logs.append)
    text = (sub / 'x.rpy').read_text(encoding='utf-8')
    assert '_("Format frag")' in text
    row = db.get_embedded_candidate(rid)
    assert (row['line'], row['col_start']) == (2, 20)  # 已回写
    assert any('重定位' in l for l in logs)
    db.close()


def test_export_apply_wraps_missing_warns_not_blocks(tmp_path):
    out, sub = _mk_export_tree(tmp_path)
    db, _rid = _mk_row(tmp_path, sub)
    (sub / 'x.rpy').write_text('label start:\n    pass\n', encoding='utf-8')
    logs = []
    _exporter(tmp_path, db)._apply_marked_wraps(out, logs.append)
    assert any('定位失败' in l for l in logs)
    db.close()


def test_export_apply_wraps_no_rows(tmp_path):
    out, sub = _mk_export_tree(tmp_path)
    (sub / 'x.rpy').write_text('label start:\n    pass\n', encoding='utf-8')
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    logs = []
    _exporter(tmp_path, db)._apply_marked_wraps(out, logs.append)
    assert logs == []  # 无 wrap 行：静默跳过
    db.close()
