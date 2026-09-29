"""批次预算只扣本批角色资料，保留角色变化导致拆批时的预算约束。"""
from types import SimpleNamespace

import pytest

from translation_service import TranslationService
from token_budget import TokenBudget


def test_exhausted_window_has_no_phantom_source_capacity():
    assert TokenBudget(4096).batch_src_token_budget(
        profile_tokens=4200, declared_max_tokens=10000) == 0


@pytest.fixture
def service(monkeypatch):
    # 固定估算，使测试覆盖分组行为而不依赖下载分词器。
    monkeypatch.setattr('translation_service.count_tokens', len)
    s = TranslationService(None, None, None, max_context_k=4,
                           max_tokens=10000, batch_lines=20)
    yield s
    s.close()


def test_many_roles_do_not_reduce_unrelated_batches(service):
    items = [dict(id=c * 20 + i, character=str(c), original_text='hello')
             for c in range(100) for i in range(20)]
    batches = service.group_into_batches(
        items, profile_tokens_by_character={str(c): 1800 for c in range(100)})
    assert [len(b) for b in batches] == [20] * 100
    assert [row for b in batches for row in b] == items


def test_new_role_splits_batch_and_profile_budget_resets(service):
    items = [dict(id=i, character=char, original_text='hello')
             for i, char in enumerate(['a'] * 10 + ['b'] * 10 + ['a'] * 10)]
    batches = service.group_into_batches(
        items, profile_tokens_by_character={'a': 2100, 'b': 2100})
    assert [len(b) for b in batches] == [10, 10, 10]
    assert [row for b in batches for row in b] == items


def test_fixed_material_and_output_limit_still_split(service):
    items = [dict(id=i, character='a', original_text='x' * 100) for i in range(20)]
    service.max_tokens = 1500
    baseline = service.group_into_batches(items)
    crowded = service.group_into_batches(
        items, glossary_tokens=1000, profile_tokens_by_character={'a': 1000})
    assert max(map(len, baseline)) < 20
    assert len(crowded) > len(baseline)
    assert [row for b in crowded for row in b] == items


async def test_prepare_caches_each_profile_and_runs_off_event_loop(service):
    import threading
    event_thread = threading.get_ident()
    profile_reads = []

    def profile(char):
        assert threading.get_ident() != event_thread
        profile_reads.append(char)
        return {'语气': 'x' * 1750}

    service.db = SimpleNamespace(
        get_profile=profile, get_glossary_for_prompt=lambda: '',
        get_characters_for_prompt=lambda: '', get_meta=lambda key: '')
    items = [dict(id=c * 20 + i, character=str(c), original_text='hello')
             for c in range(50) for i in range(20)]
    batches = await service.prepare_batches(items, 'dialogue')
    assert [len(b) for b in batches] == [20] * 50
    assert sorted(profile_reads) == sorted({i['character'] for i in items})


async def test_ui_does_not_read_character_profiles(service):
    def unexpected(char):
        raise AssertionError('UI batches must not load character profiles')

    service.db = SimpleNamespace(
        get_profile=unexpected, get_glossary_for_prompt=lambda: '',
        get_characters_for_prompt=lambda: '', get_meta=lambda key: 'style ' * 20)
    items = [dict(id=i, original_text='hello') for i in range(40)]
    assert [len(b) for b in await service.prepare_batches(items, 'ui')] == [20, 20]
