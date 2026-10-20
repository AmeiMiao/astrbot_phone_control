"""core.plugin.compat 的入口模块解析回归测试。

AstrBot 真实加载插件时的模块名是 ``data.plugins.<目录名>.core...``，入口模块名
``data.plugins.<目录名>.main``，不能只取 ``__name__`` 的第一段（否则会找
``data.main``）。
"""

from __future__ import annotations

import sys
import types

import pytest

from core.plugin import compat


def _install_main(monkeypatch: pytest.MonkeyPatch, root: str) -> types.ModuleType:
    main = types.ModuleType(f"{root}.main")
    monkeypatch.setitem(sys.modules, f"{root}.main", main)
    monkeypatch.setattr(compat, "__name__", f"{root}.core.plugin.compat", raising=False)
    return main


def test_resolves_astrbot_data_plugins_package(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _install_main(monkeypatch, "data.plugins.astrbot_phone_control")
    assert compat.plugin_main_module() is main


def test_resolves_test_temporary_package(monkeypatch: pytest.MonkeyPatch) -> None:
    main = _install_main(monkeypatch, "astrbot_phone_control_under_test")
    assert compat.plugin_main_module() is main


def test_raises_when_main_module_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        compat, "__name__", "no_such_plugin.core.plugin.compat", raising=False
    )
    with pytest.raises(RuntimeError, match="main"):
        compat.plugin_main_module()
