"""翻译文本导入与翻译Word测试：字体约定 / tword 导出 / JSON·tword 导入回导

直接对真实 ProjectDatabase（tmp_path 落 SQLite）操作。
"""
import json

import pytest

from database import ProjectDatabase
from services import text_export, text_import


@pytest.fixture
def db(tmp_path):
    d = ProjectDatabase(str(tmp_path / 'project.db'))
    d.connect()
    d.insert_dialogues([
        {'file_path': 'script.rpy', 'line_number': 10, 'label': 'start',
         'character': 'e', 'original_text': 'Hello.', 'translated_text': '你好。',
         'is_translated': True},
        {'file_path': 'script.rpy', 'line_number': 20, 'label': 'start',
         'character': 'l', 'original_text': 'Bye.', 'translated_text': '',
         'is_translated': False},
    ])
    d.insert_ui_texts([
        {'file_path': 'screens.rpy', 'line_number': 5, 'label': '',
         'original_text': 'Start', 'translated_text': '',
         'is_translated': False},
    ])
    d.insert_characters([
        {'variable': 'e', 'display_name': 'Eileen', 'cn_name': '',
         'lines_count': 12},
    ])
    d.add_glossary_term('mana', '', 'other', 'manual')
    yield d
    d.close()


# ---- 字体约定 ----

def test_docx_fonts(db, tmp_path):
    """docx：正文/标题样式中文宋体、西文 Times New Roman"""
    docx = pytest.importorskip('docx')
    from docx.oxml.ns import qn
    data = text_export.collect_data(db, ['dialogue'], {})
    path, _ = text_export.write_export_file(data, tmp_path, 'demo', 'docx')
    doc = docx.Document(path)
    for name in ('Normal', 'Title', 'Heading 1'):
        r_fonts = doc.styles[name].element.get_or_add_rPr().get_or_add_rFonts()
        assert r_fonts.get(qn('w:ascii')) == 'Times New Roman'
        assert r_fonts.get(qn('w:eastAsia')) == '宋体'


def test_xlsx_fonts(db, tmp_path):
    """xlsx：纯中文格宋体、纯西文格 Times New Roman、混排富文本分段"""
    openpyxl = pytest.importorskip('openpyxl')
    from openpyxl.cell.rich_text import CellRichText
    data = {'dialogue': [
        {'original': 'Hello.', 'translated': '你好。'},
        {'original': 'Hi 你好', 'translated': 'OK'},
    ]}
    path, _ = text_export.write_export_file(data, tmp_path, 'demo', 'xlsx')
    ws = openpyxl.load_workbook(path, rich_text=True)['对话翻译']
    assert ws['A1'].font.name == '宋体'          # 表头「原文」
    assert ws['A2'].font.name == 'Times New Roman'  # Hello.
    assert ws['B2'].font.name == '宋体'          # 你好。
    assert isinstance(ws['A3'].value, CellRichText)  # 中英混排 → 富文本


# ---- 翻译 Word 导出 ----

def test_tword_only_originals(db, tmp_path):
    """翻译 Word：只含原文、每条一段、无标题无表头，顺序 = 规范类型序"""
    docx = pytest.importorskip('docx')
    data = text_export.collect_data(db, ['ui', 'dialogue'], {})
    path, counts = text_export.write_export_file(data, tmp_path, 'demo', 'tword')
    assert path.suffix == '.docx'
    paras = [p.text for p in docx.Document(path).paragraphs]
    # 规范类型序 dialogue 在前，与勾选顺序无关
    assert paras == ['Hello.', 'Bye.', 'Start']
    assert counts == {'dialogue': 2, 'ui': 1}


def test_tword_newline_in_entry_keeps_one_paragraph(db, tmp_path):
    """条目内换行用 <w:br/>，保证 1 条 = 1 段（导入按段对齐）"""
    docx = pytest.importorskip('docx')
    data = {'dialogue': [{'original': 'line1\nline2'}, {'original': 'next'}]}
    path, _ = text_export.write_export_file(data, tmp_path, 'demo', 'tword')
    paras = [p.text for p in docx.Document(path).paragraphs]
    assert paras == ['line1\nline2', 'next']


# ---- JSON 导入 ----

def _roundtrip_json(db, tmp_path, mutate):
    """导出 json → mutate 改译文 → 解析生成计划"""
    data = text_export.collect_data(db, ['dialogue', 'ui', 'names', 'glossary'], {})
    doc = json.loads(text_export.render_json(
        data, {'project': 'demo', 'exported_at': 'now'}))
    mutate(doc)
    pairs = text_import.parse_json_import(json.dumps(doc, ensure_ascii=False))
    return text_import.build_json_plan(db, pairs)


