"""场景合成器：图像名解析（显式 image 定义 + 自动图像索引）+ Pillow 场景缩略图合成

Ren'Py 图像名解析的两种来源：
1. 显式定义：`image bg room = "images/bg/room.webp"`（含 Movie/ATL/Composite 等
   表达式降级取首个图片路径字面量；layeredimage 取 always 层）
2. 自动图像：game 目录下所有图片文件按路径组件命名（images/ 前缀剥掉，
   文件名按空格/下划线拆组件），实测不少游戏全靠这一种

场景状态机按节点 scene/show/hide 快照顺序应用，取"首次对话时"的画面状态，
用 Pillow 拼合背景+立绘为缩略图（位置只取横向近似锚点，ATL/transform
不精确还原）。

角色立绘：优先 Character(image="tag") 定义，其次变量名/显示名匹配图像 tag，
取该 tag 下组件最少（最"基础"）的变体。
"""

import re
from pathlib import Path
from typing import Optional

from PIL import Image

from source_tree import SourceTree

_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp'}
_AUTO_EXCLUDE = {'cache', 'saves', 'tl', 'gui'}  # gui 多为界面素材，不进索引

# image bg room = "path" / image x = Movie(...) / image x = Solid(...)
_IMAGE_DEF_RE = re.compile(r'^image\s+([\w\s]+?)\s*=\s*(.+)$')
# image x: 块 / layeredimage x: 块
_IMAGE_BLOCK_RE = re.compile(r'^(?:image|layeredimage)\s+([\w\s]+?)\s*:\s*$')
_QUOTED_IMG_RE = re.compile(r'["\']([^"\']+\.(?:png|jpg|jpeg|webp))["\']',
                            re.IGNORECASE)
# Solid("#000") / Solid("#fff") —— 纯色占位图
_SOLID_RE = re.compile(r'Solid\(\s*["\'](#[0-9a-fA-F]{3,8})["\']')
# layeredimage 块内：always: / attribute X [default]: 的成员图
_LAYER_ALWAYS_RE = re.compile(r'^always\s*:\s*$')
_LAYER_ATTR_RE = re.compile(r'^attribute\s+(\w+)')
_LAYER_MEMBER_RE = re.compile(r'^["\']([^"\']+)["\']\s*$')
# define e = Character("Eileen", image="eileen")
_CHAR_IMAGE_RE = re.compile(
    r'^(?:define\s+)?(\w+)\s*=\s*(?:Dynamic)?Character\s*\([^)]*?'
    r'image\s*=\s*["\']([\w ]+)["\']')

# transform 静态属性表（合成器按它摆放立绘——此前只有 8 个内置名，
# 自定义 transform 全部回退中心导致立绘错位/残缺）
_TRANSFORM_RE = re.compile(r'^transform\s+(\w+)')
_TRANSFORM_PROP_RE = re.compile(
    r'^(zoom|xzoom|yzoom|xalign|yalign|xpos|ypos|xanchor|yanchor'
    r'|xcenter|ycenter|xoffset|yoffset)\s+(-?[\d.]+)')
# 动画语句：transform 块内遇到即停止采集（静帧取块首状态）
_TRANSFORM_ANIM_RE = re.compile(
    r'^(linear|ease\w*|pause|choice|repeat|parallel|block|contains'
    r'|function|on|show|event|time|warp|circles|clockwise'
    r'|counterclockwise|frames|animation|delay|voice)\b')
# 游戏画面尺寸（define config.screen_width = 1920）
_SCREEN_SIZE_RE = re.compile(
    r'^define\s+config\.screen_(width|height)\s*=\s*(\d+)')

# Ren'Py 内置 transform 的静态属性（pos/anchor 为画面比例，zoom 缩放）
BUILTIN_TRANSFORMS: dict = {
    'left': {'xalign': 0.0, 'yalign': 1.0},
    'right': {'xalign': 1.0, 'yalign': 1.0},
    'center': {'xalign': 0.5, 'yalign': 0.5},
    'truecenter': {'xalign': 0.5, 'yalign': 0.5},
    'top': {'xalign': 0.5, 'yalign': 0.0},
    'topleft': {'xalign': 0.0, 'yalign': 0.0},
    'topright': {'xalign': 1.0, 'yalign': 0.0},
    'offscreenleft': {'xalign': 0.0, 'yalign': 1.0, 'xanchor': 1.0},
    'offscreenright': {'xalign': 1.0, 'yalign': 1.0, 'xanchor': 0.0},
}

