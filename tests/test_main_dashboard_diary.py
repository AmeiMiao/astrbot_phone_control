"""main.PhoneControlPlugin Dashboard「浏览日志」页接口测试（容器内跑，本地无 astrbot 跳过）。

GET dashboard/diary 返回统计与分页日志（最新在前），支持关键词检索；日志清理
上限/保护轮次来自 browse 配置，页面不返回截图绝对路径。
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
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "astrbot_phone_control_diary_dashboard_test"
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

from core.models import BrowseResult, FeedItem  # noqa: E402


class FakeContext:
    cron_manager = None

    def __init__(self) -> None:
        self.tools: list = []
        self.web_routes: list[tuple[str, tuple[str, ...]]] = []

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)

    def register_web_api(self, route, handler, methods=None, desc=None) -> None:
        self.web_routes.append((str(route), tuple(methods or [])))


class FakeQuery:
    def __init__(self, values: dict) -> None:
        self.values = values

    def get(self, name, default=None, type=None):  # noqa: A002 - 模仿框架签名
        if name not in self.values:
            return default
        value = self.values[name]
        if type is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default


class FakeRequest:
    def __init__(self, query: dict) -> None:
        self.query = FakeQuery(query)


def _raw_config(**overrides) -> dict:
    raw = dataclasses.asdict(PhoneConfig.from_mapping(None))
    raw["adb"] = {
        "host": "",
        "port": 0,
        "path": "",
        "serial": "",
        "connect_timeout": 2,
        "command_timeout": 3,
    }
    for key, value in overrides.items():
        raw[key] = value
    return raw


def _make_plugin(tmp_path, monkeypatch) -> "plugin_main.PhoneControlPlugin":
    monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path / "data")
    context = FakeContext()
    plugin = plugin_main.PhoneControlPlugin(context, _raw_config())
    monkeypatch.setattr(
        plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
    )
    return plugin


def _json(response) -> dict:
    body = getattr(response, "body", None)
    if body is None and isinstance(response, tuple):
        body = response[0]
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)


def _request(plugin, monkeypatch, query: dict) -> None:
    monkeypatch.setattr(
        plugin_main,
        "request",
        FakeRequest(query),
    )


def _seed(plugin) -> None:
    diary = plugin.service.diary
    for index in range(6):
        result = BrowseResult(
            app="抖音",
            items=[
                FeedItem(
                    app="douyin",
                    signature=f"sig-{index}",
                    texts=[f"第{index}条内容"],
                    title=f"标题{index}",
                    author="作者",
                )
            ],
            steps=1,
            elapsed=0.5,
        )
        diary.add_browse(
            result,
            note=f"备注{index}",
            round_id=f"r{index // 3}",
            round_kind="autonomous",
            max_entries=500,
            keep_rounds=3,
        )


def test_diary_route_registered(tmp_path, monkeypatch):
    plugin = _make_plugin(tmp_path, monkeypatch)
    try:
        routes = dict(plugin.context.web_routes)
        assert routes[f"/{plugin_main.PLUGIN_NAME}/dashboard/diary"] == ("GET",)
    finally:
        asyncio.run(plugin.terminate())


def test_diary_endpoint_payload_pagination_and_stats(tmp_path, monkeypatch):
    plugin = _make_plugin(tmp_path, monkeypatch)

    async def scenario() -> None:
        try:
            _seed(plugin)
            _request(plugin, monkeypatch, {"limit": "2", "offset": "0"})
            data = _json(await plugin.dashboard_diary())
            assert data["total"] == 6
            assert data["limit"] == 2 and data["offset"] == 0
            assert [entry["items"][0]["title"] for entry in data["entries"]] == [
                "标题5",
                "标题4",
            ]
            assert data["stats"]["entries"] == 6
            assert data["stats"]["max_entries"] == 500
            assert data["stats"]["keep_rounds"] == 3
            assert data["entries"][0]["round_kind"] == "autonomous"
            assert "screenshot" not in data["entries"][0]["items"][0]
            assert "path" not in data["stats"]

            _request(plugin, monkeypatch, {"query": "标题2", "limit": "10"})
            searched = _json(await plugin.dashboard_diary())
            assert searched["total"] == 1
            assert searched["entries"][0]["items"][0]["title"] == "标题2"
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_diary_endpoint_works_without_adb(tmp_path, monkeypatch):
    """假开启（ADB 未配置）时面板浏览日志页仍可读。"""
    plugin = _make_plugin(tmp_path, monkeypatch)

    async def scenario() -> None:
        try:
            assert plugin._config().adb.configured is False
            _seed(plugin)
            _request(plugin, monkeypatch, {"limit": "20"})
            data = _json(await plugin.dashboard_diary())
            assert data["total"] == 6
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_startup_cleanup_applies_configured_limits(tmp_path, monkeypatch):
    plugin = _make_plugin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
    )

    async def scenario() -> None:
        try:
            # 18 条日志 = 3 轮任务（每轮 6 条）；上限 10、保护最近 1 轮。
            from core.models import BrowseResult, FeedItem

            for index in range(18):
                result = BrowseResult(
                    app="抖音",
                    items=[
                        FeedItem(
                            app="douyin",
                            signature=f"sig-{index}",
                            texts=[f"第{index}条内容"],
                            title=f"标题{index}",
                            author="作者",
                        )
                    ],
                    steps=1,
                    elapsed=0.5,
                )
                plugin.service.diary.add_browse(
                    result, round_id=f"r{index // 6}", round_kind="autonomous"
                )
            raw = dict(plugin.raw_config)
            raw["browse"] = {
                **dict(raw.get("browse") or {}),
                "diary_max_entries": 10,
                "diary_protect_rounds": 1,
            }
            plugin.raw_config.clear()
            plugin.raw_config.update(raw)
            plugin.service._raw_config = plugin.raw_config
            await plugin._cleanup_diary_limits()
            entries = list(plugin.service.diary.all_entries())
            assert len(entries) == 10
            # 最近一轮 r2 的 6 条全部保留。
            assert [entry.get("round_id") for entry in entries[-6:]] == ["r2"] * 6
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
