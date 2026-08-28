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
# define e = Character("Eileen", image="eileen")
_CHAR_IMAGE_RE = re.compile(
    r'^(?:define\s+)?(\w+)\s*=\s*(?:Dynamic)?Character\s*\([^)]*?'
    r'image\s*=\s*["\']([\w ]+)["\']')

# show at 位置 → 横向锚点（画面宽的比例）
_X_ANCHOR = {
    'left': 0.22, 'right': 0.78, 'center': 0.5, 'truecenter': 0.5,
    'topleft': 0.22, 'topright': 0.78, 'offscreenleft': 0.0,
    'offscreenright': 1.0,
}

THUMB_SIZE = (960, 540)


class ImageResolver:
    """图像名 → 文件路径 解析器（一次构建，全项目复用）"""

    def __init__(self, source_root: str):
        """source_root: game/game 所在目录（与 SourceTree 基准一致）"""
        self.root = Path(source_root)
        self._auto: dict[tuple, Path] = {}   # 组件元组 → 文件
        self._explicit: dict[tuple, Path] = {}
        self.char_image_tags: dict[str, str] = {}  # 角色变量 → image tag
        self._build_explicit()
        self._build_auto()

    # ---- 索引构建 ----

    def _build_explicit(self):
        tree = SourceTree(str(self.root))
        for rel in tree.files():
            lines = tree.lines(rel)
            block_name = None       # image x: 块的组件名
            block_indent = -1
            for raw in lines:
                stripped = raw.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                indent = len(raw) - len(raw.lstrip())
                if block_name is not None and indent <= block_indent:
                    block_name = None
                if block_name is not None:
                    # 块内首个图片路径字面量作为该图像的文件（always/属性层不细分）
                    if block_name not in self._explicit:
                        q = _QUOTED_IMG_RE.search(stripped)
                        if q:
                            self._set_explicit(block_name, q.group(1))
                    continue
                m = _IMAGE_DEF_RE.match(stripped)
                # 不限顶层：有的游戏把 image 定义写在 label/init 块内（运行时定义）
                if m:
                    name = tuple(m.group(1).split())
                    q = _QUOTED_IMG_RE.search(m.group(2))
                    if q:
                        self._set_explicit(name, q.group(1))
                    continue
                m = _IMAGE_BLOCK_RE.match(stripped)
                if m:
                    block_name = tuple(m.group(1).split())
                    block_indent = indent
                    continue
                m = _CHAR_IMAGE_RE.match(stripped)
                if m:
                    self.char_image_tags.setdefault(m.group(1), m.group(2))

    def _set_explicit(self, name: tuple, rel_path: str):
        p = (self.root / rel_path)
        if p.exists():
            self._explicit.setdefault(tuple(t.lower() for t in name), p)

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
        return None

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

    def resolve_char_sprite(self, variable: str,
                            display_name: str = '') -> Optional[Path]:
        """角色立绘：Character(image=) tag → 变量名 → 显示名，取最基础变体

        候选 tag 覆盖常见命名习惯：
        - 变量名整体/下划线分段（lady_c → c、lady）
        - 显示名整体/末词（Demon Cherry → cherry）
        - 动态名提取（[cleo_name] → cleo）
        """
        candidates = []
        tag = self.char_image_tags.get(variable)
        if tag:
            r = self.resolve(tag.split())
            if r:
                return r
            candidates.append(tag.split()[0])
        # 特异性从高到低：变量全名 → 显示名（含下划线形）→ 名称末词 →
        # 动态名词干 → 变量分段（单字母段最易误匹配，排最后）
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
        for c in candidates:
            if not c or c in seen:
                continue
            seen.add(c)
            variants = self.resolve_tag_variants(c)
            if variants:
                return variants[0]
        return None


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


