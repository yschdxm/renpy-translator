"""Ren'Py 翻译工具统一入口

用法:
    python run.py                  # 托盘模式（默认，无窗口驻系统托盘）
    python run.py --mode gui       # GUI 模式（pywebview 桌面窗口，关窗即退）
    python run.py --mode web       # WebUI 模式（自动打开浏览器）
    python run.py --mode server    # 前台服务（调试用，Ctrl+C 停止）
    python run.py --mode stop      # 停止后台服务

架构:
    服务以分离进程常驻后台（server-detached），托盘/窗口/浏览器只是界面。
    关闭任何界面进程都不影响任务；托盘菜单可重新打开界面或退出服务。
"""
import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

# Windows 编码（windowed exe 无控制台时 stdout 为 None，跳过）
if sys.platform == "win32" and sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 打包资源根（src/、web/dist）：冻结后在 _MEIPASS
if getattr(sys, 'frozen', False):
    BUNDLE = Path(sys._MEIPASS)
else:
    BUNDLE = Path(__file__).resolve().parent

sys.path.insert(0, str(BUNDLE / "src"))

# 加载 .env（若存在；位置随程序：exe/仓库根旁）
env_file = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
            else BUNDLE) / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# 用户数据根（rt_home 三级解析：指针 → 便携 exe 旁 → 平台默认；
# 环境变量 RT_HOME 可覆盖，见 src/rt_home.py）
from rt_home import home as _rt_home  # noqa: E402

HOME = _rt_home()

# 冻结且无控制台（windowed exe）：print 无处可去，重定向到启动器日志
if getattr(sys, 'frozen', False) and sys.stdout is None:
    (HOME / 'logs').mkdir(exist_ok=True)
    sys.stdout = open(HOME / 'logs' / 'launcher.log', 'a', encoding='utf-8')
    sys.stderr = sys.stdout

# 静态导入 server 包：让 PyInstaller 追踪到完整依赖树
# （uvicorn 以字符串 "server.app:create_app" 加载，静态分析发现不了）
import server.app  # noqa: F401,E402

PORT = int(os.environ.get("PORT", 7861))


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_port(port: int, timeout: float = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.2)
    return False


