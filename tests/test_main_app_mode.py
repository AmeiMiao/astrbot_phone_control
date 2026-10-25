"""真实 AstrBot 装配下的 App 模式、配对和 APK 交付回归。"""

import asyncio
import base64
import importlib
import json
import pytest

astrbot = pytest.importorskip("astrbot")
if getattr(astrbot, "_acat_local_stub", False):
    pytest.skip("需要真实 AstrBot", allow_module_level=True)
helper = importlib.import_module("test_main_config_hot_apply")


def test_app_bootstrap_pairing_and_apk(tmp_path, monkeypatch):
    p = helper._make_plugin(tmp_path, monkeypatch)
    monkeypatch.setattr(
        helper.plugin_main, "json_response", lambda value=None, **kw: value
    )
    monkeypatch.setattr(
        helper.plugin_main, "error_response", lambda value, **kw: {"error": value, **kw}
    )

    async def run():
        try:
            result = await p.dashboard_app_connection()
            assert result["enabled"] is False
            result = await p.dashboard_app_apk()
            apk = base64.b64decode(result["content_base64"])
            assert apk.startswith(b"PK")
            await p._app_bridge.start(0)
            module = importlib.import_module(p.dashboard_app_connection.__module__)
            monkeypatch.setattr(
                module,
                "request",
                type(
                    "Request",
                    (),
                    {"query": {"host": "relay.example.test"}, "headers": {}},
                )(),
            )
            result = await p.dashboard_app_connection()
            code = json.loads(base64.urlsafe_b64decode(result["code"]))
            assert code["host"] == "relay.example.test" and len(code["pin"]) == 64
            old_token = code["token"]
            await p.dashboard_app_reset()
            assert p._app_bridge._state["token"] != old_token
        finally:
            await p.terminate()

    asyncio.run(run())


def test_app_configuration_constructs_app_transport(tmp_path, monkeypatch):
    plugin_main = helper.plugin_main
    monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
    raw = helper._raw_config()
    raw["adb"]["mode"] = "使用 App 控制"
    p = plugin_main.PhoneControlPlugin(helper.FakeContext(), raw)

    async def run():
        try:
            assert p.service.config.adb.mode == "app"
            assert p.adb.app_mode and not p._components.needs_prepare(p.service.config)
            await p.service.backends.ensure()
            assert p.service.backends._scrcpy_client is None
        finally:
            await p.terminate()

    asyncio.run(run())