# show at 位置 → 横向锚点（画面宽的比例，保留向后兼容）
_X_ANCHOR = {
    'left': 0.22, 'right': 0.78, 'center': 0.5, 'truecenter': 0.5,
    'topleft': 0.22, 'topright': 0.78, 'offscreenleft': 0.0,
    'offscreenright': 1.0,
}

THUMB_SIZE = (960, 540)
# 缩略图圆角（源图 960 宽上的像素）。前端边框 path 的圆角按同一比例
# （26/960）定义，二者随卡片等比缩放永远重合
THUMB_RADIUS = 26


# 立绘回退黑名单：路径含这些组件的图不是人物（房间/地图/界面），
# 只在没有任何竖幅候选时才考虑 cg 目录的横图（角色出演的 CG）
_AVATAR_DIR_BLACKLIST = {'bg', 'background', 'backgrounds', 'map', 'maps',
                         'location', 'locations', 'room', 'rooms', 'ui',
                         'icons', 'gui', 'tutorial_images'}


class LayeredImageDef:
    """layeredimage 定义：always 层 + 属性 → 分层图（show tag a b 时按
    属性拼层合成）"""

    def __init__(self):
        self.always: list = []          # 组件图名列表（空格分隔）
        self.attrs: dict = {}           # 属性 token → 组件图名


