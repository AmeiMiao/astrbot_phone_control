"""ADB 未配置时的「假开启」状态测试（容器内跑，本地无 astrbot 跳过）。

空白 adb.host/port 时插件必须能正常加载：面板/配置/迁移可用，但不连接设备、
不启动自主浏览/随机/持续刷/抓包/WireGuard 等后台循环，命令与工具返回可读提示。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_astrbot = pytest.importorskip("astrbot")
if getattr(_astrbot, "_acat_local_stub", False):
    pytest.skip("本地无真实 AstrBot，跳过容器级用例", allow_module_level=True)


def _load_plugin_main():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "astrbot_phone_control_fake_enabled_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.main", plugin_dir / "main.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin_main = _load_plugin_main()


class FakeContext:
    """最小 Star Context：只实现插件装配需要的接口。"""

    cron_manager = None

    def __init__(self) -> None:
        self.tools: list = []
        self.web_routes: list[tuple[str, tuple[str, ...]]] = []

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)

    def register_web_api(self, route, handler, methods=None, desc=None) -> None:
        self.web_routes.append((str(route), tuple(methods or [])))


def _make_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path / "data")
    context = FakeContext()
    plugin = plugin_main.PhoneControlPlugin(context, {})
    monkeypatch.setattr(
        plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
    )
    return plugin


def _json(response) -> dict:
    """兼容 AstrBot json_response 返回对象/元组两种形态。"""
    body = getattr(response, "body", None)
    if body is None and isinstance(response, tuple):
        body = response[0]
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)


def test_empty_adb_loads_as_fake_enabled(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            config = plugin._config()
            assert config.adb.configured is False
            assert config.adb.target == ""
            assert plugin._phone_enabled is False
            assert plugin.adb.configured is False
            # 工具照常注册（调用时由门禁返回未配置提示），面板路由照常注册。
            assert plugin.context.tools
            routes = [route for route, _ in plugin.context.web_routes]
            assert any(route.endswith("/dashboard/overview") for route in routes)

            await plugin.initialize()
            # 假开启：没有任何设备后台任务被启动。
            assert getattr(plugin, "_auto_config_task", None) is None
            assert getattr(plugin, "_random_task", None) is None
            assert getattr(plugin, "_continuous_task", None) is None
            assert getattr(plugin, "_traffic_keepalive_task", None) is None
            assert getattr(plugin, "_wireguard_keepalive_task", None) is None

            status = await plugin.service.status_text()
            assert "未启用" in status
            assert "adb.host" in status

            overview = _json(await plugin.dashboard_overview())
            assert overview["version"]
            assert overview["quota"]["enabled"] is False
            # 面板概览不应触发设备预热。
            assert getattr(plugin, "_scrcpy_warmup_task", None) is None
            # 设备操作明确报未配置，不启动 adb 子进程。
            with pytest.raises(plugin_main.AdbNotConfiguredError):
                await plugin.adb.connect()
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_empty_adb_command_and_tool_gate(tmp_path, monkeypatch):
    """命令/工具在假开启状态下返回统一的未配置提示。"""
    from core.config import UNCONFIGURED_MESSAGE

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            with pytest.raises(plugin_main.AdbNotConfiguredError) as excinfo:
                plugin.service.ensure_configured()
            assert UNCONFIGURED_MESSAGE in str(excinfo.value)
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
