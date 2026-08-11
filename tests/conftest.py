"""测试公共配置：把 src/ 与仓库根加入 sys.path

- src/：业务模块以扁平方式互相 import（translator/database/... 无包前缀）
- 仓库根：server 包（server.state 等）与 rt_home 的资源解析基准
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / 'src'):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
