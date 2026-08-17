"""LLMClient 错误码分类测试（不打网络）

用 stub 替换 OpenAI client 的 chat.completions.create，抛出各类
openai 异常，验证 llm_client.chat_completion 的分类行为：

- 400/401/402/403/404 → 立即抛 FatalAPIError（不重试，批量任务应中止）
- 421/429/5xx → 指数退避重试 MAX_RETRIES 次后抛普通异常
- 超时 / 连接错误 → 同上按可重试处理
"""
import httpx
import openai
import pytest

from llm_client import LLMClient, FatalAPIError
from translator import TranslationConfig


def _status_error(code: int):
    req = httpx.Request('POST', 'https://api.test/v1/chat/completions')
    return openai.APIStatusError(
        f'HTTP {code}', response=httpx.Response(code, request=req), body=None)


class _StubCompletions:
    """按预设异常/返回值序列响应 create() 调用"""

    def __init__(self, outcomes: list):
        self.outcomes = outcomes
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _make_client(monkeypatch, outcomes: list) -> tuple[LLMClient, _StubCompletions]:
    llm = LLMClient(TranslationConfig(api_key='offline-test-key'))
    stub = _StubCompletions(outcomes)
    llm.client = type('C', (), {'chat': type('X', (), {'completions': stub})()})()
    monkeypatch.setattr('time.sleep', lambda s: None)  # 跳过退避等待
    return llm, stub


@pytest.mark.parametrize('code', [400, 401, 402, 403, 404])
def test_fatal_codes_raise_immediately(monkeypatch, code):
    """致命错误码：第一次调用就抛 FatalAPIError，带状态码与中文说明"""
    llm, stub = _make_client(monkeypatch, [_status_error(code)])
    with pytest.raises(FatalAPIError) as exc:
        llm.chat_completion(messages=[{'role': 'user', 'content': 'hi'}])
    assert exc.value.status_code == code
    assert stub.calls == 1  # 不重试


@pytest.mark.parametrize('code', [421, 429, 500, 502, 503, 504])
def test_retryable_codes_exhaust_then_raise(monkeypatch, code):
    """可重试错误码：重试 MAX_RETRIES 次后抛普通异常（非 FatalAPIError）"""
    llm, stub = _make_client(monkeypatch, [_status_error(code)])
    with pytest.raises(Exception) as exc:
        llm.chat_completion(messages=[{'role': 'user', 'content': 'hi'}])
    assert not isinstance(exc.value, FatalAPIError)
    assert stub.calls == LLMClient.MAX_RETRIES


def test_retryable_code_recovers_on_retry(monkeypatch):
    """429 后重试成功：返回内容"""
    req = httpx.Request('POST', 'https://api.test/v1/chat/completions')
    ok = type('R', (), {
        'choices': [type('Ch', (), {
            'message': type('M', (), {'content': ' 你好 '})()})()],
        'model_dump': lambda self: {},
    })()
    llm, stub = _make_client(monkeypatch, [_status_error(429), ok])
    assert llm.chat_completion(messages=[{'role': 'user', 'content': 'hi'}]) == '你好'
    assert stub.calls == 2


def test_timeout_and_connection_are_retryable(monkeypatch):
    req = httpx.Request('POST', 'https://api.test/v1/chat/completions')
    for exc in (openai.APITimeoutError(request=req),
                openai.APIConnectionError(message='boom', request=req)):
        llm, stub = _make_client(monkeypatch, [exc])
        with pytest.raises(Exception) as e:
            llm.chat_completion(messages=[{'role': 'user', 'content': 'hi'}])
        assert not isinstance(e.value, FatalAPIError)
        assert stub.calls == LLMClient.MAX_RETRIES


def test_unknown_status_code_is_retryable(monkeypatch):
    """未列出的状态码（如 422）不误判为致命，按可重试处理"""
    llm, stub = _make_client(monkeypatch, [_status_error(422)])
    with pytest.raises(Exception) as exc:
        llm.chat_completion(messages=[{'role': 'user', 'content': 'hi'}])
    assert not isinstance(exc.value, FatalAPIError)
    assert stub.calls == LLMClient.MAX_RETRIES


def test_empty_content_retried(monkeypatch):
    """内容审核拦截（空 content）：按可重试处理"""
    empty = type('R', (), {
        'choices': [type('Ch', (), {
            'message': type('M', (), {'content': None})()})()],
        'model_dump': lambda self: {},
    })()
    llm, stub = _make_client(monkeypatch, [empty])
    with pytest.raises(Exception, match='内容审核拦截'):
        llm.chat_completion(messages=[{'role': 'user', 'content': 'hi'}])
    assert stub.calls == LLMClient.MAX_RETRIES