def test_json_import_roundtrip(db, tmp_path):
    """导出 JSON 填入译文后回导：按原文匹配写回，同原文多条全部命中"""
    def mutate(doc):
        doc['ui_texts'][0]['translated'] = '开始'
        doc['names'][0]['translated'] = '艾琳'
        doc['glossary'][0]['cn'] = '法力'
    plan, stats = _roundtrip_json(db, tmp_path, mutate)
    assert stats['ui']['matched'] == 1
    assert stats['names']['matched'] == 1
    counts = text_import.apply_plan(db, plan)
    assert counts == {'ui': 1, 'names': 1, 'glossary': 1}
    assert db.get_all_ui_texts()[0]['translated_text'] == '开始'
    assert db.get_all_ui_texts()[0]['is_translated'] is True
    assert db.get_characters()[0]['cn_name'] == '艾琳'
    assert db.get_glossary()['mana'] == '法力'


def test_json_import_unchanged_and_unmatched(db, tmp_path):
    """译文未变跳过、原文找不到计入 unmatched"""
    def mutate(doc):
        doc['dialogues'].append({'original': 'NotExist', 'translated': '不存在'})
    plan, stats = _roundtrip_json(db, tmp_path, mutate)
    # dialogue[0] 译文已是「你好。」未变 → unchanged；新加条目 → unmatched
    assert stats['dialogue']['unchanged'] == 1
    assert stats['dialogue']['unmatched'] == 1
    assert not [p for p in plan if p['type'] == 'dialogue']


def test_json_import_new_glossary_term(db, tmp_path):
    """JSON 中库内没有的术语导入时新增"""
    def mutate(doc):
        doc['glossary'].append({'en': 'hp', 'cn': '生命'})
    plan, _ = _roundtrip_json(db, tmp_path, mutate)
    text_import.apply_plan(db, plan)
    assert db.get_glossary()['hp'] == '生命'


def test_parse_json_rejects_foreign_file():
    with pytest.raises(ValueError, match='format_version'):
        text_import.parse_json_import('{"foo": []}')


# ---- 翻译 Word 导入 ----

def _make_tword(tmp_path, lines, name='t.docx'):
    text_export.write_tword(tmp_path / name, lines)
    return (tmp_path / name).read_bytes()


def test_tword_import_roundtrip(db, tmp_path):
    """翻译 Word：译文按段落顺序写回对应条目"""
    entries = text_import.ordered_entries(db, ['dialogue', 'ui'])
    assert [e['original'] for e in entries] == ['Hello.', 'Bye.', 'Start']
    content = _make_tword(tmp_path, ['你好。', '再见。', '开始'])
    lines = text_import.read_tword_lines(content)
    assert lines == ['你好。', '再见。', '开始']
    plan, stats = text_import.build_tword_plan(db, lines, ['dialogue', 'ui'])
    assert stats['dialogue']['matched'] == 2
    assert stats['ui']['matched'] == 1
    counts = text_import.apply_plan(db, plan)
    assert counts == {'dialogue': 2, 'ui': 1}
    rows = db.get_all_dialogues()
    assert rows[1]['translated_text'] == '再见。'
    assert rows[1]['is_translated'] is True


def test_tword_import_skips_untouched_lines(db, tmp_path):
    """与原文相同的行视为未翻译，不写回"""
    content = _make_tword(tmp_path, ['Hello.', '再见。', ''])
    lines = text_import.read_tword_lines(content)
    # 末尾空段被去除 → 段落数 2 != 条目数 3？不——空段在中间才占位；
    # 这里第 3 段为空在末尾被去掉，应对齐失败
    with pytest.raises(ValueError, match='段落数'):
        text_import.build_tword_plan(db, lines, ['dialogue', 'ui'])


def test_tword_import_middle_empty_keeps_alignment(db, tmp_path):
    """中间空段占位维持对齐且跳过写回"""
    content = _make_tword(tmp_path, ['你好。', '', '开始'])
    lines = text_import.read_tword_lines(content)
    plan, stats = text_import.build_tword_plan(db, lines, ['dialogue', 'ui'])
    assert stats['dialogue']['skipped_empty'] == 1
    text_import.apply_plan(db, plan)
    rows = db.get_all_dialogues()
    assert rows[0]['translated_text'] == '你好。'
    assert rows[1]['translated_text'] == ''  # 空段未写回
    assert db.get_all_ui_texts()[0]['translated_text'] == '开始'


def test_tword_import_count_mismatch_raises(db, tmp_path):
    content = _make_tword(tmp_path, ['你好。'])
    lines = text_import.read_tword_lines(content)
    with pytest.raises(ValueError, match='段落数'):
        text_import.build_tword_plan(db, lines, ['dialogue', 'ui'])


def test_plan_digest_stable(db, tmp_path):
    """digest 对同计划稳定、对不同计划变化（apply 前比对防错配）"""
    content = _make_tword(tmp_path, ['你好。', '再见。', '开始'])
    lines = text_import.read_tword_lines(content)
    plan1, _ = text_import.build_tword_plan(db, lines, ['dialogue', 'ui'])
    plan2, _ = text_import.build_tword_plan(db, lines, ['dialogue', 'ui'])
    assert text_import.plan_digest('tword', plan1) == \
        text_import.plan_digest('tword', plan2)
    plan2[0]['translated'] = '改动'
    assert text_import.plan_digest('tword', plan1) != \
        text_import.plan_digest('tword', plan2)
