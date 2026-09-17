"""Ren'Py 渲染沙盒（实验性功能中的实验性功能，开关控制）

用游戏自己的引擎渲染场景缩略图——真 ATL/真 layeredimage/真程序化
角色（LR2 的 draw_person 这类引擎换装，Pillow 合成器在原理上就做不到）。

架构：
1. 驱动脚本写入目标游戏源码根（zz_sandbox_*.rpy，随用随清）：
   - 游戏脚本正常加载（图像/layeredimage/transform/init 角色对象全生效）
   - 驱动定义 `_start` label：Ren'Py 入口优先 _start 于 start
     （7.x/8.x 一致），游戏的 start 不冲突、不被执行
2. renpy.exe 跑游戏项目：逐 label 回放 scene/show/hide/exec，取态点
   screenshot_to_bytes((960,540)) 落盘 PNG
3. 主流程回收 PNG → 烘圆角存 webp，复用既有候选/缓存管线

实验性边界：引擎渲染逐游戏 quirks 多（init 副作用、私有 API、缺图），
单 label 失败静默跳过；整个沙盒启动失败时调用方回退 Pillow 合成器。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from rt_home import find_resource

# 入口劫持（label_overrides 在 jump/call 时查询，见 renpy/script.py）
_REDIRECT_RPY = '''\
init -1 python:
    config.label_overrides['start'] = 'sandbox_entry'
'''

# 驱动脚本头部：助手函数。正文由 build_driver_script 按 jobs 生成真实
# Ren'Py 语句（pause 语句让引擎自然渲染一帧——7.x 截图要求主循环已启动，
# 单 python 块内 renpy.pause 会撞 widget/layer 栈）
_DRIVER_HEAD = '''\
init python:
    import json as _json
    import os as _os
    import io as _io

    _out_dir = _os.path.join(config.gamedir, "zz_sandbox_out")
    if not _os.path.isdir(_out_dir):
        _os.makedirs(_out_dir)
    # 禁掉快捷菜单（游戏的 quick_menu 屏会在 interact 时求值）
    quick_menu = False

    def _sb_at_list(at):
        out = []
        for tok in at:
            t = getattr(store, tok, None)
            if t is not None:
                out.append(t)
        return out

    def _sb_show(images, at, code=""):
        # 原文行先 exec（bg_manager 自建显示器/change_location 设置运行时
        # 状态）；名字解析照旧走——exec 失败时背景不丢，成功时也只是重复
        if code:
            _sb_exec(code)
        if not images:
            return
        name = " ".join(images)
        at_list = _sb_at_list(at)
        try:
            if renpy.has_image(name):
                renpy.show(name, at_list=at_list, layer="master")
                return
            # 无此图像名：按文件名直读（Room 背景这类文件路径名字）
            for prefix in ("images/", "images/background_images/", ""):
                for ext in (".png", ".jpg", ".jpeg", ".webp"):
                    fn = prefix + name + ext
                    if renpy.loadable(fn):
                        renpy.show(name, what=renpy.display.im.image(fn),
                                   at_list=at_list, layer="master")
                        return
        except Exception:
            pass

    def _sb_scene(images, at, code=""):
        try:
            renpy.scene(layer="master")
        except Exception:
            pass
        _sb_show(images, at, code)

    def _sb_hide(tag):
        try:
            renpy.hide(tag, layer="master")
        except Exception:
            pass

    def _sb_exec(code):
        if 'draw_person' in code:
            # 接收者必须是已实例化的角色——否则 draw_person 的 wipe_scene
            # 会把已渲染的背景清成灰屏（mc 这类占位对象也会造成误判，
            # 所以按接收者逐个检查而不是全局 Person 探测）
            recv = code.split('.')[0].strip()
            p = getattr(store, recv, None)
            if p is None \
                    or getattr(getattr(p, '__class__', None),
                               '__name__', '') != 'Person':
                return
        try:
            exec(code, store.__dict__)
        except Exception:
            pass

    def _sb_shot(name):
        try:
            data = renpy.screenshot_to_bytes((960, 540))
            if data:
                fn = _os.path.join(_out_dir, name + ".png")
                with _io.open(fn, "wb") as _f:
                    _f.write(data)
            else:
                print("SANDBOX_SHOT_NONE", name)
        except Exception as e:
            print("SANDBOX_SHOT_FAIL", name, repr(e)[:80])

    def _sb_frame():
        renpy.ui.saybehavior(dismiss='dismiss_hard_pause')
        renpy.ui.interact(mouse='pause', type='pause', roll_forward=None,
                          pause=0.05)

    def _sb_init_game():
        # 1) 游戏自己的角色实例化（instantiate_* label，缺失/报错跳过）
        for lb in ("instantiate_roles", "instantiate_personalities",
                   "instantiate_serum_traits",
                   "instantiate_side_effect_traits",
                   "instantiate_positions", "instantiate_outfits"):
            try:
                if renpy.game.script.has_label(lb):
                    renpy.call(lb)
            except Exception:
                pass
        # 2) 角色工厂表：LR2 这类游戏的定义模块把工厂函数注册进 store 的
        # list_of_instantiation_functions（游戏 start 逐个调用），函数
        # 本身也在 store。函数间有顺序依赖（血清基础→衍生），
        # 多趟调用至收敛（每趟成功的不再重试）
        try:
            fns = list(getattr(store, 'list_of_instantiation_functions',
                               None) or [])
        except Exception:
            fns = []
        done = set()
        for _pass in range(3):
            errs = 0
            for name in fns:
                if name in done:
                    continue
                fn = getattr(store, name, None)
                if callable(fn):
                    try:
                        fn()
                        done.add(name)
                    except Exception:
                        errs += 1
            if not errs:
                break
        # 3) 角色对象搬进 store 兜底：工厂在模块 global 绑定的角色，
        # 脚本 $ the_person = stephanie 需要 store 变量
        import sys as _sys
        for mod in list(_sys.modules.values()):
            d = getattr(mod, '__dict__', None)
            if not d:
                continue
            for k, v in list(d.items()):
                cls = getattr(v, '__class__', None)
                if cls is not None and cls.__name__ == 'Person':
                    try:
                        if not hasattr(store, k):
                            setattr(store, k, v)
                    except Exception:
                        pass

    def _sb_finish():
        with _io.open(_os.path.join(config.gamedir,
                                    "zz_sandbox_done.flag"), "w") as _f:
            _f.write("done")

# 入口劫持放最后（init 99）：游戏自己的 label_overrides 在 init 0 设置，
# 这里压过它。入口 lookup 时会查覆盖（renpy/script.py lookup 应用
# label_overrides），7.x/8.x 一致。splashscreen 也劫持——8.x 入口先跑
# splashscreen（LR2 主菜单路径），不劫持会卡死在菜单
init 99 python:
    config.label_overrides['start'] = 'zz_sandbox_entry'
    config.label_overrides['splashscreen'] = 'zz_sandbox_entry'

'''

_DRIVER_FILES = ('zz_sandbox_driver.rpy',
                 'zz_sandbox_jobs.json', 'zz_sandbox_done.flag')


def _sdk_exe(source_root: Path) -> Path:
    """按游戏引擎版本选 SDK：7.x 游戏必须 7.4 SDK（Python 2 语法），
    8.x/未知用 8.5 SDK；都没有时退 8.5"""
    from sdk_manager import detect_engine_version

    ver = None
    try:
        # 引擎在 项目根/renpy/（源码根是项目根/game[/game]），从项目根探测
        ver = detect_engine_version(_project_dir(source_root))
    except Exception:
        ver = None
    candidates = []
    if ver and ver[0] < 8:
        candidates.append('tools/renpy-7.4.11-sdk/renpy.exe')
    candidates.append('tools/renpy-8.5.3-sdk/renpy.exe')
    if not (ver and ver[0] < 8):
        candidates.append('tools/renpy-7.4.11-sdk/renpy.exe')
    for rel in candidates:
        exe = find_resource(rel)
        if exe is not None and Path(exe).exists():
            return Path(exe)
    raise RuntimeError('渲染沙盒 SDK 不存在（renpy-7.4.11/8.5.3 均缺失）')


def _project_dir(source_root: Path) -> Path:
    """renpy.exe 的项目目录（含 game/ 的那级）：源码根本身就是 game
    目录（resolve_source_root 已下钻），取其一级父目录——
    标准布局 project/game → project；嵌套布局 install/game/game → install/game"""
    p = source_root.resolve()
    return p.parent if p.name.lower() == 'game' else p


def _collect_stops(first_dlg_line: int, last_dlg_line: int,
                   scene_ops: list) -> list:
    """取态点（与 Pillow 合成器同一策略）：末台词/中点/首台词/最终态，
    无台词时按场景语句位置补中间态"""
    mid_line = (first_dlg_line + last_dlg_line) // 2 if first_dlg_line else 0
    stops = []
    for line in (last_dlg_line, mid_line, first_dlg_line, 0):
        if line not in stops:
            stops.append(line)
    if len(stops) <= 2 and scene_ops:
        lines = sorted(o.line for o in scene_ops)
        n = len(lines)
        for i in (n // 4, n // 2, 3 * n // 4):
            if 0 < i < n and lines[i] not in stops:
                stops.append(lines[i])
    return stops


def build_jobs(entries: list) -> list:
    """[(label, scene_ops, first_dlg, last_dlg, init_state)] → 驱动 jobs

    init_state 为 _SceneState（画面继承）：bg/layers 转成驱动可回放的
    初始指令；每个取态点一个 shot（与 Pillow 候选帧同构）
    """
    jobs = []
    for label, ops, first_dlg, last_dlg, init in entries:
        stops = _collect_stops(first_dlg, last_dlg, ops)
        shots = []
        for i, line in enumerate(stops):
            shots.append({'suffix': '' if i == 0 else f'_{i + 1}',
                          'upto': line if line else 1 << 30})
        job = {'name': label, 'ops': [
            {'op': o.op, 'images': list(o.images), 'at': list(o.at),
             'code': getattr(o, 'code', ''), 'line': o.line}
            for o in ops], 'shots': shots}
        if init is not None:
            job['init'] = {
                'bg': (list(init.bg), []) if init.bg else None,
                'layers': [[list(imgs), list(at)]
                           for imgs, at in init.layers.values()],
            }
        jobs.append(job)
    return jobs


def build_driver_script(jobs: list) -> str:
    """jobs → 驱动 .rpy 文本：助手头 + 真实 Ren'Py 语句正文

    show/scene/hide/exec 走带 try 的 python 助手（缺图/私有 API 不炸），
    每个取态点用 `pause 0.05` 语句让引擎自然渲染一帧后截图
    """
    lines = [_DRIVER_HEAD, 'label zz_sandbox_entry:\n']

    def emit_op(op, indent='    '):
        kind = op.get('op')
        imgs = op.get('images') or []
        at = op.get('at') or []
        code = op.get('code') or ''
        if kind == 'scene':
            lines.append(f"{indent}$ _sb_scene({imgs!r}, {at!r}, {code!r})")
        elif kind == 'show':
            lines.append(f"{indent}$ _sb_show({imgs!r}, {at!r}, {code!r})")
        elif kind == 'hide' and imgs:
            lines.append(f"{indent}$ _sb_hide({imgs[0]!r})")
        elif kind == 'exec' and code:
            lines.append(f"{indent}$ _sb_exec({code!r})")

    for job in jobs:
        # 头像任务：纯色键控背景 + 立绘/角色绘制 → 单帧截图
        if job.get('avatar'):
            lines.append('    $ _sb_scene([], [], "")')
            lines.append("    $ _sb_exec(\"renpy.show('zz_chroma', "
                         "what=Solid('#00FF00'), layer='master')\")")
            if job.get('code'):
                lines.append(f"    $ _sb_exec({job['code']!r})")
            elif job.get('images'):
                # 头像要头部完整：顶部锚定（脚部溢出由圆形裁剪决定取舍）
                lines.append(f"    $ _sb_show({job['images']!r}, "
                             f"['top'], '')")
            lines.append('    $ _sb_frame()')
            lines.append(f"    $ _sb_shot({('avatar__' + job['name'])!r})")
            continue
        init = job.get('init') or {}
        if init.get('bg'):
            lines.append(f"    $ _sb_scene({list(init['bg'][0])!r}, "
                         f"{list(init['bg'][1])!r})")
        for images, at in (init.get('layers') or []):
            lines.append(f"    $ _sb_show({list(images)!r}, {list(at)!r})")
        ops = job.get('ops') or []
        cur = 0
        # 回放是累积的：取态点必须按行号升序回放（快照式语义靠文件名
        # 后缀映射回优先级顺序）
        for shot in sorted(job.get('shots') or [],
                           key=lambda s: s.get('upto', 0)):
            upto = shot.get('upto', 0)
            while cur < len(ops) and (ops[cur].get('line') or 0) <= upto:
                emit_op(ops[cur])
                cur += 1
            name = job['name'] + shot['suffix']
            lines.append('    $ _sb_frame()')
            lines.append(f"    $ _sb_shot({name!r})")
    lines.append('    $ _sb_finish()')
    lines.append('    $ renpy.quit()')
    lines.append('')
    return '\n'.join(lines)


def _kill_tree(proc):
    """杀进程树（Windows taskkill /T /F；POSIX 进程组）"""
    try:
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           capture_output=True, timeout=10)
        else:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_sandbox(source_root: str, jobs: list,
                timeout: int = 1800) -> dict:
    """跑一遍渲染沙盒，返回 {label: [png 文件名（out 目录相对）]}

    source_root: 目标游戏源码根（game/game 下钻后）
    任何启动级失败抛异常（调用方回退 Pillow 合成器）
    """
    if not jobs:
        return {}
    exe = _sdk_exe(Path(source_root).resolve())

    root = Path(source_root).resolve()
    out_dir = root / 'zz_sandbox_out'
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in out_dir.glob('*.png'):
        p.unlink()
    done_flag = root / 'zz_sandbox_done.flag'
    done_flag.unlink(missing_ok=True)

    (root / 'zz_sandbox_driver.rpy').write_text(
        build_driver_script(jobs), encoding='utf-8')
    (root / 'zz_sandbox_jobs.json').write_text(
        json.dumps(jobs, ensure_ascii=False), encoding='utf-8')

    env = dict(os.environ)
    env.setdefault('SDL_AUDIODRIVER', 'dummy')
    # 窗口挪出屏幕（引擎必须有窗口，但不需要可见；若因此全黑由调用方
    # 去掉此环境变量重试）
    env.setdefault('SDL_VIDEO_WINDOW_POS', '-10000,-10000')
    proc = subprocess.Popen(
        [str(exe), str(_project_dir(root))],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(exe.parent), errors='ignore')
    out_text = ''
    try:
        out_text, _ = proc.communicate(timeout=timeout)
        if not done_flag.exists():
            raise RuntimeError(
                f'渲染沙盒未完成（exit={proc.returncode}）: '
                f'{(out_text or "")[-700:]}')
    except subprocess.TimeoutExpired:
        # 超时=沙盒卡死（多半是入口被游戏菜单截住或引擎弹错窗）
        # —— 必须连进程树杀掉，错误窗口不会自己关
        _kill_tree(proc)
        raise RuntimeError(f'渲染沙盒超时（{timeout}s），已终止进程树: '
                           f'{(out_text or "")[-400:]}')
    except BaseException:
        if proc.poll() is None:
            _kill_tree(proc)
        raise
    finally:
        # 驱动文件随用随清（out 目录保留给 harvest，随后删）
        for name in _DRIVER_FILES:
            (root / name).unlink(missing_ok=True)

    results: dict = {}
    for job in jobs:
        name = job['name']
        if job.get('avatar'):
            key = f'avatar:{name}'
            files = sorted(p.name
                           for p in out_dir.glob(f'avatar__{name}*.png'))
            if files:
                results[key] = files
            continue
        files = sorted(p.name for p in out_dir.glob(f'{name}*.png')
                       if p.stem == name
                       or p.stem.startswith(f'{name}_'))
        if files:
            results[name] = files
    results['__out_dir__'] = [str(out_dir)]
    return results


def build_avatar_jobs(characters: list) -> list:
    """角色立绘头像任务

    characters: [{variable, image_tag}]
    - image_tag 非空：show 该 tag（引擎渲染真 layeredimage 分层立绘）
    - 空：the_person.draw_person（LR2 类程序化角色，best-effort——
      接收者不是已实例化 Person 时驱动自动跳过）
    """
    jobs = []
    for c in characters:
        var = c.get('variable') or ''
        if not var:
            continue
        tag = c.get('image_tag') or ''
        job = {'name': var, 'avatar': True}
        if tag:
            job['images'] = tag.split()
        else:
            job['code'] = (f'the_person = {var}\n'
                           'the_person.draw_person(wipe_scene=False, '
                           'show_person_info=False)')
        jobs.append(job)
    return jobs


def harvest_avatars(results: dict, cache_dir: str) -> dict:
    """头像截图 → 键控去背 + 裁剪 → RGBA PNG（graph_cache/avatars/），
    返回 {variable: '@cache/avatars/<var>.png'}；键控色 #00FF00"""
    from PIL import Image

    out_dir = Path(results.get('__out_dir__', [''])[0])
    av_dir = Path(cache_dir) / 'avatars'
    av_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for label, files in results.items():
        if not label.startswith('avatar:'):
            continue
        var = label[len('avatar:'):]
        if not files:
            continue
        try:
            with Image.open(out_dir / files[0]) as im:
                im = im.convert('RGBA')
                # 容差键控（截图经色彩管线后纯色是 (0,254,0) 而非 255）
                im.putdata([
                    (0, 0, 0, 0) if (g > 200 and r < 60 and b < 60)
                    else (r, g, b, a)
                    for r, g, b, a in im.getdata()])
                bbox = im.getbbox()
                if not bbox:
                    continue
                im = im.crop(bbox)
                dest = av_dir / f'{var}.png'
                im.save(dest, 'PNG')
                out[var] = f'@cache/avatars/{var}.png'
        except OSError:
            continue
    return out


def harvest(results: dict, cache_dir: str) -> dict:
    """沙盒 PNG → 烘圆角存 webp 到 graph_cache，返回 {label: [webp 名]}；
    out 目录留给 harvest_avatars 读取，由下次 run_sandbox 开头自清"""
    import shutil

    from PIL import Image

    from scene_composer import _rounded

    out_dir = Path(results['__out_dir__'][0])
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    from scene_composer import _mean_brightness

    out = {}
    for label, files in results.items():
        if label.startswith('avatar:'):
            continue
        frames = []  # (亮度, webp 名)
        for f in files:
            webp = f'{Path(f).stem}.webp'
            try:
                with Image.open(out_dir / f) as im:
                    bright = _mean_brightness(im)
                    _rounded(im.convert('RGB')).save(
                        cache / webp, 'WEBP', quality=80)
                frames.append((bright, webp))
            except OSError:
                continue
        if frames:
            # 与 Pillow 管线同构：亮度降序（首帧为最亮，黑场帧垫底）
            frames.sort(key=lambda x: -x[0])
            out[label] = [n for _, n in frames]
    # out 目录不在此清理：头像 harvest 还要读（下次 run_sandbox 开头自清）
    return out
