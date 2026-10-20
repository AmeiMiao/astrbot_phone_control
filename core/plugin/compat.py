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
    """返回插件入口 ``main`` 模块对象。"""
    root = (__name__ or "").split(".")[0]
    module = sys.modules.get(f"{root}.main")
    if module is None:  # pragma: no cover - 仅在模块未加载完成的异常路径触发
        raise RuntimeError("插件入口 main 模块尚未加载，无法访问主模块级函数")
    return module


def json_response(*args: Any, **kwargs: Any) -> Any:
    """转发到入口模块的 ``json_response``（保留测试 monkeypatch 语义）。"""
    return plugin_main_module().json_response(*args, **kwargs)


def error_response(*args: Any, **kwargs: Any) -> Any:
    """转发到入口模块的 ``error_response``。"""
    return plugin_main_module().error_response(*args, **kwargs)
