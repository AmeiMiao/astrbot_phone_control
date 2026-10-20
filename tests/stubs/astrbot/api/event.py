"""本地测试用 ``astrbot.api.event`` 桩：只提供调度器导入所需的消息链类型。

插件生产代码统一 ``from astrbot.api.event import MessageChain``；本机没有
AstrBot 时由 ``tests/conftest.py`` 把本目录加入搜索路径，便于纯逻辑单测导入
``core.service`` 等模块。该桩不参与消息发送，只满足模块导入。
"""

from __future__ import annotations

from typing import Any

_acat_local_stub = True


class MessageChain:
    """最小消息链桩。"""

    def message(self, _text: Any = None) -> "MessageChain":
        return self