class ImageResolver:
    """图像名 → 文件路径 解析器（一次构建，全项目复用）"""

    def __init__(self, source_root: str):
        """source_root: game/game 所在目录（与 SourceTree 基准一致）"""
        self.root = Path(source_root)
        self._auto: dict[tuple, Path] = {}   # 组件元组（含目录） → 文件
        self._stem: dict[tuple, Path] = {}   # 仅文件名组件 → 文件（分层图成员）
        self._explicit: dict[tuple, Path] = {}
        self._solids: dict[tuple, str] = {}  # image x = Solid("#000")
        # 完整图像名（组件元组，小写）→ layeredimage 定义；多词名
        # （layeredimage k anal finger auto:）按全名匹配，避免首词撞名
        self._layered: dict[tuple, LayeredImageDef] = {}
        self.char_image_tags: dict[str, str] = {}  # 角色变量 → image tag
        # transform 名 → 静态属性（_build_transforms 采集）
        self._transforms: dict[str, dict] = {}
        # 游戏画面尺寸（config.screen_width/height，默认 1920x1080）
        self.screen_w = 1920
        self.screen_h = 1080
        self._build_explicit()
        self._build_transforms()
        self._build_auto()

    # ---- 索引构建 ----

    def _build_explicit(self):
        tree = SourceTree(str(self.root))
        for rel in tree.files():
            lines = tree.lines(rel)
            block_name = None       # image x: 块的组件名
            block_indent = -1
            layered: Optional[LayeredImageDef] = None
            layer_pending = None    # 'always' | 属性 token，等下一行图名
            for raw in lines:
                stripped = raw.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                indent = len(raw) - len(raw.lstrip())
                if block_name is not None and indent <= block_indent:
                    block_name = None
                    layered = None
                    layer_pending = None
                if block_name is not None:
                    # layeredimage 块：always/attribute 成员
                    if layered is not None:
                        if layer_pending is not None:
                            m = _LAYER_MEMBER_RE.match(stripped)
                            if m:
                                if layer_pending == 'always':
                                    layered.always.append(m.group(1))
                                else:
                                    layered.attrs[layer_pending] = m.group(1)
                                layer_pending = None
                            continue
                        if _LAYER_ALWAYS_RE.match(stripped):
                            layer_pending = 'always'
                            continue
                        m = _LAYER_ATTR_RE.match(stripped)
                        if m:
                            layer_pending = m.group(1).lower()
                            continue
                    # image x: 块：首个图片路径字面量作为该图像的文件
                    if block_name not in self._explicit:
                        q = _QUOTED_IMG_RE.search(stripped)
                        if q:
                            self._set_explicit(block_name, q.group(1))
                    continue
                m = _IMAGE_DEF_RE.match(stripped)
                # 不限顶层：有的游戏把 image 定义写在 label/init 块内（运行时定义）
                if m:
                    name = tuple(m.group(1).split())
                    s = _SOLID_RE.search(m.group(2))
                    if s:
                        self._solids[tuple(t.lower() for t in name)] = \
                            s.group(1)
                        continue
                    q = _QUOTED_IMG_RE.search(m.group(2))
                    if q:
                        self._set_explicit(name, q.group(1))
                    continue
                m = _IMAGE_BLOCK_RE.match(stripped)
                if m:
                    block_name = tuple(m.group(1).split())
                    block_indent = indent
                    if stripped.startswith('layeredimage'):
                        layered = LayeredImageDef()
                        self._layered.setdefault(
                            tuple(t.lower() for t in block_name), layered)
                    continue
                m = _CHAR_IMAGE_RE.match(stripped)
                if m:
                    self.char_image_tags.setdefault(m.group(1), m.group(2))

    def _build_transforms(self):
        """transform 块静态属性采集（静帧取块首，动画行即停）；
        顺带读游戏画面尺寸（config.screen_width/height）"""
        tree = SourceTree(str(self.root))
        for rel in tree.files():
            cur_name = None
            cur_props = None
            block_indent = -1
            for raw in tree.lines(rel):
                stripped = raw.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                indent = len(raw) - len(raw.lstrip())
                if cur_name is not None and indent <= block_indent:
                    if cur_props:
                        self._transforms.setdefault(cur_name, cur_props)
                    cur_name = None
                    cur_props = None
                if cur_name is not None:
                    if _TRANSFORM_ANIM_RE.match(stripped):
                        # 动画开始：静帧只取此前的常量状态
                        if cur_props:
                            self._transforms.setdefault(cur_name, cur_props)
                        cur_name = None
                        cur_props = None
                        continue
                    m = _TRANSFORM_PROP_RE.match(stripped)
                    if m:
                        cur_props[m.group(1)] = float(m.group(2))
                    continue
                m = _SCREEN_SIZE_RE.match(stripped)
                if m:
                    if m.group(1) == 'width':
                        self.screen_w = int(m.group(2))
                    else:
                        self.screen_h = int(m.group(2))
                    continue
                m = _TRANSFORM_RE.match(stripped)
                if m and stripped.endswith(':'):
                    cur_name = m.group(1)
                    cur_props = {}
                    block_indent = indent
            if cur_name is not None and cur_props:
                self._transforms.setdefault(cur_name, cur_props)

    def _set_explicit(self, name: tuple, rel_path: str):
        # Ren'Py 定义路径按 game/ 相对，但自动搜索 images/ 子目录——
        # 两种位置都试（bg/black.png → images/bg/black.png 常见）
        key = tuple(t.lower() for t in name)
        for base in (self.root, self.root / 'images'):
            p = base / rel_path
            if p.exists():
                self._explicit.setdefault(key, p)
                return

    def _build_auto(self):
        for p in self.root.rglob('*'):
            if not p.is_file() or p.suffix.lower() not in _IMAGE_EXTS:
                continue
            rel = p.relative_to(self.root)
            if _AUTO_EXCLUDE & set(rel.parts):
                continue
            parts = [t.lower() for t in rel.parts[:-1]] + [rel.stem.lower()]
            if parts and parts[0] == 'images':
                parts = parts[1:]
            if not parts:
                continue
            # 文件名按空格/下划线拆组件（Ren'Py 自动图像命名规则）
            name = []
            for tok in parts[:-1]:
                name.extend(t for t in re.split(r'[ _]+', tok) if t)
            name.extend(t for t in re.split(r'[ _]+', parts[-1]) if t)
            self._auto.setdefault(tuple(name), p)
            # 同时保留原始下划线形式（show q_1 对应 q_1.webp 不拆分时）
            raw = [t.lower() for t in rel.parts[:-1]] + [rel.stem.lower()]
            if raw and raw[0] == 'images':
                raw = raw[1:]
            self._auto.setdefault(tuple(raw), p)
            # 仅文件名组件索引：layeredimage 成员（"a base" → sprites/a/
            # a_base.png）等按名字找文件的场景，目录前缀会污染组件名
            stem_key = tuple(t for t in re.split(r'[ _]+', parts[-1]) if t)
            if stem_key and stem_key not in self._auto:
                self._stem.setdefault(stem_key, p)

    # ---- 解析 ----

    def resolve(self, images: list) -> Optional[Path]:
        """按图像名组件列表解析文件；多策略降级"""
        if not images:
            return None
        key = tuple(t.lower() for t in images)
        if key in self._explicit:
            return self._explicit[key]
        if key in self._auto:
            return self._auto[key]
        # 下划线合并（show q 1 → q_1.webp）
        joined = ('_'.join(key),)
        if joined in self._auto:
            return self._auto[joined]
        # 单组件按下划线拆开（show q_1 → images/q/1.webp）
        if len(key) == 1 and '_' in key[0]:
            split = tuple(t for t in key[0].split('_') if t)
            if split in self._auto:
                return self._auto[split]
            if split in self._stem:
                return self._stem[split]
        # 仅文件名组件（layeredimage 成员 "a base" → a_base.png）
        if key in self._stem:
            return self._stem[key]
        return None

    def _resolve_member(self, name: str) -> Optional[Path]:
        """layeredimage 成员：图像名（"a base"）或相对路径
        （"sprites/a/a_base.png"）"""
        p = self.resolve(name.split())
        if p:
            return p
        if '/' in name or '.' in name:
            for base in (self.root / 'images', self.root):
                q = base / name
                if q.exists():
                    return q
        return None

    def resolve_composite(self, images: list) -> list[Path]:
        """解析为图层文件列表：layeredimage 按最长前缀匹配名字（剩余
        token 为属性拼层），普通图像返回单文件（无可解析返回 []）"""
        if not images:
            return []
        key = tuple(t.lower() for t in images)
        if key in self._explicit or key in self._auto:
            p = self.resolve(images)
            return [p] if p else []
        # 最长前缀命中 layeredimage 定义（show k train anime →
        # k train anime / k train / k 逐个尝试，剩余 token 为属性）
        for plen in range(len(key), 0, -1):
            lid = self._layered.get(key[:plen])
            if lid is None:
                continue
            names = list(lid.always)
            for tok in key[plen:]:
                if tok in lid.attrs:
                    names.append(lid.attrs[tok])
            out = []
            for n in names:
                p = self._resolve_member(n)
                if p:
                    out.append(p)
            if out:
                return out
        p = self.resolve(images)
        return [p] if p else []

    def resolve_tag_variants(self, tag: str) -> list:
        """某 tag 的全部自动图像，按匹配位置靠前、组件数少排序

        tag 可匹配任意路径组件：images/ 下的 cleo/1/1.webp 在首组件，
        characters/aleta/aleta_main.webp 在中间组件（角色子目录命名）。
        """
        tag = tag.lower()
        out = []
        for name, p in self._auto.items():
            for pos, comp in enumerate(name):
                if comp == tag:
                    out.append(((pos, len(name)), p))
                    break
            else:
                continue
        out.sort(key=lambda x: x[0])
        return [p for _, p in out]

    def _char_tag_candidates(self, variable: str, display_name: str) -> list:
        """角色可能的图像 tag 候选（特异性从高到低）"""
        candidates = []
        tag = self.char_image_tags.get(variable)
        if tag:
            candidates.append(tag)
            candidates.append(tag.split()[0])
        if variable:
            candidates.append(variable.lower())
        name = display_name.lower()
        if name.startswith('[') and name.endswith(']'):
            inner = name[1:-1]
            stem = re.sub(r'_name$|_me$|^c_', '', inner)
            candidates.append(stem)
            candidates.extend(p for p in inner.split('_') if p)
        elif name:
            candidates.append(name.replace(' ', '_'))
            candidates.append(name)
            parts = name.split()
            if len(parts) > 1:
                candidates.append(parts[-1])   # 末词（Demon Cherry → cherry）
        if variable and '_' in variable:
            candidates.extend(p for p in variable.lower().split('_') if p)
        seen = set()
        return [c for c in candidates if c and not seen.add(c)]

    def resolve_char_sprite(self, variable: str,
                            display_name: str = '') -> Optional[Path]:
        """角色立绘：Character(image=) tag → 变量名 → 显示名

        竖幅优先；无竖幅时回退到 cg 目录的横图（角色出演的 CG，
        裁顶部方形当头像比没有强）；房间/地图/界面目录永不作立绘
        """
        cands = self.avatar_candidates(variable, display_name)
        return cands[0] if cands else None

    def avatar_candidates(self, variable: str,
                          display_name: str = '') -> list:
        """角色立绘候选图（竖幅优先 + cg 横图回退，不限数量）"""
        for c in self._char_tag_candidates(variable, display_name):
            tag = self.char_image_tags.get(variable)
            if tag and c == tag:
                r = self.resolve(tag.split())
                if r and self._is_portrait(r):
                    return [r]
            variants = self.resolve_tag_variants(c)
            if not variants:
                continue
            portrait = [v for v in variants if self._is_portrait(v)]
            if portrait:
                return portrait
            cg = [v for v in variants
                  if self._avatar_dir_ok(v) and not self._is_portrait(v)]
            if cg:
                return cg
        return []

    def _avatar_dir_ok(self, path: Path) -> bool:
        """横图回退的目录判定：黑名单（bg/map/ui/room/教程）永不作立绘；
        cg/事件目录（afterparty、mag…）的横图是角色出演的 CG，可作头像"""
        parts = {p.lower() for p in path.parts}
        if parts & _AVATAR_DIR_BLACKLIST:
            return False
        # background_images / bg2 这类变体名按前缀挡
        return not any(p.startswith('background') for p in parts)

    @staticmethod
    def _is_portrait(path: Path) -> bool:
        """竖幅/近方判定（w/h ≤ 4:3）：立绘多竖幅或近方
        （632×620 这类），16:9 背景（1.78）被排除；
        读取失败时放行（宁多勿漏）"""
        try:
            with Image.open(path) as im:
                w, h = im.size
                return h * 4 >= w * 3
        except OSError:
            return True


