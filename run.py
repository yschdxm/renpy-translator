"""启动 Ren'Py 翻译工具 - NiceGUI 版本"""

import sys
import os
from pathlib import Path

# 设置 Windows 编码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 添加源代码目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# 导入 main 模块（注册 @ui.page('/') 路由）
import main  # noqa: F401

from nicegui import ui

print("启动 Ren'Py 游戏翻译工具...")
print("访问地址: http://localhost:7860")

ui.run(
    title="Ren'Py 翻译工具",
    port=7860,
    language="zh-CN",
    dark=True,
    storage_secret='renpy-translator-secret-key',
    uvicorn_reload_dirs=str(Path(__file__).parent / 'src'),
    uvicorn_reload_includes='*.py',
)
