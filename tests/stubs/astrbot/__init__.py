"""本地测试用最小 ``astrbot`` 包（仅提供 ``astrbot.api.logger`` 桩）。

插件运行在 AstrBot 中，生产代码统一 ``from astrbot.api import logger``；
本机没有 AstrBot 时由 ``tests/conftest.py`` 把本目录加入搜索路径，便于纯逻辑单测。
"""

_acat_local_stub = True
