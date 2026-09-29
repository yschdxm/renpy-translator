from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from gui_launcher import GuiLauncher


def test_repeated_tray_clicks_reuse_process_and_reopen_after_close():
    spawned, focused = [], []

    def spawn():
        process = SimpleNamespace(pid=len(spawned) + 1, returncode=None)
        process.poll = lambda: process.returncode
        spawned.append(process)
        return process

    launcher = GuiLauncher(spawn, focused.append)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: launcher.open(), range(20)))
    assert len(spawned) == 1
    assert all(p is spawned[0] for p in results)
    assert focused == [1] * 19
    spawned[0].returncode = 0
    assert launcher.open() is spawned[1]
    assert len(spawned) == 2
