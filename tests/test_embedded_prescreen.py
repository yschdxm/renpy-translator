"""「跳过 AI 预筛」（skip_ai_prescreen / static_only）测试

契约：
- static_only 只做零 token 规则分流（模型零调用），unknown 保持未决
- 无翻译器可纯扫描/人工标记；AI 动作（预筛/灰区/精判）各自门禁
- 人工勾选未决行补齐 apply_path（导出 wrap/fstring 查询依赖），
  不伪造 AI 判定、不覆盖已有路径
- payload source：rule / ai / pending（未决行不算 AI 判定）
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import ProjectDatabase
from embedded_strings import Candidate, find_candidates
from logger import TranslationLogger
from services.embedded_pipeline import EmbeddedPipeline


class _ExplodingTranslator:
    """任何模型调用即失败——证明 static_only 零 token"""
    def analyze_text(self, *a, **k):
        raise AssertionError('static_only 不应调用模型')

    def chat_completion(self, *a, **k):
        raise AssertionError('static_only 不应调用模型')


def _mk_project(tmp_path) -> Path:
    """项目游戏根：game/game/a.rpy（一条屏幕文本 + 一条脚本字符串）"""
    root = tmp_path / 'game'
    sub = root / 'game'
    (sub / 'tl' / 'chinese').mkdir(parents=True)
    (sub / 'a.rpy').write_text(
        'screen s:\n    text "Hello there"\n'
        'init python:\n    some_list = ["mystery text here"]\n',
        encoding='utf-8')
    return root


# ---- static_only：零 AI 调用，规则分流照常 ----

def test_static_only_zero_ai_calls(tmp_path):
    from ai_screener import AIScreener
    root = _mk_project(tmp_path)
    candidates = find_candidates(str(root))
    assert len(candidates) >= 2

    s = AIScreener(_ExplodingTranslator(), str(root))
    progress = {'phase': '', 'done': 0, 'total': 0, 'finished': False}
    try:
        s.screen_all(candidates, progress, static_only=True)
    finally:
        s.close()
    assert progress['finished'] is True
    # 只跑了规则分流，没进入粗筛/精审阶段
    assert progress['phase'] == '规则分流'
    # 屏幕文本是确定性显示上下文 → 静态 KEEP（规则理由前缀）
    hello = next(c for c in candidates if c.text == 'Hello there')
    assert hello.ai_keep is True
    assert hello.ai_reason.startswith('规则: ')


# ---- 管线：无翻译器纯扫描可用，AI 动作各自门禁 ----

def _mk_pipe(tmp_path, translator):
    root = _mk_project(tmp_path)
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    pipe = EmbeddedPipeline(db, translator, str(tmp_path), None,
                            TranslationLogger())
    return pipe, db, root


def test_skip_prescreen_without_translator(tmp_path):
    pipe, db, _ = _mk_pipe(tmp_path, None)
    rows = asyncio.run(pipe.scan_and_merge())
    assert rows
    asyncio.run(pipe.screen_undecided(rows, skip_ai_prescreen=True))
    rec = db.get_embedded_candidate(rows[0]['id'])
    hello = next(r for r in rows if r['candidate'].text == 'Hello there')
    rec = db.get_embedded_candidate(hello['id'])
    assert rec['ai_keep'] == 1                      # 静态 KEEP 照常入库
    assert rec['ai_reason'].startswith('规则: ')
    assert rec['apply_path'] == 'table'
    db.close()


def test_default_mode_requires_translator(tmp_path):
    pipe, db, _ = _mk_pipe(tmp_path, None)
    rows = asyncio.run(pipe.scan_and_merge())
    with pytest.raises(RuntimeError, match='AI 判定需要翻译器'):
        asyncio.run(pipe.screen_undecided(rows))
    db.close()


def test_ai_actions_require_translator(tmp_path):
    pipe, db, _ = _mk_pipe(tmp_path, None)
    rows = asyncio.run(pipe.scan_and_merge())
    with pytest.raises(RuntimeError, match='AI 判定需要翻译器'):
        asyncio.run(pipe.recheck_grayzone(rows))
    with pytest.raises(RuntimeError, match='AI 判定需要翻译器'):
        asyncio.run(pipe.refine_rows(rows))
    with pytest.raises(RuntimeError, match='AI 判定需要翻译器'):
        asyncio.run(pipe.refine_single(rows[0]))
    db.close()


def test_history_verdicts_untouched_by_skip(tmp_path):
    """历史已决行不在 skip 处理范围：keep/reason/apply_path 不变"""
    pipe, db, _ = _mk_pipe(tmp_path, _ExplodingTranslator())
    rows = asyncio.run(pipe.scan_and_merge())
    hello = next(r for r in rows if r['candidate'].text == 'Hello there')
    db.update_embedded_ai(hello['id'], 0, '键名/内部标识', False,
                          '', 'table', 'coarse')
    hello['ai_keep'] = 0
    hello['ai_reason'] = '键名/内部标识'
    asyncio.run(pipe.screen_undecided(rows, skip_ai_prescreen=True))
    rec = db.get_embedded_candidate(hello['id'])
    assert rec['ai_keep'] == 0
    assert rec['ai_reason'] == '键名/内部标识'
    assert rec['apply_path'] == 'table'
    db.close()


# ---- payload source 语义 ----

def test_row_payload_source_rule_ai_pending(tmp_path):
    from server.api.embedded import _row_payload
    c = Candidate(file='a.rpy', rel_file='a.rpy', line=1, col_start=0,
                  col_end=5, raw='"x"', text='x', kind='screen', hint='界面',
                  confidence='high')
    r = {'id': 1, 'candidate': c, 'ai_keep': -1, 'ai_reason': '',
         'status': 'pending'}
    assert _row_payload(r)['source'] == 'pending'
    r['ai_keep'] = 1
    r['ai_reason'] = '界面文本'
    assert _row_payload(r)['source'] == 'ai'
    r['ai_reason'] = '规则: 显示上下文（a.rpy:1）'
    assert _row_payload(r)['source'] == 'rule'


# ---- 人工勾选补 apply_path ----

def _row_from_rec(root, rec, **kw):
    c = Candidate(
        file=str(root / 'game' / rec['rel_file']), rel_file=rec['rel_file'],
        line=rec['line'], col_start=rec['col_start'],
        col_end=rec['col_start'] + len(rec['raw']), raw=rec['raw'],
        text=rec['text'], kind=rec['kind'], hint=rec['hint'],
        confidence=rec['confidence'])
    for k, v in kw.items():
        setattr(c, k, v)
    return {'id': rec['id'], 'candidate': c, 'ai_keep': rec['ai_keep'],
            'apply_path': rec['apply_path'] or ''}


def _apply_one(tmp_path, cand_kw=None, existing_path=''):
    """一条未决行（apply_path 空或指定）走人工勾选，返回 db 记录"""
    root = _mk_project(tmp_path)
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    c = Candidate(file='game/game/a.rpy', rel_file='a.rpy', line=2,
                  col_start=10, col_end=23, raw='"Hello there"',
                  text='Hello there', kind='screen', hint='界面',
                  confidence='high')
    rid = db.merge_embedded_candidates([c])[0]['id']
    if existing_path:
        db.update_embedded_ai(rid, 1, '界面文本', False, '', existing_path,
                              'coarse')
    rec = db.get_embedded_candidate(rid)
    row = _row_from_rec(root, rec, **(cand_kw or {}))
    pipe = object.__new__(EmbeddedPipeline)
    pipe.db = db
    pipe.game_root = root
    pipe.sdk_path = None
    pipe.logger = TranslationLogger()
    asyncio.run(pipe.apply_selection([row], [row]))
    return db, db.get_embedded_candidate(rid)


def test_manual_choice_fills_table_path(tmp_path):
    db, rec = _apply_one(tmp_path)
    assert rec['status'] == 'marked'
    assert rec['apply_path'] == 'table'
    assert rec['ai_keep'] == -1          # 人工勾选不伪造 AI 判定
    assert rec['ai_reason'] == ''
    db.close()


def test_manual_choice_fills_wrap_path_for_fragment(tmp_path):
    db, rec = _apply_one(tmp_path, cand_kw={'static_fragment': True})
    assert rec['apply_path'] == 'wrap'
    assert db.get_marked_embedded('wrap')[0]['id'] == rec['id']
    db.close()


def test_manual_choice_fills_fstring_path(tmp_path):
    db, rec = _apply_one(tmp_path, cand_kw={'kind': 'fstring'})
    assert rec['apply_path'] == 'fstring'
    assert db.get_marked_embedded('fstring')[0]['id'] == rec['id']
    db.close()


def test_manual_choice_preserves_existing_path(tmp_path):
    db, rec = _apply_one(tmp_path, existing_path='wrap')
    assert rec['apply_path'] == 'wrap'   # 已有 AI 路径不被重算覆盖
    db.close()
