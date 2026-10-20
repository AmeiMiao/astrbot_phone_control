"""main.PhoneControlPlugin 配置补丁热应用测试（容器内跑，本地无 astrbot 自动跳过）。

运行中改配置走命令/原生配置页重载后的内部补丁路径
``plugin._apply_config_patch``（``/phone continuous``、``/phone traffic``
等共用）。本文件用假 Context 重放该路径，断言持续刷手机、实时屏幕、后端重置等运行态
动作确实被触发，覆盖「配置保存后没有即时生效」的回归场景。
"""

from __future__ import annotations

import asyncio
import dataclasses
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
    """把插件目录当作包导入（main.py 使用相对导入，不能按顶层模块导入）。"""
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "astrbot_phone_control_under_test"
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
PhoneConfig = plugin_main.PhoneConfig


class FakeContext:
    """最小 Star Context：只实现插件装配需要的接口。"""

    cron_manager = None

    def __init__(self) -> None:
        self.tools = []
        self.web_routes: list[tuple[str, tuple[str, ...]]] = []

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)

    def register_web_api(self, route, handler, methods=None, desc=None) -> None:
        self.web_routes.append((str(route), tuple(methods or [])))


def _deep_merge(base: dict, patch: dict) -> dict:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _raw_config(**overrides) -> dict:
    """按完整卡片结构构造插件配置（默认值 + 分组覆盖）。"""
    raw = dataclasses.asdict(PhoneConfig.from_mapping(None))
    raw["adb"] = {
        "host": "127.0.0.1",
        "port": 5555,
        "path": "",
        "serial": "",
        "connect_timeout": 2,
        "command_timeout": 3,
    }
    return _deep_merge(raw, overrides)


def _make_plugin(tmp_path, monkeypatch) -> "plugin_main.PhoneControlPlugin":
    """实例化插件并让配置写回落到临时目录。"""
    monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
    plugin = plugin_main.PhoneControlPlugin(FakeContext(), _raw_config())
    monkeypatch.setattr(
        plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
    )
    return plugin


async def _save(plugin, config: dict) -> list[str]:
    """走运行中配置补丁路径（与命令侧相同）。"""
    return await plugin._apply_config_patch(config)


async def _cleanup(plugin) -> None:
    try:
        await plugin.terminate()
    except Exception:  # noqa: BLE001 - 清理失败不影响断言结果
        pass


def test_patch_starts_continuous_play_immediately(tmp_path, monkeypatch):
    """补丁开启 auto.continuous_enabled=true 必须立即启动持续刷循环。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            assert plugin._continuous_enabled is False
            notes = "；".join(
                await _save(plugin, _raw_config(auto={"continuous_enabled": True}))
            )
            assert plugin._continuous_enabled is True
            assert plugin._continuous_task is not None
            assert not plugin._continuous_task.done()
            assert "持续刷手机：已开启" in notes
            assert "配置已保存并即时生效" in notes
            saved = json.loads(
                (tmp_path / "plugin_config.json").read_text(encoding="utf-8")
            )
            assert saved["auto"]["continuous_enabled"] is True
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_patch_stops_continuous_play(tmp_path, monkeypatch):
    """补丁关闭 auto.continuous_enabled 后不再开新轮。"""

    async def scenario() -> None:
        monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
        plugin = plugin_main.PhoneControlPlugin(
            FakeContext(), _raw_config(auto={"continuous_enabled": True})
        )
        monkeypatch.setattr(
            plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
        )
        try:
            assert plugin._continuous_enabled is True
            notes = "；".join(
                await _save(
                    plugin, _raw_config(auto={"continuous_enabled": False})
                )
            )
            assert plugin._continuous_enabled is False
            assert plugin._continuous_task is None
            assert "持续刷手机：已关闭" in notes
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_patch_live_screen_enable_without_task(tmp_path, monkeypatch):
    """开启任务期间持续截图：无运行中任务时记录为下次任务生效，不常驻 watcher。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            notes = "；".join(
                await _save(plugin, _raw_config(perception={"live_screen_enabled": True}))
            )
            assert "任务期间持续截图：已开启" in notes
            assert "下次任务开始时启用" in notes
            assert plugin.live_screen.running is False
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_patch_backend_change_resets_backends(tmp_path, monkeypatch):
    """控制后端变化：重置当前控制/感知后端会话。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        stop_calls = []

        async def fake_stop():
            stop_calls.append(True)

        monkeypatch.setattr(plugin.backends, "stop", fake_stop)
        try:
            notes = "；".join(
                await _save(plugin, _raw_config(control={"backend": "adb"}))
            )
            assert stop_calls == [True]
            assert "控制/感知后端" in notes
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_patch_adb_change_reconfigures_client(tmp_path, monkeypatch):
    """ADB 参数变化：调用 AdbClient.reconfigure 换目标，并重建后端。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        recorded: dict = {}

        async def fake_reconfigure(**kwargs):
            recorded.update(kwargs)
            return {"changed": True}

        async def fake_stop():
            recorded["stopped"] = True

        monkeypatch.setattr(plugin.adb, "reconfigure", fake_reconfigure)
        monkeypatch.setattr(plugin.backends, "stop", fake_stop)
        try:
            config = _raw_config()
            config["adb"].update({"host": "127.0.0.2", "port": 6666})
            notes = "；".join(await _save(plugin, config))
            assert recorded.get("host") == "127.0.0.2"
            assert recorded.get("port") == 6666
            assert recorded.get("stopped") is True
            assert "ADB 连接参数已更新" in notes
            assert "127.0.0.2:6666" in notes
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_patch_accepts_debug_section(tmp_path, monkeypatch):
    """完整配置包含 debug 段时按正常配置处理。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            notes = "；".join(
                await _save(plugin, _raw_config(debug={"log_actions": False}))
            )
            assert "忽略未知配置项" not in notes
            assert plugin.raw_config["debug"]["log_actions"] is False
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_apply_config_patch_partial_patch(tmp_path, monkeypatch):
    """命令侧传部分补丁（/phone continuous on 等）也走同一管道并即时生效。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            await plugin._apply_config_patch(
                {"auto": {"continuous_enabled": True}}
            )
            assert plugin._continuous_enabled is True
            assert plugin._continuous_task is not None
            assert not plugin._continuous_task.done()
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_traffic_log_lines_masks_sensitive_params_and_truncates():
    """抓包日志摘要：敏感参数打码、正文截断、带页面推断与时间。"""
    flows = [
        {
            "ts": 1792460000,
            "method": "GET",
            "url": "https://api.xiaoheihe.cn/bbs/app/feed?sign=secret&page=1",
            "status": 200,
            "body": "x" * 500,
        }
    ]
    lines = plugin_main.PhoneControlPlugin._traffic_log_lines(
        flows, body_chars=100, limit=3
    )
    text = "\n".join(lines)
    assert "sign=***" in text
    assert "小黑盒" in text
    assert "响应：" in text
    assert len(lines[1]) < 130