# ========== 场景状态机与合成 ==========

class _SceneState:
    """scene/show/hide 语句的简化状态机（tag = 图像名首组件，同 tag 替换）"""

    def __init__(self):
        self.bg = None            # tuple(images) 底图
        self.layers = {}          # tag -> (images, at)

    def apply(self, op) -> bool:
        """应用一条 SceneOp，返回是否有可视变化"""
        if op.dynamic or not op.images:
            return False
        if op.op == 'scene':
            self.bg = tuple(op.images)
            self.layers.clear()
            return True
        if op.op == 'show':
            self.layers[op.images[0].lower()] = (tuple(op.images), op.at)
            return True
        if op.op == 'hide':
            self.layers.pop(op.images[0].lower(), None)
            return True
        return False

    def snapshot(self) -> '_SceneState':
        s = _SceneState()
        s.bg = self.bg
        s.layers = dict(self.layers)
        return s

    def empty(self) -> bool:
        return self.bg is None and not self.layers


def _x_anchor(at: list) -> float:
    for tok in at:
        if tok in _X_ANCHOR:
            return _X_ANCHOR[tok]
    return 0.5


def _sprite_geometry(resolver: 'ImageResolver', at: list,
                     iw: int, ih: int, cw: int, ch: int):
    """at 列表 → 立绘几何 (w, h, x, y)

    Ren'Py 语义：无 transform 时图像按原始尺寸显示（缩放比 =
    画布/游戏画面）；transform 的 zoom 乘算、pos/align/anchor 决定位置。
    未知 transform 全部忽略（此前回退中心是立绘错位残缺的主因之一，
    现在 _build_transforms 已采集自定义 transform 的静态属性）。
    无任何已知 transform 时保留旧启发式（底部居中、撑高 96%）——
    没有位置信息的立绘这样看起来最合理。
    """
    known = False
    zoom = xzm = yzm = 1.0
    xp, yp = 0.5, 1.0      # pos（画面比例）
    xa, ya = 0.5, 1.0      # anchor（图像比例）
    xo = yo = 0.0          # 像素偏移（游戏坐标）
    for tok in at:
        t = resolver._transforms.get(tok) or BUILTIN_TRANSFORMS.get(tok)
        if t is None:
            continue
        known = True
        zoom *= t.get('zoom', 1.0)
        xzm *= t.get('xzoom', 1.0)
        yzm *= t.get('yzoom', 1.0)
        if 'xalign' in t:
            xp = xa = t['xalign']
        if 'yalign' in t:
            yp = ya = t['yalign']
        if 'xpos' in t:
            xp = t['xpos'] / resolver.screen_w
        if 'ypos' in t:
            yp = t['ypos'] / resolver.screen_h
        if 'xanchor' in t:
            v = t['xanchor']
            xa = v / iw if v > 1 else v
        if 'yanchor' in t:
            v = t['yanchor']
            ya = v / ih if v > 1 else v
        if 'xcenter' in t:
            xp, xa = t['xcenter'] / resolver.screen_w, 0.5
        if 'ycenter' in t:
            yp, ya = t['ycenter'] / resolver.screen_h, 0.5
        xo += t.get('xoffset', 0.0)
        yo += t.get('yoffset', 0.0)
    if not known:
        # 旧启发式：底部居中撑高
        scale = (ch * 0.96) / ih
        w = max(1, round(iw * scale))
        h = max(1, round(ih * scale))
        return w, h, round(cw * 0.5 - w / 2), ch - h
    base = ch / resolver.screen_h
    w = max(1, round(iw * base * zoom * xzm))
    h = max(1, round(ih * base * zoom * yzm))
    x = round(cw * xp - w * xa + xo * base)
    y = round(ch * yp - h * ya + yo * base)
    return w, h, x, y


