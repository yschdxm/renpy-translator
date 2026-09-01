"""级联判定测试：自适应多票粗筛 + 精审双跑决胜 + 锚定复核

契约：
- 粗筛：前两票（确定性洗牌 + 措辞变体，temperature=0）一致即定案，
  第三票只在固定模式（COARSE_ALWAYS_3）投——自适应模式下前两票分裂
  三票必是 2:1（照样升级），第三票改变不了结局
- 计票：两票一致 / 三票全一致 → 定案；分裂或票不足 → 升级精审
- keep × static_danger 的方向强制升级精审（危险方向要证据核实）
- 精审双跑：每批独立两次，一致定案；不一致第三跑决胜（三票多数）；
  极端 1:1 保持未决不猜；全败保持未决
- 锚定（灰区复核）：条目带 prior_verdict/prior_reason，system 含维持原判段
"""
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from ai_screener import AIScreener  # noqa: E402


def _tool_msg(name, arguments, call_id='call_1'):
    tc = SimpleNamespace(id=call_id,
                         function=SimpleNamespace(name=name,
                                                  arguments=arguments))
    return SimpleNamespace(tool_calls=[tc], content=None)


def _text_msg(content):
    return SimpleNamespace(tool_calls=None, content=content)


class FakeTranslator:
    """analyze_text 按调用次序返回 analyze_replies（空则 analyze_reply）"""

    def __init__(self, scripted=()):
        self.scripted = list(scripted)
        self.config = SimpleNamespace(temperature=0.2, max_tokens=2000)
        self.analyze_reply = '[]'
        self.analyze_replies = []
        self.analyze_prompts = []   # 收到的完整 prompt（洗牌/变体断言用）
        self.last_messages = None   # 最后一次 chat_completion 的 messages

    def chat_completion(self, messages, **kwargs):
        self.last_messages = messages
        assert self.scripted, '脚本外的额外调用'
        return self.scripted.pop(0)

    def analyze_text(self, prompt, max_tokens=None, temperature=None):
        self.analyze_prompts.append(prompt)
        if self.analyze_replies:
            return self.analyze_replies.pop(0)
        return self.analyze_reply


class FakeTree:
    def lines(self, rel):
        return ['x = 1', 'y = 2']


def _mk_screener(translator, tmp_path):
    s = object.__new__(AIScreener)
    s.translator = translator
    s.logger = None
    s._cancel_event = None
    s.game_sub = tmp_path
    s._pool = ThreadPoolExecutor(max_workers=2)
    s._tree = FakeTree()
    s.COARSE_BATCH = 10
    s.REFINE_BATCH = 10
    return s


def _cands(n, danger=False, fragment=False):
    return [SimpleNamespace(
        text=f't{i}', hint='h', kind='python', rel_file='a.rpy',
        line=i + 1, file='a.rpy', static_reason='', static_danger=danger,
        static_fragment=fragment,
        ai_keep=None, ai_confident=False, ai_reason='') for i in range(n)]


def _progress():
    return {'phase': '', 'done': 0, 'total': 0, 'finished': False}


def _reply(*id_keep):
    return json.dumps([{'id': i, 'keep': k, 'reason': f'r{i}'}
                       for i, k in id_keep], ensure_ascii=False)


# ---- 粗筛：自适应多票 ----

