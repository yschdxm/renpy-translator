"""角色表构建测试：以源码构造证据为准，泛指说话人变量不建角色

背景：Lab Rats 2 型动态游戏的角色全在运行时由工厂函数构造；台词里
的 the_person 等泛指形参不是具体角色，不能以变量名伪装成角色。
"""
from services.game_pipeline import refresh_characters


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


def test_dialogue_speakers_enter_as_placeholder(tmp_path):
    """无构造证据的说话人也入表，display_name 留空

    动态游戏的泛指形参（the_person）与路人变量确实说话了，不能漏；
    没有静态显示名就留空（显示层兜底变量名），is_placeholder 是
    [动态名] 角色的专用标记，此处不用
    """
    game = tmp_path / 'game'
    game.mkdir()
    (game / 'foo.rpy').write_text(
        'label a:\n    mom "hi"\n', encoding='utf-8')

    dialogues = [{'character': 'mom'}, {'character': 'the_person'}]
    db = StubDB()
    refresh_characters(tmp_path, db, dialogues)

    by_var = {c['variable']: c for c in db.inserted}
    assert by_var['the_person']['display_name'] == ''
    assert 'is_placeholder' not in by_var['the_person']


def test_factory_character_creates_row(tmp_path):
    """工厂函数构造(name = "Lily")的角色正常入表且非占位；同名说话人
    不重复建行"""
    game = tmp_path / 'game'
    game.mkdir()
    (game / 'foo.rpy').write_text(
        'lily = create_random_person(name = "Lily", age = 19)\n'
        'label a:\n'
        '    lily "hi"\n',
        encoding='utf-8')

    db = StubDB()
    refresh_characters(tmp_path, db, [{'character': 'lily'}])

    assert len(db.inserted) == 1
    row = db.inserted[0]
    assert row['variable'] == 'lily'
    assert row['display_name'] == 'Lily'
    assert not row.get('is_placeholder')


def test_refresh_characters_keeps_static_definitions(tmp_path):
    """静态 define Character 以显示名入表；泛指说话人另作占位行"""
    game = tmp_path / 'game'
    game.mkdir()
    (game / 'foo.rpy').write_text(
        'define e = Character("Eileen")\n', encoding='utf-8')

    dialogues = [{'character': 'the_person'}]
    db = StubDB()
    refresh_characters(tmp_path, db, dialogues)

    by_var = {c['variable']: c for c in db.inserted}
    assert by_var['e']['display_name'] == 'Eileen'
    assert by_var['the_person']['display_name'] == ''
    assert 'is_placeholder' not in by_var['e']
