"""真实 AstrBot 组网状态、操作校验与公开 CA 下载边界。"""

import asyncio
import base64

import pytest

astrbot = pytest.importorskip("astrbot")
if getattr(astrbot, "_acat_local_stub", False):
    pytest.skip("需要真实 AstrBot", allow_module_level=True)
from test_main_fake_enabled import _make_plugin, _json, plugin_main
from core.traffic import CA_FILE_NAME


def test_mesh_dashboard_rejects_invalid_actions_and_only_returns_public_ca(
    tmp_path, monkeypatch
):
    async def run():
        plugin = _make_plugin(tmp_path, monkeypatch)

        class Body:
            value = {"action": []}

            async def json(self):
                return self.value

        body = Body()
        monkeypatch.setattr(plugin_main, "request", body)
        try:
            status = _json(await plugin.dashboard_mesh())
            assert not status["ready"] and not status["running"]
            assert "tailscale_auth_key" not in status
            for value in [{"action": []}, {"action": "invalid"}, [], None]:
                body.value = value
                assert (await plugin.dashboard_mesh_action()).status_code == 400
            assert (await plugin.dashboard_mesh_ca()).status_code == 400
            path = plugin._traffic.confdir / CA_FILE_NAME
            path.parent.mkdir(parents=True, exist_ok=True)
            certificate = (
                b"-----BEGIN CERTIFICATE-----\nPUBLIC CERT\n-----END CERTIFICATE-----"
            )
            path.write_bytes(certificate)
            result = _json(await plugin.dashboard_mesh_ca())
            assert base64.b64decode(result["content_base64"]) == certificate
            path.write_bytes(certificate + b"\n-----BEGIN PRIVATE KEY-----\nsecret")
            assert (await plugin.dashboard_mesh_ca()).status_code == 500
        finally:
            await plugin.terminate()

    asyncio.run(run())


def test_mesh_start_stop_commands_persist_enabled_state(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from test_main_0130_features import FakeEvent

    async def run():
        plugin = _make_plugin(tmp_path, monkeypatch)
        for name in ("tailscale", "tailscaled"):
            path = plugin._components.path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"manual-test-component")
        plugin._mesh.reconcile = AsyncMock(return_value={})
        plugin._mesh.start = AsyncMock(return_value={"ready": False})
        plugin._mesh.stop = AsyncMock()
        plugin._mesh.status = AsyncMock(return_value={"running": False})
        plugin._say = AsyncMock()
        event = FakeEvent()
        try:
            await plugin._dispatch_phone(event, "mesh start")
            assert plugin.service.config.mesh.enabled
            plugin._mesh.start.assert_awaited_once()
            await plugin._dispatch_phone(event, "mesh stop")
            assert not plugin.service.config.mesh.enabled
            plugin._mesh.stop.assert_awaited_once()
        finally:
            await plugin.terminate()

    asyncio.run(run())


def test_wireguard_dashboard_requires_recent_peer_handshake(tmp_path, monkeypatch):
    import time

    async def run():
        plugin = _make_plugin(tmp_path, monkeypatch)
        plugin.raw_config["network"] = {"enabled": True, "provider": "wireguard"}
        status = {
            "ok": True,
            "running": True,
            "enabled": True,
            "public_key": "public",
            "interface_addresses": ["10.0.0.1/24"],
            "adb_over_wg": False,
            "error": "",
            "health_reason": "",
            "last_handshake_unix": 0,
        }
        monkeypatch.setattr(plugin._wireguard, "status", lambda: status)
        try:
            result = _json(await plugin.dashboard_mesh())
            assert result["running"] and not result["ready"]
            status["last_handshake_unix"] = time.time() - 30
            assert _json(await plugin.dashboard_mesh())["ready"]
            status["last_handshake_unix"] = time.time() - 600
            assert not _json(await plugin.dashboard_mesh())["ready"]
        finally:
            await plugin.terminate()

    asyncio.run(run())