class TestCoarseCascade:
    def test_two_votes_agree_decided(self, tmp_path):
        """前两票一致 → 定案，不投第三票"""
        tr = FakeTranslator()
        tr.analyze_reply = _reply((0, True), (1, True))
        s = _mk_screener(tr, tmp_path)
        cands = _cands(2)
        s._coarse_screen(cands, _progress())
        assert len(tr.analyze_prompts) == 2
        assert all(c.ai_keep is True and c.ai_confident for c in cands)

    def test_two_votes_split_escalates(self, tmp_path):
        """前两票分裂 → 升级精审（ai_confident=False），不投第三票"""
        tr = FakeTranslator()
        tr.analyze_replies = [_reply((0, True)), _reply((0, False))]
        s = _mk_screener(tr, tmp_path)
        cands = _cands(1)
        s._coarse_screen(cands, _progress())
        assert len(tr.analyze_prompts) == 2  # 第三票改变不了 2:1 结局，不投
        assert cands[0].ai_confident is False
        assert cands[0].ai_keep is None

    def test_missing_id_treated_as_split(self, tmp_path):
        """某票漏了条目 → 票不足按分裂升级"""
        tr = FakeTranslator()
        tr.analyze_replies = [_reply((0, True)), '[]']
        s = _mk_screener(tr, tmp_path)
        cands = _cands(1)
        s._coarse_screen(cands, _progress())
        assert cands[0].ai_confident is False

    def test_keep_with_danger_escalates(self, tmp_path):
        """票决 keep × 危险 × 拼接（wrap 路径）→ 强制升级精审核实；
        无拼接的 table 路径 keep 即使带 danger 也直接定案（逻辑仍用原文，
        误判几乎无害，不值得花精审的钱）"""
        tr = FakeTranslator()
        tr.analyze_reply = _reply((0, True), (1, True))
        s = _mk_screener(tr, tmp_path)
        cands = _cands(2, danger=True)
        cands[1].static_fragment = True   # 0 号 table 路径，1 号 wrap 路径
        s._coarse_screen(cands, _progress())
        assert cands[0].ai_keep is True and cands[0].ai_confident is True
        assert cands[1].ai_keep is True and cands[1].ai_confident is False

    def test_shuffle_deterministic_per_vote(self, tmp_path):
        """批内顺序按票种子确定性洗牌：同种子同序，与 random.Random 一致"""
        tr = FakeTranslator()
        tr.analyze_reply = _reply(*[(i, True) for i in range(8)])
        s = _mk_screener(tr, tmp_path)
        cands = _cands(8)
        s._coarse_screen(cands, _progress())
        assert len(tr.analyze_prompts) == 2
        orders = []
        for prompt in tr.analyze_prompts:
            items = json.loads(prompt[prompt.index('[{'):prompt.rindex('}]') + 2])
            orders.append([it['id'] for it in items])
        expected0 = list(range(8))
        random.Random(0).shuffle(expected0)
        expected1 = list(range(8))
        random.Random(1).shuffle(expected1)
        assert orders[0] == expected0
        assert orders[1] == expected1

    def test_prompt_variants_differ(self, tmp_path):
        """两票用不同措辞变体（票间去相关来自输入差异）"""
        tr = FakeTranslator()
        tr.analyze_reply = _reply((0, True))
        s = _mk_screener(tr, tmp_path)
        s._coarse_screen(_cands(1), _progress())
        assert tr.analyze_prompts[0] != tr.analyze_prompts[1]

    def test_always_3_mode(self, tmp_path):
        """固定三票模式：全投三票；三票全一致定案，2:1 升级"""
        tr = FakeTranslator()
        tr.analyze_replies = [
            _reply((0, True), (1, True)),
            _reply((0, True), (1, True)),
            _reply((0, True), (1, False)),  # 0 号 3:0 定案，1 号 2:1 升级
        ]
        s = _mk_screener(tr, tmp_path)
        s.COARSE_ALWAYS_3 = True
        cands = _cands(2)
        s._coarse_screen(cands, _progress())
        assert len(tr.analyze_prompts) == 3
        assert cands[0].ai_keep is True and cands[0].ai_confident is True
        assert cands[1].ai_confident is False

    def test_batch_failure_retry(self, tmp_path):
        """单批失败收尾统一重试：重试成功该票有效，仍败该票留空按分裂"""
        tr = FakeTranslator()
        s = _mk_screener(tr, tmp_path)
        calls = {'n': 0}

        def flaky(batch, vote_idx):
            calls['n'] += 1
            if calls['n'] == 1:
                raise ValueError('bad json')
            return {it['id']: (True, 'ok') for it in batch}

        s._coarse_vote = flaky
        cands = _cands(2)
        s._coarse_screen(cands, _progress())
        assert all(c.ai_keep is True for c in cands)
        assert calls['n'] == 3  # 首票首调失败 + 重试 + 第二票


# ---- 精审：双跑决胜 ----

