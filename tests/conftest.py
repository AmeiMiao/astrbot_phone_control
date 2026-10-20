"""pytest 公共配置：本地无 AstrBot 时提供最小 ``astrbot.api`` 桩。

生产代码统一从 ``astrbot.api`` 导入 logger；本机纯逻辑单测环境没有 AstrBot，
把 ``tests/stubs`` 追加到搜索路径末尾，真实 AstrBot 存在时不会被覆盖。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

if importlib.util.find_spec("astrbot") is None:
    _stub_dir = Path(__file__).resolve().parent / "stubs"
    if str(_stub_dir) not in sys.path:
        sys.path.append(str(_stub_dir))