def _server_alive(port: int) -> bool:
    """端口开着且确实是本服务（而非被其他程序占用）"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _spawn_detached(args: list, log_name: str = None,
                    visible_console: bool = False):
    """以分离进程启动（父进程退出后存活）

    visible_console=True（开发模式）：开一个独立的可见控制台窗口，
    实时显示该进程日志；窗口随新进程组独立，关启动终端不影响它。
    """
    out = None
    if log_name:
        log_file = HOME / 'logs' / log_name
        log_file.parent.mkdir(exist_ok=True)
        out = open(log_file, 'a', encoding='utf-8')
    if sys.platform == 'win32':
        if visible_console:
            flags = (subprocess.CREATE_NEW_CONSOLE
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            flags = (subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP
                     | subprocess.CREATE_NO_WINDOW)
        return subprocess.Popen(
            args,
            stdout=out if out else None,
            stderr=subprocess.STDOUT if out else None,
            creationflags=flags, cwd=str(HOME), close_fds=True)
    return subprocess.Popen(
        args,
        stdout=out if out else None,
        stderr=subprocess.STDOUT if out else None,
        start_new_session=True, cwd=str(HOME), close_fds=True)


def _self_cmd(mode: str) -> list:
    """以当前形态（冻结 exe / 脚本）再起一个本程序实例"""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--mode', mode]
    return [sys.executable, str(HOME / 'run.py'), '--mode', mode]


def ensure_server(port: int):
    """确保后台服务在跑：已在跑则复用，否则以分离进程启动（关窗不死）

    开发模式给服务一个可见的日志控制台（实时日志）；打包版隐藏+写文件。
    """
    if _port_open(port):
        if _server_alive(port):
            print(f"检测到后台服务已在运行: http://127.0.0.1:{port}")
            return
        raise SystemExit(
            f"端口 {port} 被其他程序占用（/api/health 无响应），"
            f"请释放端口或用环境变量 PORT 指定其他端口")

    frozen = getattr(sys, 'frozen', False)
    if frozen:
        _spawn_detached(_self_cmd('server-detached'), 'server.log')
    else:
        # 开发：服务开可见控制台，实时滚动日志（不重定向文件）
        _spawn_detached(_self_cmd('server-detached'),
                        visible_console=True)

    if not _wait_port(port):
        raise SystemExit(
            f"后台服务启动超时（60 秒），请查看日志: logs/server.log")
    print(f"后台服务已启动: http://127.0.0.1:{port}"
          f"（{'日志: logs/server.log' if frozen else '日志见新开的控制台窗口'}）")


def run_gui(port: int):
    """GUI 窗口模式：pywebview 窗口，关窗即退出（服务留后台）"""
    ensure_server(port)
    try:
        import webview
    except ImportError:
        raise SystemExit(_webview_missing_msg())

    class JsApi:
        """暴露给前端的原生能力（前端检测 window.pywebview 存在即用）"""

        def pick_directory(self):
            result = window.create_file_dialog(webview.FOLDER_DIALOG)
            return result[0] if result else None

        def pick_zip(self):
            result = window.create_file_dialog(
                webview.OPEN_DIALOG, file_types=('ZIP 文件 (*.zip)',))
            return result[0] if result else None

        def open_folder(self, path):
            if sys.platform == 'win32':
                os.startfile(path)  # noqa: S606（本地工具，路径来自服务端导出）
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])

        def close_window(self):
            """后端广播 shutdown 后，前端调用此方法关闭窗口"""
            window.destroy()

    url = f"http://127.0.0.1:{port}"
    window = webview.create_window(
        "Ren'Py 翻译工具", url,
        width=1440, height=900, min_size=(1100, 700),
        js_api=JsApi(),
    )

    # 看门狗：服务异常死掉（崩溃/被杀）时向前端发提示事件——只提示，不关窗
    # （正常退出服务由后端广播 shutdown，前端自行关窗，见 JsApi.close_window）
    def _watchdog():
        time.sleep(5)  # 启动宽限
        fails = 0
        while True:
            time.sleep(2)
            if _server_alive(port):
                fails = 0
            else:
                fails += 1
                if fails >= 3:
                    try:
                        window.evaluate_js(
                            "window.dispatchEvent(new Event('rt-server-lost'))")
                    except Exception:
                        pass
                    return

    threading.Thread(target=_watchdog, daemon=True).start()
    webview.start()
    # 窗口关闭：服务留在后台，任务不受影响
    print("窗口已关闭。服务仍在后台运行（可用托盘或 run.py 重新打开界面）")


def _webview_missing_msg() -> str:
    if sys.platform.startswith('linux'):
        return ("GUI 窗口需要系统 GTK/WebKit 支持："
                "sudo apt install python3-gi gir1.2-webkit2-4.1，"
                "或改用托盘/浏览器模式")
    return "GUI 窗口模式需要 pywebview（uv add pywebview）"


def _load_tray_icon_image():
    """加载应用图标（installer/make_icon.py 生成，打包进 assets/）"""
    from PIL import Image
    for p in (BUNDLE / 'assets' / 'icon.png',
              BUNDLE / 'installer' / 'icon.png'):
        if p.exists():
            return Image.open(p)
    # 兜底：运行时画一个（图标文件丢失时不至于起不来）
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    from PIL import ImageDraw
    ImageDraw.Draw(img).rounded_rectangle(
        (2, 2, 62, 62), radius=14, fill=(59, 130, 246, 255))
    return img


def run_tray(port: int):
    """托盘模式（默认）：无窗口驻系统托盘

    服务在独立分离进程，窗口/浏览器也是独立进程——
    任何界面关闭都不影响服务与任务，仅「退出服务」会停止服务。
    """
    ensure_server(port)
    try:
        import pystray
    except ImportError:
        raise SystemExit("托盘模式需要 pystray：uv add pystray pillow")

    url = f"http://127.0.0.1:{port}"

    def open_window(icon=None, item=None):
        _spawn_detached(_self_cmd('gui'), 'gui.log')

    def open_browser(icon=None, item=None):
        webbrowser.open(url)

    def quit_all(icon, item):
        try:
            stop_server(port)
        except SystemExit:
            return  # 停止超时（服务正忙）：托盘保持驻留，用户可稍后再试
        icon.stop()

    icon = pystray.Icon(
        'renpy-translator', _load_tray_icon_image(), "Ren'Py 翻译工具",
        menu=pystray.Menu(
            pystray.MenuItem('打开界面', open_window, default=True),
            pystray.MenuItem('用浏览器打开', open_browser),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('退出服务', quit_all),
        ))
    # 启动时顺便开一个窗口（首次使用体验）
    open_window()
    icon.run()


def run_web(port: int):
    ensure_server(port)
    url = f"http://127.0.0.1:{port}"
    print(f"WebUI: {url}（服务常驻后台，关闭浏览器不影响任务）")
    webbrowser.open(url)


def run_server(port: int, detached: bool = False):
    """前台跑服务（--mode server 调试用 / server-detached 由 ensure_server 拉起）"""
    import uvicorn

    if not detached and _port_open(port):
        raise SystemExit(f"端口 {port} 已被占用（可用环境变量 PORT 指定其他端口）")
    config = uvicorn.Config(
        "server.app:create_app", factory=True,
        host="127.0.0.1", port=port, log_level="info",
        access_log=False,  # 请求日志由 server.app 中间件按状态码分级
    )
    try:
        uvicorn.Server(config).run()
    except KeyboardInterrupt:
        # uvicorn 已完成优雅关闭（lifespan：关库、取消任务），静默即可
        pass
    finally:
        if detached:
            # 冻结环境下解释器退出可能被残留线程卡住
            # （优雅关闭已在上面完成），显式退出保证服务进程可终止
            try:
                sys.stdout.flush()
            except Exception:
                pass
            os._exit(0)


def stop_server(port: int):
    if not _server_alive(port):
        print("后台服务未在运行")
        return
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/shutdown", method='POST')
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # 服务可能已开始退出，连接断开属正常
    deadline = time.time() + 10
    while time.time() < deadline:
        if not _port_open(port):
            print("后台服务已停止")
            return
        time.sleep(0.3)
    raise SystemExit("停止超时（10 秒）——服务可能在执行长任务，请稍后再试")


def main_cli():
    parser = argparse.ArgumentParser(description="Ren'Py 翻译工具")
    parser.add_argument('--mode', default='tray',
                        choices=['tray', 'gui', 'web', 'server',
                                 'server-detached', 'stop'])
    args = parser.parse_args()

    if args.mode == 'gui':
        run_gui(PORT)
    elif args.mode == 'web':
        run_web(PORT)
    elif args.mode == 'server':
        run_server(PORT)
    elif args.mode == 'server-detached':
        run_server(PORT, detached=True)
    elif args.mode == 'stop':
        stop_server(PORT)
    else:
        run_tray(PORT)


if __name__ == '__main__':
    try:
        main_cli()
    except SystemExit:
        raise
    except Exception:
        # 无控制台环境下异常不能静默死：写启动器日志后原样抛出
        import traceback
        try:
            (HOME / 'logs').mkdir(exist_ok=True)
            with open(HOME / 'logs' / 'launcher.log', 'a',
                      encoding='utf-8') as f:
                f.write('\n' + traceback.format_exc() + '\n')
        except OSError:
            pass
        raise