def _paste_cover(canvas: Image.Image, img: Image.Image):
    """背景：等比缩放铺满画布并居中裁剪"""
    _paste_cover_layers(canvas, [img])


def _paste_cover_layers(canvas: Image.Image, imgs: list):
    """背景图层组：首层铺满裁剪，后续层按同一变换对齐叠加
    （layeredimage 各层同尺寸、绝对定位）"""
    cw, ch = canvas.size
    first = imgs[0].convert('RGBA')
    iw, ih = first.size
    scale = max(cw / iw, ch / ih)
    sw = max(1, round(iw * scale))
    sh = max(1, round(ih * scale))
    stage = Image.new('RGBA', (sw, sh), (0, 0, 0, 0))
    for im in imgs:
        im = im.convert('RGBA')
        if im.size != (sw, sh):
            im = im.resize((sw, sh), Image.LANCZOS)
        stage.alpha_composite(im)
    x0 = (sw - cw) // 2
    y0 = (sh - ch) // 2
    canvas.paste(stage.crop((x0, y0, x0 + cw, y0 + ch)), (0, 0))


def _paste_sprite_layers(canvas: Image.Image, imgs: list,
                         geom: tuple):
    """立绘图层组：geom=(w,h,x,y) 由 _sprite_geometry 给出，
    越界部分按画布裁剪"""
    if not imgs:
        return
    cw, ch = canvas.size
    w, h, x, y = geom
    # 画布外交集裁剪（负坐标/溢出）
    sx0 = max(0, -x)
    sy0 = max(0, -y)
    dx0 = max(0, x)
    dy0 = max(0, y)
    pw = min(w - sx0, cw - dx0)
    ph = min(h - sy0, ch - dy0)
    if pw <= 0 or ph <= 0:
        return
    for im in imgs:
        im = im.convert('RGBA')
        if im.size != (w, h):
            im = im.resize((w, h), Image.LANCZOS)
        im = im.crop((sx0, sy0, sx0 + pw, sy0 + ph))
        canvas.alpha_composite(im, (dx0, dy0))


