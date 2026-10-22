"""真实 AstrBot 依赖下的独立配置迁移接口与默认安装回归。"""

from __future__ import annotations

import asyncio
import copy
import json
import importlib
from pathlib import Path

import pytest

astrbot = pytest.importorskip("astrbot")
if getattr(astrbot, "_acat_local_stub", False):
    pytest.skip("本地无真实 AstrBot", allow_module_level=True)

helper = importlib.import_module("test_main_config_hot_apply")
_make_plugin = helper._make_plugin
plugin_main = helper.plugin_main


class DiskConfig(dict):
    def __init__(self, value, path):
        super().__init__(value)
        self.config_path = str(path)
        self.fail = False

    def save_config(self):
        if self.fail:
            raise OSError("SECRET_WRITE_ERROR")
        Path(self.config_path).write_text(json.dumps(self, ensure_ascii=False))


def setup_plugin(tmp_path, monkeypatch):
    p = _make_plugin(tmp_path / "data", monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / f"{plugin_main.PLUGIN_NAME}_config.json"
    raw = copy.deepcopy(dict(p.raw_config))
    # 新生效的默认 ADB 为空；迁移目标以新安装默认值参与「当前值是否自定义」判定。
    raw["adb"] = {**dict(raw.get("adb") or {}), "host": "", "port": 0}
    raw["security"]["owner_ids"] = ["CURRENT_OWNER_SECRET"]
    config = DiskConfig(raw, path)
    config.save_config()
    p.raw_config = config
    p.service._raw_config = config
    monkeypatch.setattr(p, "_config_file_path", lambda: path)
    source = config_dir / "astrbot_phone_control_config.json"
    source.write_text(
        json.dumps(
            {
                "adb": {"host": "192.0.2.10", "port": 16512},
                "browse": {"default_app": "bilibili"},
                "security": {"owner_ids": ["SOURCE_OWNER_SECRET"]},
            }
        )
    )
    calls = []

    async def apply_runtime(previous, notes):
        calls.append(previous)

    monkeypatch.setattr(p, "_apply_runtime_config", apply_runtime)
    monkeypatch.setattr(plugin_main, "json_response", lambda data=None, **kwargs: data)
    monkeypatch.setattr(
        plugin_main,
        "error_response",
        lambda message, **kwargs: {"error": message, **kwargs},
    )
    return p, source, path, calls


def body(monkeypatch, data):
    class Request:
        async def json(self):
            return data

    monkeypatch.setattr(plugin_main, "request", Request())


def test_routes_use_metadata_namespace_and_allows_both_methods(tmp_path, monkeypatch):
    p, source, target, calls = setup_plugin(tmp_path, monkeypatch)
    try:
        routes = p.context.web_routes
        prefix = f"/{plugin_main.PLUGIN_NAME}/dashboard/config-migration"
        assert (prefix, ("GET",)) in routes and (prefix, ("POST",)) in routes
        assert all(
            not route.startswith("/astrbot_phone_control/") for route, _ in routes
        )
        assert p._config_file_path() == target
    finally:
        asyncio.run(p.terminate())


def test_preview_import_preserves_custom_config_and_source_and_is_idempotent(
    tmp_path, monkeypatch
):
    p, source, target, calls = setup_plugin(tmp_path, monkeypatch)
    before = source.read_bytes()

    async def scenario():
        try:
            plan = await p.dashboard_config_migration_preview()
            assert plan["count"] > 0 and "security.owner_ids" in plan["kept"]
            assert "SOURCE_OWNER_SECRET" not in json.dumps(plan)
            assert "CURRENT_OWNER_SECRET" not in json.dumps(plan)
            body(monkeypatch, {"token": plan["token"]})
            result = await p.dashboard_config_migration_apply()
            assert result["count"] > 0 and calls
            assert p.raw_config["adb"]["host"] == "192.0.2.10"
            assert p.raw_config["adb"]["port"] == 16512
            assert p.raw_config["browse"]["default_app"] == "bilibili"
            assert p.raw_config["security"]["owner_ids"] == ["CURRENT_OWNER_SECRET"]
            assert source.read_bytes() == before
            assert json.loads(target.read_text()) == p.raw_config
            again = await p.dashboard_config_migration_preview()
            assert again["count"] == 0
            body(monkeypatch, {"token": again["token"]})
            assert (await p.dashboard_config_migration_apply())["count"] == 0
            assert len(calls) == 1
        finally:
            await p.terminate()

    asyncio.run(scenario())


def test_apply_requires_preview_and_rejects_changed_target(tmp_path, monkeypatch):
    p, source, target, calls = setup_plugin(tmp_path, monkeypatch)

    async def scenario():
        try:
            body(monkeypatch, {})
            assert (await p.dashboard_config_migration_apply())["status_code"] == 400
            plan = await p.dashboard_config_migration_preview()
            target.write_text(target.read_text() + "\n")
            before = target.read_bytes()
            body(monkeypatch, {"token": plan["token"]})
            result = await p.dashboard_config_migration_apply()
            assert "重新预览" in result["error"]
            assert target.read_bytes() == before and not calls
        finally:
            await p.terminate()

    asyncio.run(scenario())


def test_save_failure_restores_runtime_and_does_not_claim_success(
    tmp_path, monkeypatch
):
    p, source, target, calls = setup_plugin(tmp_path, monkeypatch)
    before, original, runtime = (
        target.read_bytes(),
        source.read_bytes(),
        copy.deepcopy(dict(p.raw_config)),
    )

    async def scenario():
        try:
            plan = await p.dashboard_config_migration_preview()
            p.raw_config.fail = True
            body(monkeypatch, {"token": plan["token"]})
            result = await p.dashboard_config_migration_apply()
            assert "保存" in result["error"] and "SECRET" not in result["error"]
            assert dict(p.raw_config) == runtime and not calls
            assert target.read_bytes() == before and source.read_bytes() == original
        finally:
            await p.terminate()

    asyncio.run(scenario())


def test_missing_or_invalid_source_is_actionable(tmp_path, monkeypatch):
    p, source, target, calls = setup_plugin(tmp_path, monkeypatch)

    async def scenario():
        try:
            source.unlink()
            assert (
                "未找到原插件配置"
                in (await p.dashboard_config_migration_preview())["error"]
            )
            source.write_text('{"adb":{"port":70000}}')
            assert "ADB" in (await p.dashboard_config_migration_preview())["error"]
            assert not calls
        finally:
            await p.terminate()

    asyncio.run(scenario())


def test_real_astrbot_config_empty_adb_loads_fake_enabled(tmp_path, monkeypatch):
    """ADB 为空时插件照常加载（假开启），不回填 127.0.0.1；不影响配置路径。"""
    from astrbot.api import AstrBotConfig

    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    monkeypatch.setattr(
        plugin_main, "_resolve_data_dir", lambda: tmp_path / "mirror-data"
    )
    from test_main_config_hot_apply import FakeContext

    for values in ({}, {"adb": {"host": "", "port": 0}}):
        path = tmp_path / f"{plugin_main.PLUGIN_NAME}_config.json"
        path.write_text(json.dumps(values))
        config = AstrBotConfig(str(path), schema=schema)
        p = plugin_main.PhoneControlPlugin(FakeContext(), config)
        try:
            assert p._config().adb.configured is False
            assert p._config().adb.target == ""
            assert p.data_dir == tmp_path / "mirror-data"
            saved = json.loads(path.read_text(encoding="utf-8-sig"))
            assert (saved.get("adb") or {}).get("host", "") == ""
            assert p._config_file_path() == path
        finally:
            asyncio.run(p.terminate())
