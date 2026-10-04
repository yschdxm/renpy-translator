"""引号说话人（匿名角色）人名修复 + 翻译/分析拆分测试

背景（bug）：游戏脚本用引号说话人 `"Ricardo" "..."`（Ren'Py 匿名角色，
名字字面量直接显示在对话框）。旧链路解析时剥掉引号，refresh_characters
把所有非静态定义的说话人按 display_name='' 入表，人名页显示「无需翻译」，
导出也不替换说话人——游戏内名字永远英文（BH-AM 实测 21 个角色）。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import ProjectDatabase  # noqa: E402
from services.game_pipeline import refresh_characters  # noqa: E402
from services.game_export import GameExporter  # noqa: E402
from services.name_translation import NameTranslationService  # noqa: E402


# ---- refresh_characters：引号说话人识别 ----

class StubDB:
    def __init__(self):
        self.inserted = []
        self.line_updates = {}

    def insert_characters(self, characters):
        self.inserted.extend(characters)

    def get_variable_map(self):
        return {}

    def update_character_lines_count(self, display_name, count):
        self.line_updates[display_name] = count


def test_quoted_speaker_gets_display_name(tmp_path):
    """源码中的引号说话人 display_name 取字面量；泛指变量仍为空"""
    game = tmp_path / 'game'
    game.mkdir()
    (game / 'foo.rpy').write_text(
        'label a:\n'
        '    "Ricardo" "hi"\n'
        '    "Woman in ear piece" "copy"\n'
        '    the_person "hello"\n',
        encoding='utf-8')

    dialogues = [{'character': 'Ricardo'},
                 {'character': 'Woman in ear piece'},
                 {'character': 'the_person'}]
    db = StubDB()
    refresh_characters(tmp_path, db, dialogues)

    by_var = {c['variable']: c for c in db.inserted}
    assert by_var['Ricardo']['display_name'] == 'Ricardo'
    assert by_var['Woman in ear piece']['display_name'] == 'Woman in ear piece'
    # 泛指运行时变量不受影响
    assert by_var['the_person']['display_name'] == ''


def test_quoted_speaker_from_tl_template(tmp_path):
    """纯 rpyc 反编译失败的降级场景：从 tl 模板注释行反推引号说话人"""
    tl = tmp_path / 'game' / 'tl' / 'chinese'
    tl.mkdir(parents=True)
    (tl / 'script.rpy').write_text(
        'translate chinese a_12345678:\n'
        '    # "Ricardo" "hi"\n'
        '    "Ricardo" "hi"\n'
        '    # "just narration"\n'
        '    "只是旁白"\n',
        encoding='utf-8')

    db = StubDB()
    refresh_characters(tmp_path, db,
                       [{'character': 'Ricardo'}, {'character': 'just narration'}])

    by_var = {c['variable']: c for c in db.inserted}
    assert by_var['Ricardo']['display_name'] == 'Ricardo'
    # 旁白注释（单字符串）不能被误认为引号说话人
    assert by_var['just narration']['display_name'] == ''


# ---- 导出：引号说话人替换 ----

class _ExportStubDB:
    def __init__(self, characters):
        self._characters = characters

    def get_characters(self):
        return self._characters


class _StubLogger:
    def error(self, *a, **kw):
        pass

    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass


def _char(variable, display_name, cn_name):
    return {'variable': variable, 'display_name': display_name,
            'cn_name': cn_name, 'lines_count': 0, 'profile_json': '',
            'is_placeholder': False}


def _fill(tmp_path, characters, tl_body, translation_dict):
    tl_dir = tmp_path / 'tl'
    tl_dir.mkdir()
    (tl_dir / 'script.rpy').write_text(tl_body, encoding='utf-8')
    ex = GameExporter.__new__(GameExporter)
    ex.db = _ExportStubDB(characters)
    ex.logger = _StubLogger()
    ex._cancel_event = None
    ex._blocked = []
    ex._fill_dialogue(tl_dir, translation_dict)
    return (tl_dir / 'script.rpy').read_text(encoding='utf-8')


TL_QSPK = '''translate chinese a_12345678:
    # "Ricardo" "Yes, ma'am."
    "Ricardo" "Yes, ma'am."
'''


def test_export_replaces_quoted_speaker(tmp_path):
    """人名表有译文时，new 行说话人替换为中文名"""
    out = _fill(tmp_path, [_char('Ricardo', 'Ricardo', '里卡多')],
                TL_QSPK, {"Yes, ma'am.": '是的，夫人。'})
    assert '"里卡多" "是的，夫人。"' in out


def test_export_keep_original_speaker_untouched(tmp_path):
    """保留原名（cn == en）不进替换表，说话人保持原样"""
    out = _fill(tmp_path, [_char('Ricardo', 'Ricardo', 'Ricardo')],
                TL_QSPK, {"Yes, ma'am.": '是的，夫人。'})
    assert '"Ricardo" "是的，夫人。"' in out


def test_export_untranslated_name_keeps_line(tmp_path):
    """人名未翻译时整行保持原样（说话人与台词同为英文）"""
    out = _fill(tmp_path, [_char('Ricardo', 'Ricardo', '')],
                TL_QSPK, {})
    assert out == TL_QSPK


# ---- keep_original_names ----

def _db(tmp_path):
    db = ProjectDatabase(str(tmp_path / 'p.db'))
    db.connect()
    return db


def test_keep_original_names(tmp_path):
    db = _db(tmp_path)
    db.insert_characters([
        {'variable': 'r', 'display_name': 'Ricardo'},
        {'variable': 't', 'display_name': 'Talia', 'cn_name': '塔莉娅'},
        {'variable': 'x', 'display_name': ''},
        {'variable': 'mc', 'display_name': '[mc_name]', 'is_placeholder': True},
    ])
    try:
        n = db.keep_original_names()
        assert n == 1
        chars = {c['variable']: c for c in db.get_characters()}
        assert chars['r']['cn_name'] == 'Ricardo'          # 填充未翻译项
        assert chars['t']['cn_name'] == '塔莉娅'            # 不覆盖已有译文
        assert chars['x']['cn_name'] == ''                  # 空显示名跳过
        assert chars['mc']['cn_name'] == ''                 # 占位符跳过
    finally:
        db.close()


# ---- name_translation mode 拆分 ----

class _NameStubDB:
    def __init__(self, display_name='Ricardo', lines=('hi',)):
        self._display_name = display_name
        self._lines = list(lines)
        self.cn_saved = []
        self.profiles_saved = []

    def get_characters(self):
        return [{'variable': 'r', 'display_name': self._display_name,
                 'cn_name': '', 'profile_json': '', 'is_placeholder': False}]

    def get_glossary_for_prompt(self):
        return ''

    def get_characters_for_prompt(self):
        return ''

    def get_variable_map(self):
        return {'r': self._display_name} if self._display_name else {}

    def get_dialogues_by_character(self, var):
        return [{'original_text': t} for t in self._lines]

    def update_character_cn_name(self, display_name, cn_name, variable=None):
        self.cn_saved.append((display_name, cn_name, variable))

    def save_profile(self, display_name, profile, variable=None):
        self.profiles_saved.append((display_name, profile))


class _StubTranslator:
    def translate_name(self, name, glossary_text='', debug=False):
        return '里卡多'

    def analyze_text(self, prompt=''):
        return '【人名翻译】\n中文名：里卡多\n性格特点：急躁'


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _service(db):
    return NameTranslationService(
        db=db, translator=_StubTranslator(), translation_service=None,
        logger=_StubLogger(), max_context_k=8)


def test_mode_name_only_saves_cn_not_profile(tmp_path):
    db = _NameStubDB()
    svc = _service(db)
    _run(svc.translate_and_analyze('Ricardo', variable='r', mode='name'))
    assert db.cn_saved == [('Ricardo', '里卡多', 'r')]
    assert db.profiles_saved == []


def test_mode_analyze_saves_profile_not_cn(tmp_path):
    db = _NameStubDB()
    svc = _service(db)
    _run(svc.translate_and_analyze('Ricardo', variable='r', mode='analyze'))
    assert db.cn_saved == []
    assert len(db.profiles_saved) == 1
    assert db.profiles_saved[0][1].get('性格特点') == '急躁'


def test_mode_both_saves_both(tmp_path):
    db = _NameStubDB()
    svc = _service(db)
    _run(svc.translate_and_analyze('Ricardo', variable='r', mode='both'))
    assert db.cn_saved == [('Ricardo', '里卡多', 'r')]
    assert len(db.profiles_saved) == 1


def test_mode_name_skips_nameless_character(tmp_path):
    """无显示名角色（泛指变量）按库中真实显示名核实后跳过翻译"""
    db = _NameStubDB(display_name='')
    svc = _service(db)
    _run(svc.translate_and_analyze('the_person', variable='r', mode='name'))
    assert db.cn_saved == []
