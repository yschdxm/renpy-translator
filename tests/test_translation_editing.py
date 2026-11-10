"""用真实 SQLite 验证跨页替换、范围隔离与预览后的并发修改保护。"""
import pytest

from database import ProjectDatabase


@pytest.fixture
def db(tmp_path):
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    db.connect()
    yield db
    db.close()


@pytest.mark.parametrize('kind', ['dialogue', 'ui'])
def test_replace_all_pages_literal_and_preserve_original(db, kind):
    insert = db.insert_dialogues if kind == 'dialogue' else db.insert_ui_texts
    insert([dict(original_text=f'Ami {i}', translated_text='阿米，阿米！[x] 100%',
                 is_translated=True) for i in range(75)])
    preview = db.replace_translations(kind, '阿米', '亚美')
    assert preview['count'] == 75 and preview['occurrences'] == 150
    assert len(preview['samples']) == 30
    assert preview['samples'][0]['before'] == '阿米，阿米！[x] 100%'
    result = db.replace_translations(kind, '阿米', '亚美', expected_digest=preview['digest'])
    assert result['count'] == 75
    page = db.get_dialogues_page if kind == 'dialogue' else db.get_ui_texts_page
    rows, _ = page(page_size=100)
    assert all(r['original_text'].startswith('Ami') for r in rows)
    assert all(r['translated_text'] == '亚美，亚美！[x] 100%' for r in rows)
    assert all(r['is_translated'] for r in rows)
    # SQL wildcard characters are literal in the replacement search.
    assert db.replace_translations(kind, '100%', '50%')['count'] == 75
    assert db.replace_translations(kind, '100_', '50')['count'] == 0


def test_replace_scope_and_conflict_are_atomic(db):
    db.insert_dialogues([
        dict(original_text='Ami hello', character='a', translated_text='阿米', is_translated=True),
        dict(original_text='Ami hello', character='b', translated_text='阿米', is_translated=True),
        dict(original_text='Goodbye', character='a', translated_text='阿米', is_translated=True),
    ])
    args = dict(content_type='dialogue', find='阿米', replacement='亚美',
                search='hello', character='a', filter_mode='translated')
    preview = db.replace_translations(**args)
    assert preview['count'] == 1
    item_id = preview['samples'][0]['id']
    db.update_dialogue(item_id, '阿米你好')
    with pytest.raises(ValueError, match='重新预览'):
        db.replace_translations(**args, expected_digest=preview['digest'])
    assert db.get_dialogue(item_id)['translated_text'] == '阿米你好'
    preview = db.replace_translations(**args)
    db.replace_translations(**args, expected_digest=preview['digest'])
    assert [r['translated_text'] for r in db.get_all_dialogues()] == ['亚美你好', '阿米', '阿米']


def test_new_match_invalidates_preview_and_empty_find_rejected(db):
    preview = db.replace_translations('ui', '阿米', '亚美')
    db.insert_ui_texts([dict(original_text='Ami', translated_text='阿米')])
    with pytest.raises(ValueError):
        db.replace_translations('ui', '阿米', '亚美', expected_digest=preview['digest'])
    with pytest.raises(ValueError):
        db.replace_translations('ui', '', '亚美')
    assert db.replace_translations('ui', '阿米', '阿米')['count'] == 0


def test_context_lines_actually_limit_reference(db):
    from types import SimpleNamespace
    from translation_service import TranslationService
    translator = SimpleNamespace(config=SimpleNamespace(context_lines=3))
    service = TranslationService(translator, db, None, max_context_k=32)
    try:
        assert service._calc_context_count() == 3
        translator.config.context_lines = 0
        assert service._calc_context_count() == 0
        translator.config.context_lines = 12
        assert service._calc_context_count() == 12
    finally:
        service.close()


def test_clear_translation_marks_untranslated(db):
    """空译文=清空：is_translated 必须回退 0（单条 + 批量、whitespace 同样视为空）"""
    db.insert_dialogues([
        dict(original_text='Ami hello', translated_text='阿米', is_translated=True),
        dict(original_text='Bye', translated_text='拜', is_translated=True),
    ])
    db.insert_ui_texts([
        dict(original_text='OK', translated_text='好', is_translated=True),
        dict(original_text='Cancel', translated_text='取消', is_translated=True),
    ])
    d1, d2 = db.get_all_dialogues()
    db.update_dialogue(d1['id'], '')
    assert db.get_dialogue(d1['id'])['translated_text'] == ''
    assert not db.get_dialogue(d1['id'])['is_translated']
    db.update_dialogues_batch([(d2['id'], '   ')])
    assert not db.get_dialogue(d2['id'])['is_translated']
    u1, u2 = db.get_all_ui_texts()
    db.update_ui_text(u1['id'], '')
    assert not db.get_ui_text(u1['id'])['is_translated']
    db.update_ui_texts_batch([(u2['id'], '取消')])
    assert db.get_ui_text(u2['id'])['is_translated']