def _mean_brightness(canvas: Image.Image) -> float:
    small = canvas.convert('L').resize((24, 14))
    return sum(small.getdata()) / (24 * 14)  # noqa: B905


# 亮度低于该值视为"全黑废图"（scene bg black 占位），触发重取
_DARK_THRESHOLD = 12.0

# 合成器版本：渲染逻辑变更（圆角/传播/取点策略）时 +1，使旧缓存失效
COMPOSITOR_VERSION = 3


def state_after(scene_ops: list, initial_state=None) -> '_SceneState':
    """不重渲染地算 label 体最终画面状态（缓存命中时的传播用）"""
    cur = initial_state.snapshot() if initial_state else _SceneState()
    for op in scene_ops:
        cur.apply(op)
    return cur


def thumb_key(scene_ops: list, first_dlg_line: int, last_dlg_line: int,
              initial_state=None) -> str:
    """缩略图内容哈希：ops/取点/初始状态/合成器版本全部参与，
    任一变化即重渲染（重建时未变的 label 直接复用缓存文件）"""
    import hashlib

    def _state_sig(st) -> str:
        if st is None:
            return ''
        layers = sorted(
            (tuple(i), tuple(a)) for i, a in st.layers.values())
        return repr((st.bg, layers))

    h = hashlib.sha1()
    h.update(str(COMPOSITOR_VERSION).encode())
    h.update(repr([(o.line, o.op, tuple(o.images), tuple(o.at), o.dynamic)
                   for o in scene_ops]).encode())
    h.update(f'{first_dlg_line}:{last_dlg_line}'.encode())
    h.update(_state_sig(initial_state).encode())
    return h.hexdigest()[:16]


