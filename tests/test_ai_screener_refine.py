"""AI 精审：判决未覆盖整批时追问补齐而非抛错"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from ai_screener import AIScreener  # noqa: E402


def _tool_msg(name, arguments, call_id='call_1'):
    tc = SimpleNamespace(id=call_id,
                         function=SimpleNamespace(name=name,
                                                  arguments=arguments))
    return SimpleNamespace(tool_calls=[tc], content=None)


def _text_msg(content):
    return SimpleNamespace(tool_calls=None, content=content)


def _verdicts(*ids):
    return json.dumps({'verdicts': [
        {'id': i, 'keep': True, 'reason': f'r{i}'} for i in ids]})


class FakeTranslator:
    """按脚本逐轮返回消息"""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.rounds = 0
        self.config = SimpleNamespace(temperature=0.2, max_tokens=2000)

    def chat_completion(self, messages, **kwargs):
        self.rounds += 1
        assert self.scripted, '脚本外的额外调用'
        return self.scripted.pop(0)


def _mk_screener(translator, tmp_path):
    s = object.__new__(AIScreener)
    s.translator = translator
    s.logger = None
    s._cancel_event = None
    s.game_sub = tmp_path  # 工具执行兜底用，本测试不触发
    return s


def _batch(n):
    cands = []
    for i in range(n):
        cands.append(SimpleNamespace(text=f't{i}', hint='h', kind='python',
                                     rel_file='a.rpy', line=i + 1,
                                     static_reason='', static_danger=False))
    return list(enumerate(cands))


class TestRefineBatchPartialVerdicts:
    def test_partial_then_complete(self, tmp_path):
        """首轮缺 1 条 → 追问 → 次轮补齐，两轮内返回全集"""
        tr = FakeTranslator([
            _tool_msg('submit_verdicts', _verdicts(0, 1)),
            _tool_msg('submit_verdicts', _verdicts(2), 'call_2'),
        ])
        s = _mk_screener(tr, tmp_path)
        result = s._refine_batch(_batch(3))
        assert sorted(result) == [0, 1, 2]
        assert result[2] == (True, 'r2')
        assert tr.rounds == 2

    def test_broken_json_then_complete(self, tmp_path):
        """submit_verdicts 参数损坏 → 追问而非抛错"""
        tr = FakeTranslator([
            _tool_msg('submit_verdicts', '{bad json'),
            _tool_msg('submit_verdicts', _verdicts(0, 1), 'call_2'),
        ])
        s = _mk_screener(tr, tmp_path)
        result = s._refine_batch(_batch(2))
        assert sorted(result) == [0, 1]

    def test_partial_text_then_complete(self, tmp_path):
        """无 tool call 的文本判决也允许部分覆盖"""
        tr = FakeTranslator([
            _text_msg(json.dumps([{'id': 0, 'keep': False, 'reason': 'x'}])),
            _tool_msg('submit_verdicts', _verdicts(1), 'call_2'),
        ])
        s = _mk_screener(tr, tmp_path)
        result = s._refine_batch(_batch(2))
        assert result[0] == (False, 'x')
        assert result[1] == (True, 'r1')

    def test_all_rounds_incomplete_raises(self, tmp_path):
        """追问到轮数上限仍缺 → 响亮失败（不静默降级）"""
        s = _mk_screener(FakeTranslator([]), tmp_path)
        s.MAX_ROUNDS = 2
        # 每轮都只交 id 0；轮数上限后强制文本轮仍只交 id 0
        s.translator = FakeTranslator([
            _tool_msg('submit_verdicts', _verdicts(0), 'c1'),
            _tool_msg('submit_verdicts', _verdicts(0), 'c2'),
            _text_msg(json.dumps([{'id': 0, 'keep': True, 'reason': 'x'}])),
        ])
        with pytest.raises(RuntimeError, match='仍有 1 条'):
            s._refine_batch(_batch(2))

    def test_forced_final_round_merges(self, tmp_path):
        """轮数上限后的强制文本轮可以补齐缺口"""
        s = _mk_screener(FakeTranslator([]), tmp_path)
        s.MAX_ROUNDS = 1
        s.translator = FakeTranslator([
            _tool_msg('submit_verdicts', _verdicts(0), 'c1'),
            _text_msg(json.dumps([{'id': 1, 'keep': True, 'reason': 'y'}])),
        ])
        result = s._refine_batch(_batch(2))
        assert sorted(result) == [0, 1]