def test_bulk_clear_and_ids_filter(db):
    """多选清空：ids 按筛选取全量，clear 批量回退未翻译且不动筛选外的行"""
    db.insert_dialogues([
        dict(original_text='Line A', character='a', translated_text='甲',
             is_translated=True),
        dict(original_text='Line B', character='b', translated_text='乙',
             is_translated=True),
        dict(original_text='Line C', character='a', translated_text='',
             is_translated=False),
    ])
    db.insert_ui_texts([
        dict(original_text='OK', translated_text='好', is_translated=True),
        dict(original_text='Load', translated_text='读取', is_translated=True),
    ])
    all_ids = db.get_ids_filtered('dialogue', 'all')
    assert len(all_ids) == 3
    # 角色筛选只取该角色的 id
    assert db.get_ids_filtered('dialogue', 'all', character='a') == [
        r['id'] for r in db.get_all_dialogues() if r['character'] == 'a']
    # 只清已选两条：第三条不动
    cleared = db.clear_translations('dialogue', all_ids[:2])
    assert cleared == 2
    rows = {r['id']: r for r in db.get_all_dialogues()}
    assert not rows[all_ids[0]]['is_translated'] and rows[all_ids[0]]['translated_text'] == ''
    assert not rows[all_ids[1]]['is_translated']
    assert rows[all_ids[2]]['translated_text'] == ''      # 未译行不变
    # ui 与空列表
    assert db.clear_translations('ui', db.get_ids_filtered('ui')) == 2
    assert db.clear_translations('dialogue', []) == 0


def test_failed_count_excludes_duplicates_and_manually_corrected_rows(db):
    db.insert_dialogues([dict(original_text='Hello'), dict(original_text='Goodbye')])
    items = db.get_all_dialogues()
    db.add_failed_batch('dialogue', items)
    db.add_failed_batch('dialogue', items)
    assert db.count_failed_items('dialogue') == 2
    db.update_dialogue(items[0]['id'], '你好')
    assert db.count_failed_items('dialogue') == 1
    assert db.count_failed_items('ui') == 0


@pytest.mark.parametrize('kind,table', [('dialogue', 'dialogues'), ('ui', 'ui_texts')])
def test_failed_count_does_not_scan_unrelated_untranslated_rows(db, kind, table):
    db._conn.executemany(f'INSERT INTO {table} (original_text) VALUES (?)',
                        [('Hello',)] * 30000)
    db._conn.commit()
    db.add_failed_batch(kind, [{'id': 1}, {'id': 2}])
    # A VM-instruction budget detects a full-project scan without wall-clock
    # timing assumptions. Counting two pending IDs should take little work.
    calls = 0

    def limit_work():
        nonlocal calls
        calls += 1
        return calls > 100

    db._conn.set_progress_handler(limit_work, 1000)
    try:
        assert db.count_failed_items(kind) == 2
    finally:
        db._conn.set_progress_handler(None, 0)


def test_replace_api_preview_confirm_conflict_and_validation(db):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.api.texts import router
    from server.errors import register_error_handlers

    async def db_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    app = FastAPI()
    app.state.app_state = SimpleNamespace(db=db, db_call=db_call)
    app.include_router(router, prefix='/api')
    register_error_handlers(app)
    db.insert_ui_texts([dict(original_text='Ami', translated_text='阿米', is_translated=True)])
    with TestClient(app) as client:
        url = '/api/current/texts/ui/replace'
        args = {'find': '阿米', 'replacement': '亚美'}
        preview = client.post(url, json=args)
        assert preview.status_code == 200 and preview.json()['count'] == 1
        assert db.get_ui_text(1)['translated_text'] == '阿米'
        assert client.post(url, json={**args, 'expected_digest': 'stale'}).status_code == 409
        response = client.post(url, json={**args, 'expected_digest': preview.json()['digest']})
        assert response.status_code == 200 and db.get_ui_text(1)['translated_text'] == '亚美'
        assert client.post(url, json={'find': ''}).status_code == 422
        assert client.post('/api/current/texts/bad/replace', json=args).status_code == 404
        assert client.get('/api/current/texts/ui/failed-batches/count').json() == {'count': 0}


