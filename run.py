"""启动Ren'Py翻译工具 - NiceGUI版本"""

import sys
import os

# 设置Windows编码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 添加源代码目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from nicegui import ui
from ui_nicegui import create_app

# 创建应用
create_app()

print("启动Ren'Py游戏翻译工具...")
print("访问地址: http://localhost:8088")

# 启动（不要放在 main guard 里）
ui.run(
    title="Ren'Py翻译工具",
    port=8088,
    language="zh-CN",
    dark=True,
)
