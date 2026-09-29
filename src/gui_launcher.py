"""托盘窗口进程复用：启动期间也持有锁，避免连续点击拉起多个 WebView。"""
import sys
import threading


def focus_window(pid: int) -> bool:
    """Windows 上按进程定位窗口并恢复；其他平台保持已有窗口。"""
    if sys.platform != 'win32':
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    found = False

    @callback_type
    def visit(hwnd, _):
        nonlocal found
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            if user32.IsIconic(hwnd):
                user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            found = True
            return False
        return True

    user32.EnumWindows(visit, 0)
    return found


class GuiLauncher:
    def __init__(self, spawn, focus=focus_window):
        self._spawn = spawn
        self._focus = focus
        self._lock = threading.Lock()
        self.process = None

    def open(self):
        with self._lock:
            if self.process is None or self.process.poll() is not None:
                self.process = self._spawn()
            else:
                # 还在启动、尚未建窗时不再启动第二份。
                self._focus(self.process.pid)
            return self.process
