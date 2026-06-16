"""启动Ren'Py翻译工具"""

import sys
import os

# 设置Windows编码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 添加源代码目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from main import create_ui

if __name__ == "__main__":
    print("启动Ren'Py游戏翻译工具...")
    print("请确保已配置API接口")
    print("访问地址: http://localhost:7860")

    demo = create_ui()
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        share=False,
        inbrowser=True,
    )
