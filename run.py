"""启动Ren'Py翻译工具 - NiceGUI版本"""

import sys
import os
from pathlib import Path

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

# 获取项目根目录
project_root = Path(__file__).parent

print("启动Ren'Py游戏翻译工具...")
print("访问地址: http://localhost:7860")

# 启动（不要放在 main guard 里）
ui.run(
    title="Ren'Py翻译工具",
    port=7860,
    language="zh-CN",
    dark=True,
    # 只监控 src 目录的变化，忽略其他目录
    uvicorn_reload_dirs=str(project_root / 'src'),
    uvicorn_reload_includes='*.py',
)
