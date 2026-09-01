"""项目创建的自愈机制测试：补齐 SDK 标准库缺失的 init 依赖模块

背景：Lab Rats 2 在 script.rpy 的 init 里 import unittest，其依赖 pprint
是作者手动塞进发行包 lib/python2.7 的，SDK 精简标准库里没有 → SDK 生成
模板即 ImportError。heal_missing_module 从游戏自带运行库把模块复制进
项目副本 game/ 后重试。
"""
import asyncio
import shutil

import pytest

from services.project_creation import generate_tl_templates, heal_missing_module


# ---- 1. heal_missing_module 单元用例 ----

def make_game(tmp_path, lib='python2.7'):
    """构造带自带运行库的游戏工作目录，返回 (game_work_dir, lib 目录)"""
    lib_dir = tmp_path / 'lib' / lib
    (tmp_path / 'game').mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    return tmp_path, lib_dir


def test_heal_single_module(tmp_path):
    work, lib_dir = make_game(tmp_path)
    (lib_dir / 'pprint.py').write_text('saferepr = repr', encoding='utf-8')

    name = heal_missing_module(work, 'ImportError: No module named pprint')
    assert name == 'pprint'
    assert (work / 'game' / 'pprint.py').read_text(encoding='utf-8') == 'saferepr = repr'


def test_heal_quoted_py3_name(tmp_path):
    work, lib_dir = make_game(tmp_path, lib='python3.11')
    (lib_dir / 'pprint.py').write_text('x = 1', encoding='utf-8')

    name = heal_missing_module(
        work, "ModuleNotFoundError: No module named 'pprint'")
    assert name == 'pprint'
    assert (work / 'game' / 'pprint.py').exists()


def test_heal_dotted_name_takes_top_level(tmp_path):
    work, lib_dir = make_game(tmp_path)
    (lib_dir / 'email').mkdir()
    (lib_dir / 'email' / '__init__.py').write_text('', encoding='utf-8')

    name = heal_missing_module(work, "ImportError: No module named email.mime")
    assert name == 'email'
    assert (work / 'game' / 'email' / '__init__.py').exists()


def test_no_heal_when_module_already_in_game(tmp_path):
    """game/ 下已有同名模块仍报错 = 注入无效，返回 None 不覆盖"""
    work, _ = make_game(tmp_path)
    existing = work / 'game' / 'pprint.py'
    existing.write_text('original = True', encoding='utf-8')

    name = heal_missing_module(work, 'ImportError: No module named pprint')
    assert name is None
    assert existing.read_text(encoding='utf-8') == 'original = True'


def test_no_heal_when_source_missing(tmp_path):
    work, _ = make_game(tmp_path)
    assert heal_missing_module(
        work, 'ImportError: No module named pprint') is None


def test_no_heal_on_unrelated_output(tmp_path):
    """脚本语法错误等输出不含 No module named，不触发"""
    work, _ = make_game(tmp_path)
    out = 'File "game/scripts/x.rpy", line 12\\nSyntaxError: invalid syntax'
    assert heal_missing_module(work, out) is None


# ---- 2. generate_tl_templates 集成：失败→自愈→重试成功 ----

IMPORT_ERR_OUTPUT = '''\
While running game code:
  File "game/script.rpy", line 1, in script
  File "game/script.rpy", line 12, in <module>
    import unittest
  File "unittest/case.py", line 6, in <module>
ImportError: No module named pprint
'''


class FakeSDKManager:
    """按序返回预设结果；记录调用次数

    results/calls 挂类上：generate_tl_templates 内部自行实例化，
    测试处无法持有该实例
    """

    calls = 0
    results = None

    def __init__(self, sdk_path=''):
        self.sdk_path = sdk_path

    def generate_translations(self, game_dir, language, cancel_event=None):
        type(self).calls += 1
        return type(self).results.pop(0)


class StubLogger:
    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


def run_generate(work):
    """同步驱动 generate_tl_templates（executor 直接调用、db 不触发）"""
    async def rie(fn, *args):
        return fn(*args)

    return asyncio.run(generate_tl_templates(
        'fake-sdk', work, [], None, StubLogger(),
        lambda pct, text: None, rie, cancel_event=None))


def test_generate_retries_after_heal(tmp_path, monkeypatch):
    work, lib_dir = make_game(tmp_path)
    (lib_dir / 'pprint.py').write_text('saferepr = repr', encoding='utf-8')
    # 第二次成功前预置一个模板输出，通过 has_templates 检查
    tl = work / 'game' / 'tl' / 'chinese'
    tl.mkdir(parents=True)
    (tl / 'scripts.rpy').write_text('translate chinese strings:', encoding='utf-8')

    FakeSDKManager.results = [
        {'success': False, 'message': '生成失败 (返回码: 1)',
         'output': IMPORT_ERR_OUTPUT},
        {'success': True, 'message': 'ok', 'output': ''},
    ]
    monkeypatch.setattr('sdk_manager.SDKManager', FakeSDKManager,
                        raising=True)
    FakeSDKManager.calls = 0

    assert run_generate(work) == []
    assert FakeSDKManager.calls == 2
    assert (work / 'game' / 'pprint.py').exists()


def test_generate_raises_when_unhealable(tmp_path, monkeypatch):
    """游戏自带 lib 里也没有该模块：第一次即抛原错误，信息带上 SDK 输出"""
    work, _ = make_game(tmp_path)

    FakeSDKManager.results = [
        {'success': False, 'message': '生成失败 (返回码: 1)',
         'output': IMPORT_ERR_OUTPUT}]
    monkeypatch.setattr('sdk_manager.SDKManager', FakeSDKManager,
                        raising=True)
    FakeSDKManager.calls = 0

    with pytest.raises(Exception, match='No module named pprint'):
        run_generate(work)
    assert FakeSDKManager.calls == 1
