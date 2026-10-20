"""插件主模块访问兼容层。

AstrBot 以函数所在模块绑定插件处理器，因此命令/钩子处理器必须仍定义在插件入口
``main.py``；搬出的 mixin 在运行时通过本模块访问入口模块里的少量符号
（``json_response``/``error_response``/``request``/``_resolve_data_dir``），
这样测试对入口模块的 monkeypatch 仍然生效。

兼容真实包（``astrbot_phone_control.main``）与测试用临时包（``<pkg>.main``）。
"""

from __future__ import annotations

import sys
from typing import Any


def plugin_main_module() -> Any:
    """返回插件入口 ``main`` 模块对象。

    AstrBot 真实加载时插件包名是 ``data.plugins.<目录名>``，不能只取
    ``__name__`` 第一段（那会得到 ``data``）。这里从当前模块名逐级向上查找
    已加载的 ``<prefix>.main``，同时兼容真实包与测试临时包。
    """
    parts = (__name__ or "").split(".")
    for index in range(len(parts) - 1, -1, -1):
        candidate = ".".join([*parts[:index], "main"]) if index else "main"
        module = sys.modules.get(candidate)
        if module is not None:
            return module
    raise RuntimeError("插件入口 main 模块尚未加载，无法访问主模块级函数")


def json_response(*args: Any, **kwargs: Any) -> Any:
    """转发到入口模块的 ``json_response``（保留测试 monkeypatch 语义）。"""
    return plugin_main_module().json_response(*args, **kwargs)


def error_response(*args: Any, **kwargs: Any) -> Any:
    """转发到入口模块的 ``error_response``。"""
    return plugin_main_module().error_response(*args, **kwargs)
