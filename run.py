"""启动 Ren'Py 翻译工具 - NiceGUI 版本"""

import sys
import os
from pathlib import Path

# 设置 Windows 编码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 加载 .env 文件中的环境变量（若存在）
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# 添加源代码目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# 导入 main 模块（注册 @ui.page('/') 路由）
import main  # noqa: F401

from nicegui import ui

port = int(os.environ.get("PORT", 7860))

print("启动 Ren'Py 游戏翻译工具...")
print(f"访问地址: http://localhost:{port}")

ui.run(
    title="Ren'Py 翻译工具",
    port=port,
    language="zh-CN",
    dark=True,
    storage_secret='renpy-translator-secret-key',
    uvicorn_reload_dirs=str(Path(__file__).parent / 'src'),
    uvicorn_reload_includes='*.py',
)