def compose_scene_candidates(resolver: ImageResolver, scene_ops: list,
                             first_dlg_line: int, last_dlg_line: int,
                             out_dir: str, stem: str,
                             size: tuple = THUMB_SIZE,
                             initial_state=None) -> tuple:
    """合成节点场景缩略图候选（多帧）

    画面状态的多候选选择：intro 幻灯/对话间换图的内容出现在台词之后，
    而场景结尾常淡出为纯黑——单一取点必然得到一批全黑废图。
    按取态点 [末条台词 → 首尾台词中点 → 首条台词 → 最终态] 各渲染一帧
    （去重后），首个亮度达标的排最前，其余按亮度降序跟在后面；
    全部写盘（{stem}.webp、{stem}_2.webp …）并按序返回文件名。
    无任何可解析图像返回 []。

    initial_state：进入本 label 时的画面状态——Ren'Py 的画面跨 label
    持续，只有 show 没有 scene 的 label 继承上一 label 的背景（调用方
    按剧情图前驱传播）。返回 (文件名列表, 最终状态) 供链式传播。
    """
    mid_line = (first_dlg_line + last_dlg_line) // 2 if first_dlg_line else 0
    stops = []  # 取态行号；0 = 最终态
    for line in (last_dlg_line, mid_line, first_dlg_line, 0):
        if line not in stops:
            stops.append(line)
    if len(stops) <= 2 and scene_ops:
        # 无台词/单取点的 label：按场景语句位置补取点（ evenly 取至多 3 个
        # 中间态），否则候选永远只有最终态一帧
        lines = sorted(o.line for o in scene_ops)
        n = len(lines)
        for i in (n // 4, n // 2, 3 * n // 4):
            if 0 < i < n and lines[i] not in stops:
                stops.append(lines[i])

    # 单遍应用，按取态行号快照状态（mark 处的状态 = 行号 ≤ mark 的
    # 全部语句应用后；不能含 mark 之后的 scene（slideshow 里会清掉立绘））
    marks = sorted(l for l in stops if l)
    states: dict[int, _SceneState] = {}
    cur = initial_state.snapshot() if initial_state else _SceneState()
    mi = 0
    for op in scene_ops:
        while mi < len(marks) and op.line > marks[mi]:
            states[marks[mi]] = cur.snapshot()
            mi += 1
        cur.apply(op)
    while mi < len(marks):
        states[marks[mi]] = cur.snapshot()
        mi += 1
    states[0] = cur  # 最终态

    frames = []       # (亮度, canvas)，按取点顺序
    for line in stops:
        state = states.get(line)
        if state is None or state.empty():
            continue
        canvas = _render(resolver, state, size)
        if canvas is None:
            continue
        frames.append((_mean_brightness(canvas), canvas))
    if not frames:
        return [], cur

    # 首个达标帧优先；其余按亮度降序（去暗废帧）
    first_ok = next((i for i, (b, _) in enumerate(frames)
                     if b >= _DARK_THRESHOLD), None)
    if first_ok is None:
        ordered = sorted(frames, key=lambda f: -f[0])
    else:
        rest = sorted((f for i, f in enumerate(frames) if i != first_ok),
                      key=lambda f: -f[0])
        ordered = [frames[first_ok]] + rest

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    names = []
    for i, (_, canvas) in enumerate(ordered):
        name = f'{stem}.webp' if i == 0 else f'{stem}_{i + 1}.webp'
        _rounded(canvas.convert('RGB')).save(out / name, 'WEBP', quality=80)
        names.append(name)
    return names, cur


def _rounded(img: Image.Image) -> Image.Image:
    """烘透明圆角（与前端边框 path 圆角同比例，随卡片等比缩放重合）"""
    from PIL import ImageDraw

    mask = Image.new('L', img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, img.width - 1, img.height - 1), radius=THUMB_RADIUS, fill=255)
    img = img.convert('RGBA')
    img.putalpha(mask)
    return img


