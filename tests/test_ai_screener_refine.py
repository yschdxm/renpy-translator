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
        self.temps = []          # chat_completion 收到的温度（判定应固定 0）
        self.analyze_temps = []  # analyze_text 收到的温度
        self.analyze_reply = '[]'

    def chat_completion(self, messages, **kwargs):
        self.rounds += 1
        self.temps.append(kwargs.get('temperature'))
        assert self.scripted, '脚本外的额外调用'
        return self.scripted.pop(0)

    def analyze_text(self, prompt, max_tokens=None, temperature=None):
        self.analyze_temps.append(temperature)
        return self.analyze_reply


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


class TestDeterministicTemperature:
    """判定任务固定 0 温度：同输入同判定，全部重判不再随机翻转

    背景：粗筛/精审曾沿用用户配置的翻译温度（0.3+），重判同一候选
    两轮结果不同（0↔1 翻转），用户看到"再筛一遍又出新的可翻译内容"。
    """

    def test_refine_uses_zero_temperature(self, tmp_path):
        tr = FakeTranslator([_tool_msg('submit_verdicts', _verdicts(0))])
        s = _mk_screener(tr, tmp_path)
        s._refine_batch(_batch(1))
        assert tr.temps == [0]

    def test_coarse_uses_zero_temperature(self, tmp_path):
        tr = FakeTranslator([])
        tr.analyze_reply = json.dumps(
            [{'id': 0, 'keep': True, 'confident': True, 'reason': '界面文本'}],
            ensure_ascii=False)
        s = _mk_screener(tr, tmp_path)
        verdicts = s._coarse_batch(
            [{'id': 0, 'text': 'Settings', 'hint': '界面',
              'kind': 'screen', 'rel_file': 'a.rpy', 'line': 1}])
        assert verdicts[0] == (True, True, '界面文本')
        assert tr.analyze_temps == [0]


# ---- 整批失败不中断：跳过 → 收尾重试 → 仍败保持未决 ----

class FakeTree:
    def lines(self, rel):
        return ['x = 1', 'y = 2']


def _cands(n):
    return [SimpleNamespace(
        text=f't{i}', hint='h', kind='python', rel_file='a.rpy',
        line=i + 1, file='a.rpy', static_reason='', static_danger=False,
        ai_keep=None, ai_confident=False, ai_reason='') for i in range(n)]


def _progress():
    return {'phase': '', 'done': 0, 'total': 0, 'finished': False}


def _screener_for_screen(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    s = _mk_screener(FakeTranslator([]), tmp_path)
    s._pool = ThreadPoolExecutor(max_workers=2)
    s._tree = FakeTree()
    s.REFINE_BATCH = 2
    s.COARSE_BATCH = 2
    return s


class TestBatchFailureNotFatal:
    def test_refine_retry_succeeds(self, tmp_path):
        """首批抛异常不中断：其余批次照跑，收尾重试成功则全部判定"""
        s = _screener_for_screen(tmp_path)
        calls = {'n': 0}

        def fake_refine(batch, progress=None):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('10 轮追问后仍未获判决')
            return {idx: (True, f'r{idx}') for idx, _ in batch}

        s._refine_batch = fake_refine
        cands = _cands(4)
        s._refine_screen(cands, _progress())
        assert all(c.ai_keep is True for c in cands)
        assert calls['n'] == 3  # 2 批 + 1 次收尾重试

    def test_refine_retry_fails_keeps_pending(self, tmp_path):
        """重试仍失败：候选保持未决，原因带失败摘要，不抛"""
        s = _screener_for_screen(tmp_path)

        def always_fail(batch, progress=None):
            raise RuntimeError("AI 最后输出: '我不会判'")

        s._refine_batch = always_fail
        cands = _cands(4)
        s._refine_screen(cands, _progress())  # 不抛
        assert all(c.ai_keep is None for c in cands)
        assert all('精审失败' in c.ai_reason for c in cands)
        assert all(c.ai_confident is False for c in cands)

    def test_coarse_retry_fails_keeps_pending(self, tmp_path):
        """粗筛批失败同样跳过+收尾重试+保持未决"""
        s = _screener_for_screen(tmp_path)

        def always_fail(batch):
            raise ValueError('粗筛返回无法解析: ...')

        s._coarse_batch = always_fail
        cands = _cands(4)
        s._coarse_screen(cands, _progress())  # 不抛
        assert all(c.ai_keep is None for c in cands)
        assert all('粗筛失败' in c.ai_reason for c in cands)

    def test_coarse_retry_succeeds(self, tmp_path):
        s = _screener_for_screen(tmp_path)
        calls = {'n': 0}

        def fake_coarse(batch):
            calls['n'] += 1
            if calls['n'] == 1:
                raise ValueError('bad json')
            return {it['id']: (True, True, '界面文本') for it in batch}

        s._coarse_batch = fake_coarse
        cands = _cands(4)
        s._coarse_screen(cands, _progress())
        assert all(c.ai_keep is True for c in cands)
        assert calls['n'] == 3

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
