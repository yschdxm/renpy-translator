"""批次翻译「句数不匹配自动重试 + 部分落库 + 失败暂存」测试

不打网络：用 fake message 喂给真实的 AITranslator.translate_batch
（_call_api 打桩），再经 TranslationService 落真实 SQLite（tmp_path）。

契约（translator 层）：
- 返回 (merged {0基索引: 译文}, terms, fail_reasons {0基索引: 原因})
- 重试只重发未译出/存疑的条目（子集 id 重编），已成功的不再重发也不被覆盖
- 未返回/为空/存疑（贴错行、长度悬殊）的条目不进 merged，原因进 fail_reasons

契约（service 层）：匹配的译文落库，未译出的逐条暂存 failed_batches，
任务不再因句数不匹配中断。
"""
import json
from types import SimpleNamespace

import pytest

from database import ProjectDatabase
from logger import TranslationLogger
from translation_service import TranslationService
from translator import AITranslator, TranslationConfig

DIALOGUES = [
    {'file_path': 'game/a.rpy', 'line_number': 1, 'label': 'start',
     'character': 'e', 'original_text': 'Hello there.'},
    {'file_path': 'game/a.rpy', 'line_number': 2, 'label': 'start',
     'character': 'e', 'original_text': 'How are you?'},
    {'file_path': 'game/a.rpy', 'line_number': 3, 'label': 'start',
     'character': '', 'original_text': 'A quiet day.'},
]


def _fake_message(translations, terms=None):
    """伪造 tool_calls 响应消息（OpenAI SDK 返回对象的 duck-type 替身）"""
    args = json.dumps({'translations': translations, 'terms': terms or []},
                      ensure_ascii=False)
    fn = SimpleNamespace(arguments=args)
    return SimpleNamespace(tool_calls=[SimpleNamespace(function=fn)])


def _ok_message(n):
    return _fake_message(
        [{'id': i + 1, 'translation': f'译文{i + 1}'} for i in range(n)])


def _items(n=3):
    return [{'original_text': d['original_text'], 'character': d['character']}
            for d in DIALOGUES[:n]]


@pytest.fixture
def translator():
    # api_key 非空才会建 OpenAI client（纯构造，不发请求）
    return AITranslator(TranslationConfig(api_key='offline-test-key'))


@pytest.fixture
def db(tmp_path):
    d = ProjectDatabase(str(tmp_path / 'project.db'))
    d.connect()
    d.insert_dialogues(DIALOGUES)
    yield d
    d.close()


# ---- _parse_tool_response 单元行为 ----

def test_parse_partial_placement():
    """句数不足：能匹配的照常落位，缺失的留空（不再整批判废）"""
    msg = _fake_message([{'id': 1, 'translation': '你好'}])
    placed, terms, suspicious = AITranslator._parse_tool_response(msg, _items(2))
    assert placed == {0: '你好'}
    assert terms == []
    assert suspicious == {}


def test_parse_out_of_order_aligned_by_id():
    """模型乱序返回时按 id 对齐"""
    msg = _fake_message([
        {'id': 2, 'translation': '二'},
        {'id': 1, 'translation': '一'},
    ])
    placed, terms, _ = AITranslator._parse_tool_response(msg, _items(2))
    assert placed == {0: '一', 1: '二'}
    assert terms == []


def test_parse_empty_translation_rejected():
    """空译文不能静默通过（否则该条永远漏译）"""
    msg = _fake_message([
        {'id': 1, 'translation': '一'},
        {'id': 2, 'translation': ''},
    ])
    placed, _, _ = AITranslator._parse_tool_response(msg, _items(2))
    assert placed == {0: '一'}


def test_parse_misaligned_translation_suspicious():
    """贴错行：译文与其他条目原文高度相似且明显高于本条 → 存疑移出"""
    items = [{'original_text': 'I love you so much, darling.'},
             {'original_text': 'The weather is nice today.'}]
    msg = _fake_message([
        {'id': 1, 'translation': '我非常爱你，亲爱的。'},
        {'id': 2, 'translation': 'I love you so much, darling.'},  # 贴了第 1 条的原文
    ])
    placed, _, suspicious = AITranslator._parse_tool_response(msg, items)
    assert placed == {0: '我非常爱你，亲爱的。'}
    assert '贴错行' in suspicious[1]


def test_parse_similar_originals_not_flagged():
    """两条原文本身相似时，译文与本条原文同样相似 → 不误报贴错行"""
    items = [{'original_text': 'I love you.'},
             {'original_text': 'I love you!'}]
    msg = _fake_message([
        {'id': 1, 'translation': 'I love you.'},  # 与两条原文都几乎相同
        {'id': 2, 'translation': '我爱你！'},
    ])
    placed, _, suspicious = AITranslator._parse_tool_response(msg, items)
    assert placed == {0: 'I love you.', 1: '我爱你！'}
    assert suspicious == {}