def compose_scene(resolver: ImageResolver, scene_ops: list,
                  first_dlg_line: int, last_dlg_line: int, out_path: str,
                  size: tuple = THUMB_SIZE) -> bool:
    """合成单张节点缩略图（多帧择优，详见 compose_scene_candidates）"""
    out = Path(out_path)
    names, _ = compose_scene_candidates(
        resolver, scene_ops, first_dlg_line, last_dlg_line,
        str(out.parent), out.stem, size)
    if not names:
        return False
    if names[0] != out.name:
        src = out.parent / names[0]
        if out.exists():
            out.unlink()
        src.rename(out)
    return True


def _render(resolver: ImageResolver, state: '_SceneState',
            size: tuple) -> Optional[Image.Image]:
    """按场景状态拼合画面；无可绘制图像返回 None"""
    canvas = Image.new('RGBA', size, (24, 24, 28, 255))
    drew = False
    if state.bg is not None:
        solid = resolver._solids.get(tuple(t.lower() for t in state.bg))
        if solid:
            canvas.paste(Image.new('RGBA', size, (*_hex_rgb(solid), 255)),
                         (0, 0))
            drew = True
        else:
            layers = resolver.resolve_composite(list(state.bg))
            if layers:
                try:
                    with Image.open(layers[0]) as first:
                        imgs = [first.convert('RGBA')]
                        for p in layers[1:]:
                            with Image.open(p) as im:
                                imgs.append(im.convert('RGBA'))
                    _paste_cover_layers(canvas, imgs)
                    drew = True
                except OSError:
                    pass
    # 立绘按 x 锚点排序，从左到右叠加
    ordered = sorted(state.layers.values(),
                     key=lambda la: _x_anchor(la[1]))
    for images, at in ordered:
        layers = resolver.resolve_composite(list(images))
        if not layers:
            continue
        try:
            imgs = []
            for p in layers:
                with Image.open(p) as im:
                    imgs.append(im.convert('RGBA'))
            iw, ih = imgs[0].size
            geom = _sprite_geometry(resolver, at, iw, ih,
                                    canvas.width, canvas.height)
            _paste_sprite_layers(canvas, imgs, geom)
            drew = True
        except OSError:
            continue
    return canvas if drew else None


def _hex_rgb(color: str) -> tuple:
    color = color.lstrip('#')
    if len(color) == 3:
        color = ''.join(c * 2 for c in color)
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))