class TestRefineDoubleRun:
    def test_double_run_agree_decided(self, tmp_path):
        """双跑一致 → 定案，无决胜跑；evidence/apply 写回候选"""
        s = _mk_screener(FakeTranslator(), tmp_path)
        calls = []

        def fake_refine(batch, progress=None, anchored=None):
            calls.append(1)
            return {idx: (True, f'r{idx}', 'a.rpy:1', 'wrap')
                    for idx, _ in batch}

        s._refine_batch = fake_refine
        cands = _cands(2)
        s._refine_screen(cands, _progress())
        assert len(calls) == 2
        assert all(c.ai_keep is True and c.ai_confident for c in cands)
        assert all(c.ai_evidence == 'a.rpy:1' for c in cands)
        assert all(c.ai_apply == 'wrap' for c in cands)

    def test_double_run_disagree_tiebreak(self, tmp_path):
        """双跑不一致 → 第三跑决胜（三票多数）"""
        s = _mk_screener(FakeTranslator(), tmp_path)
        calls = {'n': 0}

        def fake_refine(batch, progress=None, anchored=None):
            calls['n'] += 1
            keep = calls['n'] != 1  # 跑1=drop，跑2/3=keep → 2:1 keep
            return {idx: (keep, f'r{calls["n"]}', '', '')
                    for idx, _ in batch}

        s._refine_batch = fake_refine
        cands = _cands(2)
        s._refine_screen(cands, _progress())
        assert calls['n'] == 3
        assert all(c.ai_keep is True and c.ai_confident for c in cands)

    def test_exact_tie_keeps_pending(self, tmp_path):
        """极端 1:1（一跑缺席 + 决胜反向）→ 不猜，保持未决"""
        s = _mk_screener(FakeTranslator(), tmp_path)
        calls = {'n': 0}

        def fake_refine(batch, progress=None, anchored=None):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('网络断开')
            keep = calls['n'] == 2  # 跑1失败，跑2=keep，决胜=drop → 1:1
            return {idx: (keep, 'r', '', '') for idx, _ in batch}

        s._refine_batch = fake_refine
        cands = _cands(1)
        s._refine_screen(cands, _progress())
        assert cands[0].ai_keep is None
        assert '分歧' in cands[0].ai_reason

    def test_all_runs_fail_keeps_pending(self, tmp_path):
        """三跑全败 → 保持未决并写明原因"""
        s = _mk_screener(FakeTranslator(), tmp_path)

        def always_fail(batch, progress=None, anchored=None):
            raise RuntimeError('网络断开')

        s._refine_batch = always_fail
        cands = _cands(2)
        s._refine_screen(cands, _progress())
        assert all(c.ai_keep is None for c in cands)
        assert all('精审失败' in c.ai_reason for c in cands)


# ---- 锚定复核 ----

class TestAnchoredRefine:
    def test_anchored_prompt(self, tmp_path):
        """anchored 模式：条目带 prior 字段，system 含维持原判段"""
        tr = FakeTranslator([
            _tool_msg('submit_verdicts', json.dumps({'verdicts': [
                {'id': 0, 'keep': False, 'reason': '维持',
                 'evidence': 'a.rpy:2'}]})),
        ])
        s = _mk_screener(tr, tmp_path)
        cands = _cands(1)
        result = s._refine_batch(
            list(enumerate(cands)),
            anchored={0: {'keep': False, 'reason': '上轮是键名'}})
        system_msg = tr.last_messages[0]['content']
        user_msg = tr.last_messages[1]['content']
        assert '维持原判' in system_msg
        assert 'prior_verdict' in user_msg and '上轮是键名' in user_msg
        assert result[0] == (False, '维持', 'a.rpy:2', '')

    def test_anchored_screen_targets_all(self, tmp_path):
        """anchored 模式下全部候选都进精审（不看 ai_confident）"""
        s = _mk_screener(FakeTranslator(), tmp_path)

        def fake_refine(batch, progress=None, anchored=None):
            return {idx: (True, 'r', '', '') for idx, _ in batch}

        s._refine_batch = fake_refine
        cands = _cands(2)
        for c in cands:
            c.ai_confident = True  # 已决行在复核模式下也要重审
        s._refine_screen(cands, _progress(),
                         anchored={i: {'keep': True, 'reason': 'x'}
                                   for i in range(2)})
        assert all(c.ai_keep is True for c in cands)

    def test_anchored_exhausted_falls_back_to_prior(self, tmp_path):
        """复核轮数耗尽仍未获判决：查不出矛盾证据就等于维持原判——
        按锚定落地，不抛错、不再烧决胜跑"""
        tr = FakeTranslator([
            _text_msg('我在这里迷路了'),               # 第 1 轮：无判决
            _text_msg('[{"id": 9, "keep": true}]'),    # 强制轮：id 越界无法落位
        ])
        s = _mk_screener(tr, tmp_path)
        s.MAX_ROUNDS = 1
        cands = _cands(1)
        result = s._refine_batch(
            list(enumerate(cands)),
            anchored={0: {'keep': False, 'reason': '上轮是键名'}})
        assert result[0] == (False, '复核未找到矛盾的新证据，维持原判', '', '')
