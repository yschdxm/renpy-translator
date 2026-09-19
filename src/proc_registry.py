"""子进程注册表：服务关停时兜底清理

服务以 detached 进程常驻，退出走 os._exit(0)（冻结环境下解释器退出
可能被残留线程卡住，必须硬退）——Windows 下硬退不连带杀子进程，
renpy.exe 这类引擎子进程会变成孤儿（窗口开在屏外，任务管理器里
才看得见，且卡住时永不退出）。所有长命子进程的属主在 Popen 后
track、结束后 untrack，服务关停时统一 kill_all。
"""

import os
import subprocess
import sys
import threading

_lock = threading.Lock()
_procs: set = set()


def kill_tree(proc):
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


def track(proc):
    with _lock:
        _procs.add(proc)


def untrack(proc):
    with _lock:
        _procs.discard(proc)


def kill_all():
    """杀掉所有在跑的注册进程树（服务关停兜底，可重入）

    kill_tree 本身对已退出的进程无害（taskkill 报找不到即返回），
    无需先 poll 过滤。
    """
    with _lock:
        procs = list(_procs)
    for p in procs:
        if p.poll() is None:
            kill_tree(p)