def test_parse_length_mismatch_suspicious():
    """长度悬殊：长原文配极短译文 → 存疑移出；短原文不检查"""
    items = [{'original_text': 'This is a fairly long sentence that should '
                               'translate into something substantial.'},
             {'original_text': 'Short.'}]
    msg = _fake_message([
        {'id': 1, 'translation': '短'},
        {'id': 2, 'translation': '短'},
    ])
    placed, _, suspicious = AITranslator._parse_tool_response(msg, items)
    assert placed == {1: '短'}
    assert '长度' in suspicious[0]


# ---- translator 层：解析不完整自动重试 + 按 id 合并 ----

def test_translator_retries_on_mismatch(translator, monkeypatch):
    """第一次句数不匹配、第二次正常：重试发生且合并为完整结果。
    重试只发失败条目（子集 id 重编），已成功的第 1 句保留首轮译文"""
    calls = []

    def fake_call_api(messages, temperature, max_tokens, tools=None,
                      tool_choice=None, return_message=False, task_type=''):
        calls.append(1)
        if len(calls) == 1:
            return _fake_message([{'id': 1, 'translation': '只有一句'}])
        # 第 2 轮只重发失败的 2 条（原批索引 1、2 → 子集 id 1、2）
        return _ok_message(2)

    monkeypatch.setattr(translator, '_call_api', fake_call_api)
    merged, terms, fail_reasons = translator.translate_batch(
        _items(), content_type='dialogue')

    assert len(calls) == 2
    assert merged == {0: '只有一句', 1: '译文1', 2: '译文2'}
    assert fail_reasons == {}


def test_translator_merges_across_attempts(translator, monkeypatch):
    """多次尝试各译出一部分：失败子集逐轮补齐合并"""
    responses = [
        _fake_message([{'id': 1, 'translation': '第一句'},
                       {'id': 2, 'translation': '第二句'}]),
        # 第 2 轮子集只剩原批索引 2（子集 id 1）
        _fake_message([{'id': 1, 'translation': '第三句'}]),
    ]
    calls = []

    def fake_call_api(messages, temperature, max_tokens, tools=None,
                      tool_choice=None, return_message=False, task_type=''):
        calls.append(1)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(translator, '_call_api', fake_call_api)
    merged, _, fail_reasons = translator.translate_batch(
        _items(), content_type='dialogue')

    assert merged == {0: '第一句', 1: '第二句', 2: '第三句'}
    assert fail_reasons == {}


def test_translator_gives_up_after_max_retries(translator, monkeypatch):
    """重试耗尽仍缺句：缺失条目带原因返回（不再返回 None）"""
    calls = []

    def always_bad(messages, temperature, max_tokens, tools=None,
                   tool_choice=None, return_message=False, task_type=''):
        calls.append(1)
        if len(calls) == 1:
            return _fake_message([{'id': 1, 'translation': '只有一句'}])
        return _fake_message([])  # 重试轮模型仍不交卷

    monkeypatch.setattr(translator, '_call_api', always_bad)
    merged, _, fail_reasons = translator.translate_batch(
        _items(), content_type='dialogue')

    assert len(calls) == AITranslator.MAX_RETRIES
    assert merged == {0: '只有一句'}
    assert set(fail_reasons) == {1, 2}
    assert all('未返回' in r for r in fail_reasons.values())


# ---- service 层：整批异常不中断任务 ----

async def test_service_whole_batch_failure_stashed(translator, db, monkeypatch):
    """整批 API 异常：整批暂存、返回空结果让后续批次继续（不抛）"""
    def boom(*a, **k):
        raise RuntimeError('网络断开')

    monkeypatch.setattr(translator, 'translate_batch', boom)
    svc = TranslationService(translator, db, TranslationLogger())
    items = db.get_all_dialogues()

    results = await svc.translate_batch(items, 'dialogue')

    assert results == {}
    batches = db.list_failed_batches('dialogue')
    assert len(batches) == 1
    assert len(batches[0]['items']) == len(items)
    assert all('整批异常' in it['reason'] for it in batches[0]['items'])


async def test_service_whole_batch_no_stash_reraises(translator, db, monkeypatch):
    """stash_on_failure=False（失败条目重试任务自己管）：仍上抛"""
    def boom(*a, **k):
        raise RuntimeError('网络断开')

    monkeypatch.setattr(translator, 'translate_batch', boom)
    svc = TranslationService(translator, db, TranslationLogger())

    with pytest.raises(RuntimeError):
        await svc.translate_batch(db.get_all_dialogues(), 'dialogue',
                                  stash_on_failure=False)


async def test_service_fatal_api_error_reraises(translator, db, monkeypatch):
    """配置类致命错误（key 无效/余额耗尽）仍上抛中止——继续跑每批都会失败"""
    from llm_client import FatalAPIError

    def fatal(*a, **k):
        raise FatalAPIError(401, 'unauthorized')

    monkeypatch.setattr(translator, 'translate_batch', fatal)
    svc = TranslationService(translator, db, TranslationLogger())

    with pytest.raises(FatalAPIError):
        await svc.translate_batch(db.get_all_dialogues(), 'dialogue')


