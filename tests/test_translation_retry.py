"""批次翻译「句数不匹配自动重试」测试

不打网络：用 fake message 喂给真实的 AITranslator.translate_batch
（_call_api 打桩），再经 TranslationService 落真实 SQLite（tmp_path）。

重试发生在 AITranslator.translate_batch 内部（解析失败重试 MAX_RETRIES 次）；
TranslationService 拿到 None 则整批失败抛 RuntimeError。
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

def test_parse_mismatch_returns_none():
    """句数不足 → (None, [])，触发重试"""
    msg = _fake_message([{'id': 1, 'translation': '你好'}])
    assert AITranslator._parse_tool_response(msg, 2) == (None, [])


def test_parse_out_of_order_aligned_by_id():
    """模型乱序返回时按 id 对齐"""
    msg = _fake_message([
        {'id': 2, 'translation': '二'},
        {'id': 1, 'translation': '一'},
    ])
    result, terms = AITranslator._parse_tool_response(msg, 2)
    assert result == ['一', '二']
    assert terms == []


def test_parse_empty_translation_rejected():
    """空译文不能静默通过（否则该条永远漏译）"""
    msg = _fake_message([
        {'id': 1, 'translation': '一'},
        {'id': 2, 'translation': ''},
    ])
    assert AITranslator._parse_tool_response(msg, 2) == (None, [])


# ---- translator 层：解析失败自动重试 ----

def test_translator_retries_on_mismatch(translator, monkeypatch):
    """第一次句数不匹配、第二次正常：重试发生且返回对齐译文"""
    calls = []

    def fake_call_api(messages, temperature, max_tokens, tools=None,
                      tool_choice=None, return_message=False, task_type=''):
        calls.append(1)
        if len(calls) == 1:
            return _fake_message([{'id': 1, 'translation': '只有一句'}])
        return _ok_message(3)

    monkeypatch.setattr(translator, '_call_api', fake_call_api)
    items = [{'original_text': d['original_text'], 'character': d['character']}
             for d in DIALOGUES]
    translated, terms = translator.translate_batch(items, content_type='dialogue')

    assert len(calls) == 2
    assert translated == ['译文1', '译文2', '译文3']


def test_translator_gives_up_after_max_retries(translator, monkeypatch):
    """重试耗尽仍不匹配 → (None, [])，由上层整批记失败"""
    calls = []

    def always_bad(messages, temperature, max_tokens, tools=None,
                   tool_choice=None, return_message=False, task_type=''):
        calls.append(1)
        return _fake_message([{'id': 1, 'translation': '只有一句'}])

    monkeypatch.setattr(translator, '_call_api', always_bad)
    items = [{'original_text': d['original_text'], 'character': d['character']}
             for d in DIALOGUES]
    translated, _ = translator.translate_batch(items, content_type='dialogue')

    assert len(calls) == AITranslator.MAX_RETRIES
    assert translated is None


# ---- service 层：重试后最终落库 ----

async def test_service_batch_retry_then_persisted(translator, db, monkeypatch):
    """首次解析失败重试成功：断言重试发生、结果返回且写入 SQLite"""
    calls = []
    items = db.get_all_dialogues()

    def fake_call_api(messages, temperature, max_tokens, tools=None,
                      tool_choice=None, return_message=False, task_type=''):
        calls.append(1)
        if len(calls) == 1:
            # 句数不匹配：少一句
            return _fake_message([{'id': 1, 'translation': '你好'}])
        return _ok_message(len(items))

    monkeypatch.setattr(translator, '_call_api', fake_call_api)
    svc = TranslationService(translator, db, TranslationLogger())

    results = await svc.translate_batch(items, 'dialogue')

    assert len(calls) == 2  # 重试确实发生
    assert len(results) == len(items)
    # 落库校验
    saved = {r['id']: r for r in db.get_all_dialogues()}
    for i, it in enumerate(items):
        row = saved[it['id']]
        assert row['translated_text'] == f'译文{i + 1}'
        assert row['is_translated']
        assert results[it['id']] == f'译文{i + 1}'


async def test_service_batch_parse_failure_raises(translator, db, monkeypatch):
    """重试耗尽：service 抛 RuntimeError（整批失败，由面板中断任务）"""

    def always_bad(messages, temperature, max_tokens, tools=None,
                   tool_choice=None, return_message=False, task_type=''):
        return _fake_message([{'id': 1, 'translation': '只有一句'}])

    monkeypatch.setattr(translator, '_call_api', always_bad)
    svc = TranslationService(translator, db, TranslationLogger())

    with pytest.raises(RuntimeError, match='句数不匹配'):
        await svc.translate_batch(db.get_all_dialogues(), 'dialogue')

    # 全部未落库
    assert all(not r['is_translated'] for r in db.get_all_dialogues())
