"""scene_composer 图像解析与场景合成测试（临时文件 fixture）"""

import pytest
from PIL import Image

from flow_parser import SceneOp
from scene_composer import ImageResolver, compose_scene, THUMB_SIZE


def _make_image(path, size=(64, 48), color=(200, 30, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', size, color).save(path)


@pytest.fixture
def game_root(tmp_path):
    # 自动图像：images/ 前缀剥离 + 下划线拆分两种命名
    _make_image(tmp_path / 'images' / 'bg' / 'room.png', color=(10, 80, 160))
    _make_image(tmp_path / 'images' / 'eileen' / 'happy.png',
                size=(32, 96), color=(200, 30, 30))
    _make_image(tmp_path / 'images' / 'q_1.png', color=(30, 200, 30))
    # 角色子目录命名（非 images/ 根）
    _make_image(tmp_path / 'characters' / 'aleta' / 'aleta_main.png',
                size=(32, 96))
    # 显式定义（含写在 label 块内的运行时定义）
    (tmp_path / 'defs.rpy').write_text(
        'image bg school = "images/bg/room.png"\n'
        'label all_images:\n'
        '    image anim_1:\n'
        '        "images/q_1.png"\n'
        '        pause 0.1\n'
        '        repeat\n',
        encoding='utf-8')
    return tmp_path


def test_auto_image_resolution(game_root):
    r = ImageResolver(str(game_root))
    assert r.resolve(['bg', 'room']).name == 'room.png'
    assert r.resolve(['eileen', 'happy']).name == 'happy.png'


def test_underscore_variants(game_root):
    r = ImageResolver(str(game_root))
    # show q_1 → q_1.png；show q 1 → 拆分后同样命中
    assert r.resolve(['q_1']).name == 'q_1.png'
    assert r.resolve(['q', '1']).name == 'q_1.png'


def test_explicit_def_and_label_block(game_root):
    r = ImageResolver(str(game_root))
    # 显式定义优先于自动索引（bg school 显式指向 room.png）
    assert r.resolve(['bg', 'school']).name == 'room.png'
    # label 块内的 image x: ATL 动画取首帧
    assert r.resolve(['anim_1']).name == 'q_1.png'


def test_resolve_unknown(game_root):
    r = ImageResolver(str(game_root))
    assert r.resolve(['no_such', 'image']) is None


def test_char_sprite_character_subdir(game_root):
    r = ImageResolver(str(game_root))
    sprite = r.resolve_char_sprite('a', 'Aleta')
    assert sprite is not None and sprite.name == 'aleta_main.png'


def test_char_sprite_variable_tag(game_root):
    r = ImageResolver(str(game_root))
    sprite = r.resolve_char_sprite('eileen', 'Eileen')
    assert sprite is not None and sprite.name == 'happy.png'


def test_compose_scene_with_bg_and_sprite(game_root, tmp_path):
    r = ImageResolver(str(game_root))
    ops = [
        SceneOp(1, 'scene', ['bg', 'room']),
        SceneOp(2, 'show', ['eileen', 'happy'], ['left']),
    ]
    out = tmp_path / 'out' / 'n1.webp'
    assert compose_scene(r, ops, 3, 3, str(out))
    assert out.exists()
    with Image.open(out) as im:
        assert im.size == THUMB_SIZE


def test_compose_intro_slideshow_not_black(game_root, tmp_path):
    # intro 幻灯模式：scene bg black 在首条台词前，内容在台词之后
    # （单取首条台词前状态必得纯黑废图）
    r = ImageResolver(str(game_root))
    ops = [
        SceneOp(1, 'scene', ['bg', 'room']),
        SceneOp(3, 'show', ['eileen', 'happy'], ['left']),
    ]
    out = tmp_path / 'n2.webp'
    assert compose_scene(r, ops, 2, 4, str(out))
    with Image.open(out) as im:
        small = im.convert('L').resize((24, 14))
        assert sum(small.getdata()) / (24 * 14) > 12


def test_compose_fadeout_not_black(game_root, tmp_path):
    # 结尾淡出模式：内容在台词前，scene bg black 收尾
    r = ImageResolver(str(game_root))
    ops = [
        SceneOp(1, 'scene', ['bg', 'room']),
        SceneOp(3, 'scene', ['eileen', 'happy']),
    ]
    out = tmp_path / 'n3.webp'
    assert compose_scene(r, ops, 2, 2, str(out))
    with Image.open(out) as im:
        small = im.convert('L').resize((24, 14))
        assert sum(small.getdata()) / (24 * 14) > 12


def test_compose_scene_no_images(game_root, tmp_path):
    r = ImageResolver(str(game_root))
    assert not compose_scene(r, [], 0, 0, str(tmp_path / 'n4.webp'))
    assert not (tmp_path / 'n4.webp').exists()


def test_compose_scene_hide_removes_sprite(game_root, tmp_path):
    r = ImageResolver(str(game_root))
    ops = [
        SceneOp(1, 'show', ['eileen', 'happy'], ['left']),
        SceneOp(2, 'hide', ['eileen']),
    ]
    # 无 bg 且唯一立绘被 hide → 无可视内容，不生成
    assert not compose_scene(r, ops, 3, 3, str(tmp_path / 'n5.webp'))


def test_compose_scene_dynamic_skipped(game_root, tmp_path):
    r = ImageResolver(str(game_root))
    ops = [SceneOp(1, 'show', [], [], dynamic=True)]
    assert not compose_scene(r, ops, 0, 0, str(tmp_path / 'n6.webp'))