# ---- service 层：匹配落库 + 未译出暂存 ----

async def test_service_batch_retry_then_persisted(translator, db, monkeypatch):
    """首次解析不完整、重试成功：断言重试发生、结果返回且写入 SQLite"""
    calls = []
    items = db.get_all_dialogues()

    def fake_call_api(messages, temperature, max_tokens, tools=None,
                      tool_choice=None, return_message=False, task_type=''):
        calls.append(1)
        if len(calls) == 1:
            # 句数不匹配：少两句
            return _fake_message([{'id': 1, 'translation': '你好'}])
        # 重试只发失败的 2 条（子集 id 重编）
        return _ok_message(len(items) - 1)

    monkeypatch.setattr(translator, '_call_api', fake_call_api)
    svc = TranslationService(translator, db, TranslationLogger())

    results = await svc.translate_batch(items, 'dialogue')

    assert len(calls) == 2  # 重试确实发生
    assert len(results) == len(items)
    # 落库校验：首条保留首轮译文，其余两条来自重试
    saved = {r['id']: r for r in db.get_all_dialogues()}
    expected = ['你好', '译文1', '译文2']
    for i, it in enumerate(items):
        row = saved[it['id']]
        assert row['translated_text'] == expected[i]
        assert row['is_translated']
        assert results[it['id']] == expected[i]
    # 全部译出，无暂存
    assert db.count_failed_batches('dialogue') == 0


async def test_service_batch_partial_saved_and_stashed(translator, db, monkeypatch):
    """重试耗尽仍缺句：匹配的落库不受阻，未译出的逐条暂存（任务不中断）"""
    calls = []

    def always_bad(messages, temperature, max_tokens, tools=None,
                   tool_choice=None, return_message=False, task_type=''):
        calls.append(1)
        if len(calls) == 1:
            return _fake_message([{'id': 1, 'translation': '只有一句'}])
        return _fake_message([])  # 重试轮模型仍不交卷

    monkeypatch.setattr(translator, '_call_api', always_bad)
    svc = TranslationService(translator, db, TranslationLogger())
    items = db.get_all_dialogues()

    results = await svc.translate_batch(items, 'dialogue')

    # 匹配的照常落库
    assert len(results) == 1
    rows = {r['id']: r for r in db.get_all_dialogues()}
    assert rows[items[0]['id']]['translated_text'] == '只有一句'
    assert not rows[items[1]['id']]['is_translated']
    assert not rows[items[2]['id']]['is_translated']
    # 未译出的两条暂存，带原因
    batches = db.list_failed_batches('dialogue')
    assert len(batches) == 1
    stashed = batches[0]['items']
    assert [it['id'] for it in stashed] == [items[1]['id'], items[2]['id']]
    assert all(it['reason'] for it in stashed)


async def test_service_batch_parse_failure_no_stash(translator, db, monkeypatch):
    """stash_on_failure=False（失败条目重试任务）：只返回结果，不入暂存"""
    calls = []

    def always_bad(messages, temperature, max_tokens, tools=None,
                   tool_choice=None, return_message=False, task_type=''):
        calls.append(1)
        if len(calls) == 1:
            return _fake_message([{'id': 1, 'translation': '只有一句'}])
        return _fake_message([])  # 重试轮模型仍不交卷

    monkeypatch.setattr(translator, '_call_api', always_bad)
    svc = TranslationService(translator, db, TranslationLogger())

    results = await svc.translate_batch(
        db.get_all_dialogues(), 'dialogue', stash_on_failure=False)

    assert len(results) == 1
    assert db.count_failed_batches('dialogue') == 0


async def test_failed_batch_retry_flow(translator, db, monkeypatch):
    """暂存 → 筛出未译条目 → 小批重试成功 → 清掉暂存记录"""
    svc = TranslationService(translator, db, TranslationLogger())
    items = db.get_all_dialogues()
    db.add_failed_batch('dialogue', items, '批次内 3/3 条未译出')

    # 先手动译出第一条：filter_untranslated_items 应把它剔除
    db.update_dialogue(items[0]['id'], '手动译文')
    remaining = db.filter_untranslated_items('dialogue',
                                             db.list_failed_batches('dialogue')[0]['items'])
    assert [it['id'] for it in remaining] == [items[1]['id'], items[2]['id']]

    def ok_call(messages, temperature, max_tokens, tools=None,
                tool_choice=None, return_message=False, task_type=''):
        return _ok_message(len(remaining))

    monkeypatch.setattr(translator, '_call_api', ok_call)
    results = await svc.translate_batch(remaining, 'dialogue',
                                        stash_on_failure=False)
    assert len(results) == 2

    # 模拟重试任务收尾：无失败 → 删除暂存
    failed = [it for it in remaining if it['id'] not in set(results)]
    assert not failed
    batch_id = db.list_failed_batches('dialogue')[0]['id']
    db.delete_failed_batch(batch_id)
    assert db.count_failed_batches('dialogue') == 0
    assert all(r['is_translated'] for r in db.get_all_dialogues())