@pytest.mark.parametrize('kind', ['dialogue', 'ui'])
async def test_failed_list_prunes_repeated_attempts_and_preserves_latest_details(db, kind):
    from types import SimpleNamespace
    from server.api.texts import list_failed_items

    insert = db.insert_dialogues if kind == 'dialogue' else db.insert_ui_texts
    update = db.update_dialogue if kind == 'dialogue' else db.update_ui_text
    insert([dict(original_text='One'), dict(original_text='Two')])
    db.add_failed_batch(kind, [{'id': 1, 'reason': 'old'}, {'id': 2}])
    db.add_failed_batch(kind, [{'id': 1, 'reason': 'new', 'rejected': 'candidate'},
                               {'id': 1, 'reason': 'duplicate'}, {'id': 999}])
    update(2, '二')

    async def db_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    state = SimpleNamespace(db=db, db_call=db_call)
    result = await list_failed_items(kind, state)
    assert result['count'] == db.count_failed_items(kind) == 1
    assert result['items'][0]['id'] == 1
    assert result['items'][0]['reason'] == 'new'
    assert result['items'][0]['rejected'] == 'candidate'
    assert sum(len(r['items']) for r in db.list_failed_batches(kind)) == 1
    assert await list_failed_items(kind, state) == result


async def test_cancel_after_batch_preserves_results_and_skips_final_retry(db):
    from types import SimpleNamespace
    from server.api.texts import _make_translate_job
    from server.jobs.registry import JobCancelled

    db.insert_ui_texts([dict(original_text='One'), dict(original_text='Two')])
    items = db.get_untranslated_ui_texts()
    logs, progress, calls = [], [], []
    cancelled = False

    async def db_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def prepare(items, content_type):
        return [items]

    async def translate(batch, content_type):
        nonlocal cancelled
        calls.append(batch)
        db.update_ui_text(batch[0]['id'], '一')
        db.add_failed_batch(content_type, batch[1:])
        cancelled = True
        return {batch[0]['id']: '一'}

    def check():
        if cancelled:
            raise JobCancelled()

    state = SimpleNamespace(db=db, db_call=db_call, translation_service=SimpleNamespace(
        prepare_batches=prepare, translate_batch=translate))
    job = SimpleNamespace(check_cancelled=check, emit_log=logs.append,
                          emit_progress=lambda value, text: progress.append(text))
    with pytest.raises(JobCancelled):
        await _make_translate_job(state, 'ui', items)(job)
    assert len(calls) == 1
    assert db.get_ui_text(items[0]['id'])['translated_text'] == '一'
    assert db.count_failed_items('ui') == 1
    assert '成功 1' in progress[-1] and '待重试 1' in progress[-1]


def test_failed_batches_pagination(db):
    """失败条目分页：大失败量（数万条）全量返回+渲染会卡死前端。
    items 仅当前页，count 始终是总条数；无参数默认 50 条/页"""
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.api.texts import router
    from server.errors import register_error_handlers

    async def db_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    app = FastAPI()
    app.state.app_state = SimpleNamespace(db=db, db_call=db_call)
    app.include_router(router, prefix='/api')
    register_error_handlers(app)
    db.insert_dialogues([dict(original_text=f'L{i}') for i in range(120)])
    db.add_failed_batch('dialogue', db.get_all_dialogues())
    with TestClient(app) as client:
        base = '/api/current/texts/dialogue/failed-batches'
        data = client.get(f'{base}?page=0&size=50').json()
        assert data['count'] == 120 and len(data['items']) == 50
        assert len(client.get(f'{base}?page=1&size=50').json()['items']) == 50
        assert len(client.get(f'{base}?page=2&size=50').json()['items']) == 20
        # 无参数默认 50 条/页（旧前端全量渲染的入口也不再卡死）
        assert len(client.get(base).json()['items']) == 50
        # 页码越界返回空页，总数不变
        empty = client.get(f'{base}?page=99').json()
        assert empty['items'] == [] and empty['count'] == 120