def _paste_cover(canvas: Image.Image, img: Image.Image):
    """背景：等比缩放铺满画布并居中裁剪"""
    cw, ch = canvas.size
    iw, ih = img.size
    scale = max(cw / iw, ch / ih)
    img = img.resize((max(1, round(iw * scale)), max(1, round(ih * scale))),
                     Image.LANCZOS)
    x = (img.width - cw) // 2
    y = (img.height - ch) // 2
    canvas.paste(img.crop((x, y, x + cw, y + ch)), (0, 0))


def _paste_sprite(canvas: Image.Image, img: Image.Image, x_frac: float):
    """立绘：按画布高度等比缩放，底部对齐，横向锚点居中"""
    cw, ch = canvas.size
    iw, ih = img.size
    scale = (ch * 0.96) / ih
    w = max(1, round(iw * scale))
    h = max(1, round(ih * scale))
    img = img.resize((w, h), Image.LANCZOS)
    x = round(cw * x_frac - w / 2)
    y = ch - h
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    canvas.paste(img, (x, y), img)


def _mean_brightness(canvas: Image.Image) -> float:
    small = canvas.convert('L').resize((24, 14))
    return sum(small.getdata()) / (24 * 14)  # noqa: B905


# 亮度低于该值视为"全黑废图"（scene bg black 占位），触发重取
_DARK_THRESHOLD = 12.0


def compose_scene(resolver: ImageResolver, scene_ops: list,
                  first_dlg_line: int, last_dlg_line: int, out_path: str,
                  size: tuple = THUMB_SIZE) -> bool:
    """合成节点场景缩略图

    画面状态的多候选选择：intro 幻灯/对话间换图的内容出现在台词之后，
    而场景结尾常淡出为纯黑——单一取点必然得到一批全黑废图。
    因此按优先级 [末条台词 → 首尾台词中点 → 首条台词 → 最终态] 逐个
    取态渲染，首个亮度达标的采用；全不达标取最亮的一帧。
    无任何可解析图像返回 False（调用方不生成缩略图）。
    """
    mid_line = (first_dlg_line + last_dlg_line) // 2 if first_dlg_line else 0
    stops = []  # (优先级顺序, 取态行号)；0 = 最终态
    for line in (last_dlg_line, mid_line, first_dlg_line, 0):
        if line not in stops:
            stops.append(line)

    # 单遍应用，按取态行号快照状态（mark 处的状态 = 行号 ≤ mark 的
    # 全部语句应用后；不能含 mark 之后的 scene（slideshow 里会清掉立绘））
    marks = sorted(l for l in stops if l)
    states: dict[int, _SceneState] = {}
    cur = _SceneState()
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

    best = None       # (亮度, canvas)
    for line in stops:
        state = states.get(line)
        if state is None or state.empty():
            continue
        canvas = _render(resolver, state, size)
        if canvas is None:
            continue
        bright = _mean_brightness(canvas)
        if best is None or bright > best[0]:
            best = (bright, canvas)
        if bright >= _DARK_THRESHOLD:
            break
    if best is None:
        return False

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    best[1].save(out, 'WEBP', quality=80)
    return True


def _render(resolver: ImageResolver, state: '_SceneState',
            size: tuple) -> Optional[Image.Image]:
    """按场景状态拼合画面；无可绘制图像返回 None"""
    canvas = Image.new('RGB', size, (24, 24, 28))
    drew = False
    if state.bg is not None:
        p = resolver.resolve(list(state.bg))
        if p:
            try:
                with Image.open(p) as im:
                    _paste_cover(canvas, im.convert('RGB'))
                drew = True
            except OSError:
                pass
    # 立绘按 x 锚点排序，从左到右叠加
    ordered = sorted(state.layers.values(),
                     key=lambda la: _x_anchor(la[1]))
    for images, at in ordered:
        p = resolver.resolve(list(images))
        if not p:
            continue
        try:
            with Image.open(p) as im:
                _paste_sprite(canvas, im, _x_anchor(at))
            drew = True
        except OSError:
            continue
    return canvas if drew else None
