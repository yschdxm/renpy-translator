"""游戏导出：语言切换按钮注入与默认中文启动"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from services.export_healer import ExportHealer  # noqa: E402
from services.game_export import GameExporter  # noqa: E402


class FakeDB:
    def __init__(self, store=None, marked=None):
        self.store = dict(store or {})
        self.marked = list(marked or [])

    def get_meta(self, key, default=''):
        return self.store.get(key, default)

    def set_meta(self, key, value):
        self.store[key] = value

    def get_marked_embedded(self):
        return self.marked


@pytest.fixture
def exporter():
    # 注入/默认语言/保留决策都不依赖实例状态（db 按需注入）
    return object.__new__(GameExporter)


TEMPLATE_PREFERENCES = '''screen preferences():
    tag menu
    use game_menu(_("Preferences"), scroll="viewport"):
        vbox:
            hbox:
                style_prefix "check"
                label _("Skip")

            null height (4 * gui.pref_spacing)

            hbox:
                style_prefix "slider"
                bar value Preference("text speed")
'''

# 模板被作者改过：间隔行写法不同，只能靠 slider hbox 兜底
CUSTOM_PREFERENCES = '''screen preferences():
    tag menu
    use game_menu(_("Prefs")):
        vbox:
            hbox:
                style_prefix "check"
                label _("Skip")
            hbox:
                style_prefix "slider"
                bar value Preference("text speed")
'''


def _mk_game(tmp_path, screens=None, subdir='game'):
    game = tmp_path / subdir
    game.mkdir(parents=True)
    if screens is not None:
        (game / 'screens.rpy').write_text(screens, encoding='utf-8')
    return tmp_path


def _read(tmp_path):
    return (tmp_path / 'game' / 'screens.rpy').read_text(encoding='utf-8')


class TestAddLanguageSelector:
    def test_template_anchor(self, exporter, tmp_path):
        _mk_game(tmp_path, TEMPLATE_PREFERENCES)
        logs = []
        exporter._add_language_selector(tmp_path, logs.append)
        content = _read(tmp_path)
        assert 'textbutton "中文" action Language("chinese")' in content
        # 注入在 null height 锚点之前，且继承其缩进
        lines = content.split('\n')
        i_label = next(i for i, l in enumerate(lines)
                       if 'label _("Language")' in l)
        i_null = next(i for i, l in enumerate(lines)
                      if 'null height' in l)
        assert i_label < i_null
        assert lines[i_label].startswith('                ')

    def test_slider_hbox_fallback(self, exporter, tmp_path):
        _mk_game(tmp_path, CUSTOM_PREFERENCES)
        logs = []
        exporter._add_language_selector(tmp_path, logs.append)
        content = _read(tmp_path)
        assert 'Language("chinese")' in content
        # 插在 slider hbox 之前
        assert content.index('label _("Language")') < content.index(
            'style_prefix "slider"')

    def test_no_preferences_screen(self, exporter, tmp_path):
        _mk_game(tmp_path, 'screen foo():\n    pass\n')
        logs = []
        exporter._add_language_selector(tmp_path, logs.append)
        assert 'Language("chinese")' not in _read(tmp_path)
        assert any('警告' in m for m in logs)

    def test_no_screens_rpy(self, exporter, tmp_path):
        _mk_game(tmp_path)
        logs = []
        exporter._add_language_selector(tmp_path, logs.append)
        assert any('警告' in m for m in logs)

    def test_idempotent(self, exporter, tmp_path):
        _mk_game(tmp_path, TEMPLATE_PREFERENCES)
        logs = []
        exporter._add_language_selector(tmp_path, logs.append)
        exporter._add_language_selector(tmp_path, logs.append)
        assert _read(tmp_path).count('Language("chinese")') == 1

    def test_scripts_subdir(self, exporter, tmp_path):
        scripts = tmp_path / 'game' / 'scripts'
        scripts.mkdir(parents=True)
        (scripts / 'screens.rpy').write_text(TEMPLATE_PREFERENCES,
                                             encoding='utf-8')
        logs = []
        exporter._add_language_selector(tmp_path, logs.append)
        assert 'Language("chinese")' in (
            scripts / 'screens.rpy').read_text(encoding='utf-8')

    def test_nonstandard_location(self, exporter, tmp_path):
        """非标准位置（game/ui/screens.rpy）也能找到并注入"""
        ui = tmp_path / 'game' / 'ui'
        ui.mkdir(parents=True)
        (ui / 'screens.rpy').write_text(TEMPLATE_PREFERENCES,
                                        encoding='utf-8')
        logs = []
        exporter._add_language_selector(tmp_path, logs.append)
        assert 'Language("chinese")' in (
            ui / 'screens.rpy').read_text(encoding='utf-8')

    def test_tl_skeleton_not_a_candidate(self, exporter, tmp_path):
        """tl/chinese 下的翻译骨架不是注入目标"""
        tl = tmp_path / 'game' / 'tl' / 'chinese'
        tl.mkdir(parents=True)
        (tl / 'screens.rpy').write_text(
            'translate chinese strings:\n    old "OK"\n    new "好"\n',
            encoding='utf-8')
        logs = []
        exporter._add_language_selector(tmp_path, logs.append)
        assert 'Language(' not in (
            tl / 'screens.rpy').read_text(encoding='utf-8')
        assert any('警告' in m for m in logs)


class TestPickKeptDecompiled:
    """反编译产物的保留决策：语言按钮载体 + 内嵌标记文件"""

    def _mk_screens(self, tmp_path, rel='game/scripts/screens.rpy'):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TEMPLATE_PREFERENCES, encoding='utf-8')

    def test_keep_injectable_screens(self, exporter, tmp_path):
        self._mk_screens(tmp_path)
        exporter.db = FakeDB()
        keep = exporter._pick_kept_decompiled(
            tmp_path, ['game/scripts/screens.rpy', 'game/script.rpy'])
        assert keep == ['game/scripts/screens.rpy']

    def test_keep_marked_embedded_files(self, exporter, tmp_path):
        """含内嵌标记 _() 的反编译文件必须保留，否则译文运行时不生效"""
        exporter.db = FakeDB(marked=[
            {'rel_file': 'scripts/specialCharactersData.rpy'},
            {'rel_file': 'scripts/helperFuncs.rpy'},
            {'rel_file': 'scripts/already_gone.rpy'},  # 不在清单里
        ])
        keep = exporter._pick_kept_decompiled(
            tmp_path, ['game/scripts/specialCharactersData.rpy',
                       'game/scripts/helperFuncs.rpy',
                       'game/script.rpy'])
        assert keep == ['game/scripts/helperFuncs.rpy',
                        'game/scripts/specialCharactersData.rpy']

    def test_dropped_files_excluded(self, exporter, tmp_path):
        """自愈判定编译失败的文件永久排除"""
        self._mk_screens(tmp_path)
        exporter.db = FakeDB(
            store={'dropped_decompiled_files':
                   '["game/scripts/screens.rpy"]'},
            marked=[{'rel_file': 'scripts/helperFuncs.rpy'}])
        keep = exporter._pick_kept_decompiled(
            tmp_path, ['game/scripts/screens.rpy',
                       'game/scripts/helperFuncs.rpy'])
        assert keep == ['game/scripts/helperFuncs.rpy']

    def test_original_source_not_kept(self, exporter, tmp_path):
        """注入目标是原始源码（不在反编译清单里）时不需要保留豁免"""
        _mk_game(tmp_path, TEMPLATE_PREFERENCES)
        exporter.db = FakeDB()
        assert exporter._pick_kept_decompiled(
            tmp_path, ['game/script.rpy']) == []

    def test_nothing_to_keep(self, exporter, tmp_path):
        exporter.db = FakeDB()
        assert exporter._pick_kept_decompiled(
            tmp_path, ['game/script.rpy']) == []


class TestHealerDropsBrokenKeptFiles:
    """兜底：保留的反编译文件编译报错 → 记入 dropped 列表 + 重导出"""

    def _healer(self, db):
        h = object.__new__(ExportHealer)
        h._cancel_check = None
        h.db = db
        return h

    def test_kept_file_error_triggers_reexport(self, tmp_path):
        db = FakeDB({'kept_decompiled_files':
                     '["game/scripts/screens.rpy", "game/scripts/init.rpy"]'})
        errors = [{'file': 'game/scripts/screens.rpy', 'line': 42,
                   'msg': 'SyntaxError'},
                  {'file': 'game/scripts/screens.rpy', 'line': 43,
                   'msg': 'SyntaxError'}]
        logs = []
        status = asyncio.run(
            self._healer(db)._heal_round(errors, tmp_path, logs.append))
        assert status == 'reexport'
        assert db.store['dropped_decompiled_files'] == (
            '["game/scripts/screens.rpy"]')

    def test_other_game_error_unaffected(self, tmp_path):
        """报错不在保留文件上时仍走内嵌标记路径（无候选 → fail）"""
        db = FakeDB({'kept_decompiled_files': '["game/scripts/screens.rpy"]'})
        errors = [{'file': 'game/script.rpy', 'line': 1, 'msg': 'boom'}]
        logs = []
        status = asyncio.run(
            self._healer(db)._heal_round(errors, tmp_path, logs.append))
        assert status == 'fail'
        assert 'dropped_decompiled_files' not in db.store


class TestChineseFontMapping:
    """游戏自带字体（无 CJK 字形）必须进入 font_replacement_map"""

    def _patch_fonts(self, monkeypatch, tmp_path):
        """伪造数据根字体目录（软件不携带字体，用户手动放置）"""
        user_fonts = tmp_path / 'user_fonts'
        user_fonts.mkdir(exist_ok=True)
        (user_fonts / 'SomeCJK.ttf').write_bytes(b'y')
        import rt_home
        monkeypatch.setattr(rt_home, 'find_resource',
                            lambda name: user_fonts if name == 'fonts'
                            else None)
        monkeypatch.setattr(rt_home, 'home', lambda: tmp_path)
        return user_fonts

    def test_game_fonts_mapped(self, exporter, tmp_path, monkeypatch):
        game = tmp_path / 'game'
        (game / 'gui' / 'fonts').mkdir(parents=True)
        (game / 'gui' / 'fonts' / 'Closeness.ttf').write_bytes(b'x')
        (game / 'tl' / 'chinese').mkdir(parents=True)
        self._patch_fonts(monkeypatch, tmp_path)

        font_files = exporter._require_user_fonts()
        logs = []
        exporter._add_chinese_font(tmp_path, logs.append, font_files)
        override = (game / 'tl' / 'chinese' / 'font_override.rpy')
        content = override.read_text(encoding='utf-8')
        assert 'config.font_replacement_map["gui/fonts/Closeness.ttf", False, False]' in content
        assert 'config.font_replacement_map["Closeness.ttf", False, False]' in content
        assert 'config.font_replacement_map["DejaVuSans.ttf", False, False]' in content
        # 中文字体自身不能被映射（否则自我替换）
        assert 'SomeCJK.ttf", False, False] = ("fonts/SomeCJK' not in content
        # 字体文件已复制
        assert (game / 'fonts' / 'SomeCJK.ttf').exists()

    def test_missing_font_aborts(self, exporter, tmp_path, monkeypatch):
        """用户未放置字体：导出预检直接失败（不允许无字体导出）"""
        import rt_home
        monkeypatch.setattr(rt_home, 'find_resource', lambda name: None)
        monkeypatch.setattr(rt_home, 'home', lambda: tmp_path)
        with pytest.raises(RuntimeError, match='未找到中文字体'):
            exporter._require_user_fonts()

    def test_empty_fonts_dir_aborts(self, exporter, tmp_path, monkeypatch):
        """fonts 目录存在但没有字体文件同样失败"""
        empty = tmp_path / 'fonts'
        empty.mkdir()
        import rt_home
        monkeypatch.setattr(rt_home, 'find_resource',
                            lambda name: empty if name == 'fonts' else None)
        monkeypatch.setattr(rt_home, 'home', lambda: tmp_path)
        with pytest.raises(RuntimeError, match='未找到中文字体'):
            exporter._require_user_fonts()




class TestSetDefaultLanguage:
    def test_appends_to_common(self, exporter, tmp_path):
        tl = tmp_path / 'game' / 'tl' / 'chinese'
        tl.mkdir(parents=True)
        (tl / 'script.rpy').write_text('translate chinese s_1:\n    e "hi"\n',
                                       encoding='utf-8')
        (tl / 'common.rpy').write_text('translate chinese strings:\n',
                                       encoding='utf-8')
        logs = []
        exporter._set_default_language(tmp_path, logs.append)
        content = (tl / 'common.rpy').read_text(encoding='utf-8')
        assert '_preferences.language = "chinese"' in content
        assert 'persistent._rt_default_lang' in content
        assert '_preferences.language = "chinese"' not in (
            tl / 'script.rpy').read_text(encoding='utf-8')

    def test_fallback_to_first_file(self, exporter, tmp_path):
        tl = tmp_path / 'game' / 'tl' / 'chinese'
        tl.mkdir(parents=True)
        (tl / 'script.rpy').write_text('translate chinese s_1:\n    e "hi"\n',
                                       encoding='utf-8')
        exporter._set_default_language(tmp_path, lambda m: None)
        assert '_preferences.language' in (
            tl / 'script.rpy').read_text(encoding='utf-8')

    def test_idempotent(self, exporter, tmp_path):
        tl = tmp_path / 'game' / 'tl' / 'chinese'
        tl.mkdir(parents=True)
        (tl / 'common.rpy').write_text('translate chinese strings:\n',
                                       encoding='utf-8')
        exporter._set_default_language(tmp_path, lambda m: None)
        exporter._set_default_language(tmp_path, lambda m: None)
        assert (tl / 'common.rpy').read_text(encoding='utf-8').count(
            '_rt_default_lang') == 2  # getattr + 赋值两行，只追加一次

    def test_no_tl_dir(self, exporter, tmp_path):
        logs = []
        exporter._set_default_language(tmp_path, logs.append)
        assert any('警告' in m for m in logs)
