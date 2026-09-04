"""内嵌 strings 表应用路径（embedded_table）测试

核心契约（文本形态，生产坑）：
- 文件形态（old 行）：\ → \\、 " → \"、真实换行 → 字面 \n
- 入库形态（ui_texts.original_text）：文件形态仅去掉引号转义
  （_fill_strings/_parse_strings_block 读 old 行只反转义 \"）
- 导出 translation_dict 以入库形态为 key——zz 文件写入→解析→入库→
  填充的整条链路形态必须闭环，否则译文永远填不进
- 重复 old 是 Ren'Py 硬错误：写前必须按现有 tl old 集合去重
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from database import ProjectDatabase
from embedded_strings import Candidate
from services import embedded_table as et
from tl_parser import _parse_strings_block


def _mk_project(tmp_path) -> Path:
    """造嵌套布局的项目游戏目录：game/game/{a.rpy, tl/chinese/}"""
    root = tmp_path / 'game'
    sub = root / 'game'
    (sub / 'tl' / 'chinese').mkdir(parents=True)
    (sub / 'a.rpy').write_text(
        'screen s:\n    text "Hello there"\n'
        'label start:\n    $ msg = "Multi\\nline text"\n',
        encoding='utf-8')
    return root


def _entry(text='Hello there', file='a.rpy', line=2, hint='界面'):
    return {'text': text, 'db_form': et.to_db_form(text),
            'file': file, 'line': line, 'hint': hint}


# ---- 形态转换 ----

def test_escape_forms():
    t = 'say "hi"\nthere'
    assert et.to_db_form(t) == 'say "hi"\\nthere'          # 引号不转义
    assert et.escape_tl_old(t) == 'say \\"hi\\"\\nthere'   # 文件形态带 \"


def test_backslash_doubled():
    t = 'a\\b'
    assert et.to_db_form(t) == 'a\\\\b'
    assert et.escape_tl_old(t) == 'a\\\\b'


# ---- 写入 → 解析闭环 ----

def test_write_parse_roundtrip(tmp_path):
    tl = tmp_path / 'tl'
    entries = [_entry('Multi\nline text')]
    out = et.write_strings_table(tl, entries)
    ui = []
    _parse_strings_block(out.read_text(encoding='utf-8').split('\n'),
                         out, tmp_path, ui)
    assert len(ui) == 1
    # 入库形态：换行是字面 \n（与 _fill_strings 查 key 的形态一致）
    assert ui[0]['original_text'] == 'Multi\\nline text'


def test_write_empty_deletes_file(tmp_path):
    tl = tmp_path / 'tl'
    out = et.write_strings_table(tl, [_entry()])
    assert out.exists()
    et.write_strings_table(tl, [])
    assert not out.exists()


def test_existing_tl_olds(tmp_path):
    tl = tmp_path / 'tl'
    tl.mkdir()
    (tl / 'other.rpy').write_text(
        'translate chinese strings:\n\n    old "Hello there"\n    new "x"\n',
        encoding='utf-8')
    et.write_strings_table(tl, [_entry('Table text')])
    olds = et.existing_tl_olds(tl)
    assert 'Hello there' in olds
    assert 'Table text' not in olds  # ZZ 自身排除（全量重写）


# ---- collect 三重过滤 ----

def test_collect_filters(tmp_path):
    root = _mk_project(tmp_path)
    source_root = root / 'game'
    rows = [
        {'rel_file': 'a.rpy', 'raw': '"Hello there"', 'text': 'Hello there',
         'line': 2, 'hint': '界面'},
        # 源码不存在（raw 不在文件里）
        {'rel_file': 'a.rpy', 'raw': '"Ghost"', 'text': 'Ghost',
         'line': 9, 'hint': ''},
        # 同文本重复（按入库形态去重）
        {'rel_file': 'a.rpy', 'raw': '"Hello there"', 'text': 'Hello there',
         'line': 2, 'hint': '界面'},
    ]
    entries, vanished, duped = et.collect_table_entries(rows, source_root, set())
    assert len(entries) == 1
    assert vanished == 1 and duped == 1
    assert entries[0]['text'] == 'Hello there'
    # 已在现有 old 集合的也不收
    entries, vanished, duped = et.collect_table_entries(
        rows[:1], source_root, {'Hello there'})
    assert entries == [] and duped == 1


# ---- regen 端到端（DB + 文件 + 导出填充闭环） ----

def _db_with_table_row(tmp_path, text='Hello there', raw='"Hello there"'):
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    c = Candidate(
        file='game/game/a.rpy', rel_file='a.rpy', line=2,
        col_start=10, col_end=10 + len(raw), raw=raw, text=text,
        kind='screen', hint='s界面·界面文本', confidence='high')
    rid = db.merge_embedded_candidates([c])[0]['id']
    db.update_embedded_ai(rid, 1, '界面文本', False, 'a.rpy:2', 'table',
                          'coarse')
    db.set_embedded_status([rid], 'marked')
    return db


def test_regen_end_to_end(tmp_path):
    root = _mk_project(tmp_path)
    db = _db_with_table_row(tmp_path)
    inserted = et.regen_embedded_table(db, root)
    assert inserted == 1

    zz = root / 'game' / 'tl' / 'chinese' / et.ZZ_NAME
    assert zz.exists()
    content = zz.read_text(encoding='utf-8')
    assert 'old "Hello there"' in content

    # 入库：original_text 为入库形态，context_hint 回填
    ui = [r for r in db.get_all_ui_texts() if r['original_text'] == 'Hello there']
    assert len(ui) == 1
    assert ui[0]['context_hint'] == 's界面·界面文本'

    # 导出填充闭环：translation_dict 以入库形态为 key 能填进 new
    from services.game_export import GameExporter
    ex = object.__new__(GameExporter)
    ex._blocked = []
    ex._cancel_event = None
    filled = ex._fill_strings(root / 'game' / 'tl' / 'chinese',
                              {'Hello there': '你好'})
    assert filled == 1
    assert 'new "你好"' in zz.read_text(encoding='utf-8')
    db.close()


def test_regen_vanished_text_not_written(tmp_path):
    """已标记文本在新源码中消失：不写文件，行保持 marked"""
    root = _mk_project(tmp_path)
    db = _db_with_table_row(tmp_path, text='Ghost text', raw='"Ghost text"')
    inserted = et.regen_embedded_table(db, root)
    assert inserted == 0
    zz = root / 'game' / 'tl' / 'chinese' / et.ZZ_NAME
    assert not zz.exists()
    marked = db.get_table_marked_embedded()
    assert len(marked) == 1  # 行保持 marked（文本回归后自动恢复）
    db.close()


# ---- 源码只读化：wrap 行同样走 zz 合成条目 ----

def _db_with_wrap_row(tmp_path, text='Hello there', raw='"Hello there"'):
    """wrap 路径行（raw 须真实存在于 a.rpy 源码，collect 会校验）"""
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    c = Candidate(
        file='game/game/a.rpy', rel_file='a.rpy', line=2,
        col_start=10, col_end=10 + len(raw), raw=raw, text=text,
        kind='screen', hint='拼接片段', confidence='low')
    rid = db.merge_embedded_candidates([c])[0]['id']
    db.update_embedded_ai(rid, 1, '拼接片段', False, 'a.rpy:2', 'wrap',
                          'refine')
    db.set_embedded_status([rid], 'marked')
    return db, rid


def test_regen_includes_wrap_rows(tmp_path):
    """源码只读化：wrap 路径行的译文条目与 table 行一样由 zz 表合成——
    _() 查找与 strings 表共用存储，导出时包裹的 _() 查这些条目"""
    root = _mk_project(tmp_path)
    db, _rid = _db_with_wrap_row(tmp_path)
    inserted = et.regen_embedded_table(db, root)
    assert inserted == 1
    zz = root / 'game' / 'tl' / 'chinese' / et.ZZ_NAME
    content = zz.read_text(encoding='utf-8')
    assert 'old "Hello there"' in content
    # 源码未被写入 _()（工作副本保持原样）
    src = (root / 'game' / 'a.rpy').read_text(encoding='utf-8')
    assert '_("Hello there")' not in src
    db.close()


def test_apply_path_empty_migration(tmp_path):
    """旧库 marked 行 apply_path='' 连接时归一为 'table'（
    语义本就=table，但 get_table_marked_embedded 查不到，导致标记从未生效）"""
    db = _db_with_table_row(tmp_path)
    # 模拟旧库：清回 ''
    db._conn.execute(
        "UPDATE embedded_candidates SET apply_path='' WHERE status='marked'")
    db._conn.commit()
    db.close()
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    db.connect()  # 触发迁移
    row = db.get_table_marked_embedded()
    assert len(row) == 1
    assert db.get_marked_embedded() == []  # wrap-only 语义
    db.close()


def test_apply_selection_wrap_no_source_change_no_sdk(tmp_path):
    """源码只读化：wrap 行标记不再需要 SDK、不触碰源码；
    wrapped 恒 0（包裹推迟到导出副本）"""
    import asyncio
    from logger import TranslationLogger
    from services.embedded_pipeline import EmbeddedPipeline

    root = _mk_project(tmp_path)
    db, rid = _db_with_wrap_row(tmp_path)
    rec = db.get_embedded_candidate(rid)
    c = Candidate(
        file=str(root / 'game' / rec['rel_file']), rel_file=rec['rel_file'],
        line=rec['line'], col_start=rec['col_start'],
        col_end=rec['col_start'] + len(rec['raw']), raw=rec['raw'],
        text=rec['text'], kind=rec['kind'], hint=rec['hint'],
        confidence=rec['confidence'], apply_path='wrap')
    rows = [{'id': rid, 'candidate': c, 'ai_keep': 1, 'apply_path': 'wrap'}]

    src_before = (root / 'game' / 'a.rpy').read_text(encoding='utf-8')
    pipe = object.__new__(EmbeddedPipeline)
    pipe.db = db
    pipe.game_root = root
    pipe.sdk_path = None   # 无 SDK 也必须成功
    pipe.logger = TranslationLogger()
    result = asyncio.get_event_loop().run_until_complete(
        pipe.apply_selection(rows, rows))

    assert result['wrapped'] == 0
    assert result['tabled'] == 1
    # 工作副本零改动
    assert (root / 'game' / 'a.rpy').read_text(encoding='utf-8') == src_before
    # zz 表含该 wrap 行的合成条目
    zz = (root / 'game' / 'tl' / 'chinese' / et.ZZ_NAME).read_text(
        encoding='utf-8')
    assert 'old "Hello there"' in zz
    db.close()


# ---- apply_selection 分流 ----

def test_apply_selection_table_only_no_sdk(tmp_path):
    """table 行不需要 SDK：写表入库，wrap=0，不触 SDK 流程"""
    import asyncio
    from logger import TranslationLogger
    from services.embedded_pipeline import EmbeddedPipeline

    root = _mk_project(tmp_path)
    db = _db_with_table_row(tmp_path)
    # 重建 rows/chosen（candidate 从 db 行重建，apply_path=table）
    row_id = db._conn.execute(
        "SELECT id FROM embedded_candidates").fetchone()['id']
    rec = db.get_embedded_candidate(row_id)
    c = Candidate(
        file=str(root / 'game' / rec['rel_file']), rel_file=rec['rel_file'],
        line=rec['line'], col_start=rec['col_start'],
        col_end=rec['col_start'] + len(rec['raw']), raw=rec['raw'],
        text=rec['text'], kind=rec['kind'], hint=rec['hint'],
        confidence=rec['confidence'])
    rows = [{'id': row_id, 'candidate': c, 'ai_keep': 1,
             'apply_path': 'table'}]

    pipe = object.__new__(EmbeddedPipeline)
    pipe.db = db
    pipe.game_root = root
    pipe.sdk_path = None   # 无 SDK：table-only 必须不报错
    pipe.logger = TranslationLogger()

    # 行先回 pending（apply 会重新标 marked）
    db.set_embedded_status([row_id], 'pending')
    result = asyncio.get_event_loop().run_until_complete(
        pipe.apply_selection(rows, rows))

    assert result['tabled'] == 1
    assert result['wrapped'] == 0
    assert result['inserted'] == 1
    assert (root / 'game' / 'tl' / 'chinese' / et.ZZ_NAME).exists()
    assert db.get_table_marked_embedded()[0]['id'] == row_id
    db.close()
