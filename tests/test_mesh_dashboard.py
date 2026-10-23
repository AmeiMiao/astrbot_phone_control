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
