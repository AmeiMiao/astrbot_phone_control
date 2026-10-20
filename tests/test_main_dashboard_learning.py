"""main.PhoneControlPlugin Dashboard 学习记录页接口测试（容器内跑，本地无 astrbot 跳过）。

学习记录页只读：GET dashboard/learning 返回统计与记录（最新在前），支持关键词检索。
本文件用假 Context/Web request 直接调用插件方法。
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("astrbot")


def _load_plugin_main():
    """把插件目录当作包导入（main.py 使用相对导入，不能按顶层模块导入）。"""
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "astrbot_phone_control_learning_test"
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
        self.tools: list = []
        self.web_routes: list[tuple[str, tuple[str, ...]]] = []

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)

    def register_web_api(self, route, handler, methods=None, desc=None) -> None:
        self.web_routes.append((str(route), tuple(methods or [])))


class FakeQuery:
    """假 request.query：支持 .get(name, default, type=...)。"""

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
    """假 Web request：只提供 query。"""

    def __init__(self, query: dict) -> None:
        self.query = FakeQuery(query)


def _raw_config() -> dict:
    raw = dataclasses.asdict(PhoneConfig.from_mapping(None))
    raw["adb"] = {
        "host": "127.0.0.1",
        "port": 5555,
        "path": "",
        "serial": "",
        "connect_timeout": 2,
        "command_timeout": 3,
    }
    return raw


def _make_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
    context = FakeContext()
    plugin = plugin_main.PhoneControlPlugin(context, _raw_config())
    monkeypatch.setattr(
        plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
    )
    return plugin


def _seed(plugin) -> None:
    store = plugin.service.learning
    store.record(
        content="B站首页推荐了 AI 编程教程，讲用 Agent 做自动化",
        source="B站",
        tags=["AI", "编程"],
        ts=1792470000.0,
    )
    store.record(
        content="抖音刷到手机摄影构图技巧：三分法、引导线",
        source="抖音",
        tags=["摄影"],
        ts=1792470100.0,
    )
    store.record(
        content="小黑盒社区讨论新游戏上线，玩家评价普遍不错",
        source="小黑盒",
        tags=["游戏"],
        ts=1792470200.0,
    )


def test_learning_payload_empty(tmp_path, monkeypatch):
    plugin = _make_plugin(tmp_path, monkeypatch)
    try:
        payload = plugin._learning_payload()
        assert payload["entries"] == []
        assert payload["stats"]["entries"] == 0
        assert payload["stats"]["sources"] == {}
    finally:
        asyncio.run(plugin.terminate())


def test_learning_payload_returns_newest_first_with_stats(tmp_path, monkeypatch):
    plugin = _make_plugin(tmp_path, monkeypatch)
    try:
        _seed(plugin)
        payload = plugin._learning_payload(limit=10)
        assert payload["stats"]["entries"] == 3
        assert payload["stats"]["sources"] == {"B站": 1, "抖音": 1, "小黑盒": 1}
        assert payload["stats"]["last_ts"] == 1792470200.0
        assert payload["stats"]["max_entries"] == plugin.service.learning.max_entries
        entries = payload["entries"]
        # 最新在前
        assert [entry["source"] for entry in entries] == ["小黑盒", "抖音", "B站"]
        assert entries[0]["tags"] == ["游戏"]
        assert entries[2]["tags"] == ["AI", "编程"]
    finally:
        asyncio.run(plugin.terminate())


def test_learning_payload_search_filters(tmp_path, monkeypatch):
    plugin = _make_plugin(tmp_path, monkeypatch)
    try:
        _seed(plugin)
        payload = plugin._learning_payload(query="AI", limit=10)
        assert payload["query"] == "AI"
        assert [entry["source"] for entry in payload["entries"]] == ["B站"]
        payload = plugin._learning_payload(query="摄影", limit=10)
        assert [entry["source"] for entry in payload["entries"]] == ["抖音"]
        payload = plugin._learning_payload(query="不存在", limit=10)
        assert payload["entries"] == []
    finally:
        asyncio.run(plugin.terminate())


def test_dashboard_learning_api_clamps_query_and_limit(tmp_path, monkeypatch):
    plugin = _make_plugin(tmp_path, monkeypatch)
    responses: dict = {}

    def fake_json_response(data=None, **kwargs):
        responses["data"] = data
        return data

    monkeypatch.setattr(plugin_main, "json_response", fake_json_response)
    try:
        _seed(plugin)
        monkeypatch.setattr(
            plugin_main,
            "request",
            FakeRequest({"query": "B" * 200, "limit": "500"}),
        )
        result = asyncio.run(plugin.dashboard_learning())
        assert result["limit"] == 200
        assert len(result["query"]) == 100
        assert result["stats"]["entries"] == 3

        monkeypatch.setattr(plugin_main, "request", FakeRequest({"limit": "0"}))
        result = asyncio.run(plugin.dashboard_learning())
        assert result["limit"] == 1
    finally:
        asyncio.run(plugin.terminate())


def test_dashboard_registers_learning_readonly_route(tmp_path, monkeypatch):
    plugin = _make_plugin(tmp_path, monkeypatch)
    try:
        routes = dict(plugin.context.web_routes)
        assert routes.get("/astrbot_phone_control/dashboard/overview") == ("GET",)
        assert routes.get("/astrbot_phone_control/dashboard/series") == ("GET",)
        assert routes.get("/astrbot_phone_control/dashboard/learning") == ("GET",)
    finally:
        asyncio.run(plugin.terminate())
