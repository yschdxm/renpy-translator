"""术语表数据层测试：分页/搜索/筛选/排序/删除/覆盖更新

直接对真实 ProjectDatabase（tmp_path 落 SQLite）操作，
覆盖 server/api/glossary.py 依赖的仓库语义。
"""
import pytest

from database import ProjectDatabase

TERMS = [
    # (en_term, cn_term, term_type, source)
    ('apple', '苹果', 'item', 'ai'),
    ('Banana', '香蕉', 'item', 'ai'),
    ('cherry', '樱桃', 'item', 'manual'),
    ('dragon', '龙', 'monster', 'manual'),
    ('elder scroll', '上古卷轴', 'item', 'manual'),
]


@pytest.fixture
def db(tmp_path):
    d = ProjectDatabase(str(tmp_path / 'project.db'))
    d.connect()
    for en, cn, ttype, src in TERMS:
        d.add_glossary_term(en, cn, ttype, src)
    yield d
    d.close()


# ---- get_glossary_page ----

def test_page_total_and_size(db):
    """分页：total 为全量，行数受 size 限制"""
    rows, total = db.get_glossary_page(page=0, size=2)
    assert total == 5
    assert len(rows) == 2
    rows2, _ = db.get_glossary_page(page=2, size=2)
    assert len(rows2) == 1
    # 完整字段
    assert set(rows[0]) >= {'en_term', 'cn_term', 'term_type', 'source',
                            'created_at'}


def test_search_case_insensitive(db):
    """search 对 en_term/cn_term 做大小写不敏感 LIKE"""
    rows, total = db.get_glossary_page(search='APPLE')
    assert total == 1
    assert rows[0]['en_term'] == 'apple'
    # 命中中文
    rows, total = db.get_glossary_page(search='卷轴')
    assert total == 1
    assert rows[0]['en_term'] == 'elder scroll'


def test_source_filter(db):
    """source 非空时精确筛选"""
    rows, total = db.get_glossary_page(source='ai')
    assert total == 2
    assert all(r['source'] == 'ai' for r in rows)
    rows, total = db.get_glossary_page(source='manual')
    assert total == 3


def test_sort_created_at_desc(db):
    """sort_by=created_at desc：时间戳新的排前面"""
    # 直接写可区分的时间戳（Windows 时钟精度可能让连续插入拿到相同值）
    for i, (en, *_rest) in enumerate(TERMS):
        db._conn.execute("UPDATE glossary SET created_at=? WHERE en_term=?",
                         (f"2026-01-0{i + 1}T00:00:00", en))
    db._conn.commit()
    rows, _ = db.get_glossary_page(sort_by='created_at', sort_order='desc')
    assert [r['en_term'] for r in rows] == [t[0] for t in reversed(TERMS)]


def test_sort_cn_term(db):
    """sort_by 白名单内的其他列正常排序"""
    rows, _ = db.get_glossary_page(sort_by='cn_term', sort_order='asc')
    assert [r['cn_term'] for r in rows] == sorted(r['cn_term'] for r in rows)


def test_sort_by_invalid_falls_back(db):
    """非法 sort_by（含注入尝试）回落 en_term 排序（SQLite BINARY 序）"""
    expected = sorted(t[0] for t in TERMS)
    rows, _ = db.get_glossary_page(sort_by='en_term; DROP TABLE glossary;--')
    assert [r['en_term'] for r in rows] == expected
    # 表仍然存在
    _, total = db.get_glossary_page()
    assert total == 5
    # 非法 sort_order 回落 asc
    rows, _ = db.get_glossary_page(sort_order='drop')
    assert [r['en_term'] for r in rows] == expected


# ---- delete_glossary_term ----

def test_delete_glossary_term(db):
    """删除后查不到，总数减一"""
    db.delete_glossary_term('apple')
    assert db.get_glossary_term('apple') is None
    _, total = db.get_glossary_page()
    assert total == 4


# ---- add_glossary_term 覆盖更新（PATCH 依赖的语义）----

def test_add_glossary_term_upsert(db):
    """同 en_term 再写：cn_term 更新且行数不变"""
    db.add_glossary_term('apple', '大苹果', 'item', 'ai')
    row = db.get_glossary_term('apple')
    assert row['cn_term'] == '大苹果'
    _, total = db.get_glossary_page()
    assert total == 5


def test_add_glossary_term_preserves_source_when_passed(db):
    """编辑时传回原 source：ai 来源不会被覆盖成 manual"""
    db.add_glossary_term('apple', '苹果2', 'item', 'ai')
    assert db.get_glossary_term('apple')['source'] == 'ai'
