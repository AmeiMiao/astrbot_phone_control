"""本地测试用 ``astrbot.api`` 桩：只提供静默 logger。"""

from __future__ import annotations

from typing import Any

_acat_local_stub = True


class _NullLogger:
    """吞掉日志调用的空 logger，保持与 astrbot.api.logger 相同的方法名。"""

    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def exception(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def critical(self, *_args: Any, **_kwargs: Any) -> None:
        pass


logger = _NullLogger()
