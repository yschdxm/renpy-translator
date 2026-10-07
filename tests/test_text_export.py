"""翻译文本导出测试：collect_data 裁剪/筛选 + 四格式回读 + 列白名单

直接对真实 ProjectDatabase（tmp_path 落 SQLite）操作，
写盘产物用 openpyxl / python-docx 读回断言。
"""
import json

import pytest

from database import ProjectDatabase
from services import text_export


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
         'original_text': 'Start', 'translated_text': '开始',
         'is_translated': True},
        {'file_path': 'screens.rpy', 'line_number': 8, 'label': '',
         'original_text': 'Quit', 'translated_text': '',
         'is_translated': False},
    ])
    d.insert_characters([
        {'variable': 'e', 'display_name': 'Eileen', 'cn_name': '艾琳',
         'lines_count': 12},
    ])
    d.add_glossary_term('mana', '法力', 'other', 'manual')
    yield d
    d.close()


ALL_TYPES = ['dialogue', 'ui', 'names', 'glossary']


def _export(db, tmp_path, fmt, types=None, columns=None, flt=None,
            filename=''):
    types = types or ALL_TYPES
    data = text_export.collect_data(db, types, columns, flt)
    path, counts = text_export.write_export_file(
        data, tmp_path, 'demo', fmt, filename)
    return path, counts


# ---- collect_data：列裁剪 / 白名单 / 筛选 ----

def test_collect_default_columns(db):
    """columns 为空时取该类型全部列"""
    data = text_export.collect_data(db, ['dialogue'], {})
    row = data['dialogue'][0]
    assert set(row) == {'original', 'translated', 'character', 'file',
                        'line', 'label', 'status'}
    assert row['original'] == 'Hello.' and row['status'] is True


def test_collect_column_subset_and_order(db):
    """列裁剪生效且顺序对齐 COLUMN_DEFS（与勾选顺序无关）"""
    data = text_export.collect_data(
        db, ['dialogue'], {'dialogue': ['file', 'original']})
    assert list(data['dialogue'][0]) == ['original', 'file']


def test_collect_bad_column_raises(db):
    with pytest.raises(ValueError, match='未知导出列'):
        text_export.collect_data(db, ['dialogue'], {'dialogue': ['bogus']})


def test_collect_columns_key_outside_types_raises(db):
    with pytest.raises(ValueError, match='未选中的类型'):
        text_export.collect_data(db, ['dialogue'], {'ui': ['original']})


def test_collect_empty_columns_raises(db):
    with pytest.raises(ValueError, match='至少选择一列'):
        text_export.collect_data(db, ['names'], {'names': []})


def test_collect_filter_dialogue(db):
    """filter 只作用于其 content_type 指定的表"""
    flt = {'content_type': 'dialogue', 'filter_mode': 'translated',
           'search': '', 'character': ''}
    data = text_export.collect_data(db, ['dialogue', 'ui'], {}, flt)
    assert len(data['dialogue']) == 1
    assert data['dialogue'][0]['original'] == 'Hello.'
    assert len(data['ui']) == 2  # ui 不受 dialogue 筛选影响


def test_collect_filter_character_and_search(db):
    flt = {'content_type': 'dialogue', 'filter_mode': 'all',
           'search': 'bye', 'character': 'l'}
    data = text_export.collect_data(db, ['dialogue'], {}, flt)
    assert len(data['dialogue']) == 1
    assert data['dialogue'][0]['character'] == 'l'


# ---- 四格式回读 ----

def test_txt_sections_and_counts(db, tmp_path):
    path, counts = _export(db, tmp_path, 'txt')
    text = path.read_text(encoding='utf-8-sig')  # 写盘带 BOM 供记事本
    assert '===== 对话翻译（共 2 条） =====' in text
    assert '===== 人名表（共 1 条） =====' in text
    assert '[script.rpy:10 (start) e]' in text
    assert '原文：Hello.' in text and '译文：你好。' in text
    assert counts == {'dialogue': 2, 'ui': 2, 'names': 1, 'glossary': 1}


def test_txt_omits_unchecked_columns(db, tmp_path):
    path, _ = _export(db, tmp_path, 'txt', types=['dialogue'],
                      columns={'dialogue': ['original', 'translated']})
    text = path.read_text(encoding='utf-8-sig')
    assert '说话人' not in text and '[' not in text.split('=====', 1)[-1]


def test_json_schema(db, tmp_path):
    path, _ = _export(db, tmp_path, 'json',
                      columns={'names': ['original', 'translated']})
    doc = json.loads(path.read_text(encoding='utf-8'))
    assert doc['project'] == 'demo' and doc['format_version'] == 1
    assert len(doc['dialogues']) == 2 and len(doc['ui_texts']) == 2
    assert doc['names'] == [{'original': 'Eileen', 'translated': '艾琳'}]
    assert doc['glossary'][0]['en'] == 'mana'


def test_xlsx_sheets_and_rows(db, tmp_path):
    openpyxl = pytest.importorskip('openpyxl')
    path, _ = _export(db, tmp_path, 'xlsx')
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ['对话翻译', 'UI 字符串', '人名表', '术语表']
    ws = wb['对话翻译']
    assert ws['A1'].value == '原文' and ws['B1'].value == '译文'
    assert ws['A1'].font.bold
    assert ws.max_row == 3  # 表头 + 2 行
    assert ws['A2'].value == 'Hello.' and ws['B2'].value == '你好。'
    assert ws.freeze_panes == 'A2'


def test_docx_headings_and_tables(db, tmp_path):
    docx = pytest.importorskip('docx')
    path, _ = _export(db, tmp_path, 'docx')
    doc = docx.Document(path)
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith('Heading')]
    assert any('对话翻译（共 2 条）' in h for h in headings)
    assert len(doc.tables) == 4
    table = doc.tables[0]
    assert table.rows[0].cells[0].text == '原文'
    assert len(table.rows) == 3
    assert table.rows[1].cells[1].text == '你好。'


# ---- 文件名 ----

def test_filename_sanitize_and_dedup(db, tmp_path):
    p1, _ = _export(db, tmp_path, 'txt', types=['names'], filename='我的导出')
    assert p1.name == '我的导出.txt'
    p2, _ = _export(db, tmp_path, 'txt', types=['names'], filename='我的导出')
    assert p2.name == '我的导出-2.txt'  # 不静默覆盖


def test_filename_default_timestamp(db, tmp_path):
    path, _ = _export(db, tmp_path, 'json', types=['names'])
    assert path.name.startswith('demo-texts-') and path.suffix == '.json'
