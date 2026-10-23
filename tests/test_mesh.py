"""用户态组网的配置、目标隔离、登录状态与转发恢复。"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.config import PhoneConfig
from core.config_apply import build_apply_plan
from core.mesh import MeshError, MeshManager, private_json


def service(tmp_path, **mesh):
    history = []

    async def reconfigure(**kwargs):
        history.append(kwargs)

    async def ready():
        pass

    return SimpleNamespace(
        config=PhoneConfig.from_mapping({"mesh": mesh}),
        data_dir=tmp_path,
        adb=SimpleNamespace(
            _host="192.0.2.1",
            _port=5555,
            serial="192.0.2.1:5555",
            _serial_explicit=False,
            reconfigure=reconfigure,
            ensure_ready=ready,
        ),
        history=history,
    )


def test_mesh_config_plan_and_private_state(tmp_path):
    before = PhoneConfig.from_mapping({})
    after = PhoneConfig.from_mapping(
        {"mesh": {"enabled": True, "phone_ip": "100.90.0.1"}}
    )
    plan = build_apply_plan(before, after)
    assert plan.changed and plan.mesh_changed
    assert not plan.wireguard_changed
    path = tmp_path / "node.json"
    private_json(path, {"ready": True})
    assert json.loads(path.read_text()) == {"ready": True}
    assert path.stat().st_mode & 0o777 == 0o600


def test_mesh_disabled_and_wireguard_conflict_do_not_start(tmp_path):
    async def run():
        s = service(tmp_path)
        m = MeshManager(s)
        with pytest.raises(MeshError, match="开启"):
            await m.start()
        s.config = PhoneConfig.from_mapping(
            {"mesh": {"enabled": True}, "wireguard": {"enabled": True}}
        )
        with pytest.raises(MeshError, match="WireGuard"):
            await m.start()
        assert not m.running

    asyncio.run(run())


def test_mesh_status_is_sanitized_and_login_state_reported(tmp_path):
    async def run():
        s = service(tmp_path, enabled=True, tailscale_auth_key="secret-test-key")
        m = MeshManager(s)
        m.process = SimpleNamespace(returncode=None)

        async def cli(*args, **kw):
            return (
                0,
                json.dumps(
                    {
                        "BackendState": "NeedsLogin",
                        "AuthURL": "https://login.tailscale.com/a/test",
                        "TailscaleIPs": [],
                    }
                ),
                "",
            )

        m.cli = cli
        result = await m.status()
        assert not result["ready"] and result["auth_url"]
        assert "secret-test-key" not in json.dumps(result)

        async def online(*args, **kw):
            return (
                0,
                json.dumps(
                    {
                        "BackendState": "Running",
                        "TailscaleIPs": ["100.90.0.1"],
                        "Self": {"ID": "node-test"},
                    }
                ),
                "",
            )

        m.cli = online
        assert (await m.status())["ready"]

    asyncio.run(run())


def test_tailscale_does_not_fall_back_to_lan_or_public_target(tmp_path):
    async def run():
        m = MeshManager(service(tmp_path))
        for ip in ["192.168.250.10", "127.0.0.1", "8.8.8.8"]:
            with pytest.raises(MeshError, match="组网网段"):
                await m.connect(ip, 15555)

    asyncio.run(run())


def test_proxy_exposure_requires_live_mesh_and_only_local_target(tmp_path):
    async def run():
        m = MeshManager(service(tmp_path, enabled=True))
        commands = []

        async def offline():
            return {"ready": False}

        m.status = offline
        with pytest.raises(MeshError):
            await m.expose_proxy(8080)

        async def status():
            return {"ready": True, "addresses": ["100.90.0.1"]}

        async def cli(*args, **kw):
            commands.append(args)
            return 0, "", ""

        m.status = status
        m.cli = cli
        assert await m.expose_proxy(8080) == "100.90.0.1:18889"
        assert commands == [("serve", "--bg", "--tcp=18889", "tcp://127.0.0.1:8080")]

    asyncio.run(run())


def test_stop_restores_original_adb_target(tmp_path):
    async def run():
        s = service(tmp_path)
        m = MeshManager(s)
        m.adb_original = ("192.0.2.1", 5555, "")
        await m.stop()
        assert s.history == [dict(host="192.0.2.1", port=5555, serial="")]
        assert not m.running

    asyncio.run(run())


def test_zerotier_invalid_network_and_stale_status_are_not_ready(tmp_path):
    async def run():
        s = service(
            tmp_path,
            enabled=True,
            provider="zerotier",
            zerotier_network_id="not-a-network",
        )
        m = MeshManager(s)
        with pytest.raises(MeshError, match="Network ID"):
            await m.start()
        s.config = PhoneConfig.from_mapping(
            {
                "mesh": {
                    "enabled": True,
                    "provider": "zerotier",
                    "zerotier_network_id": "0123456789abcdef",
                }
            }
        )
        m.process = SimpleNamespace(returncode=None)
        private_json(
            m.root / "zerotier-status.json",
            dict(network_id="other-network", updated_at=0, ready=True),
        )
        result = await m.status()
        assert not result["ready"] and result["error"]

    asyncio.run(run())


def test_bundled_zerotier_sdk_exports_required_native_api():
    import platform
    from pathlib import Path
    from core.mesh_zerotier_worker import SDK

    if platform.system() != "Linux" or platform.machine().lower() not in {
        "x86_64",
        "amd64",
    }:
        pytest.skip("Linux x86_64 bundled artifact")
    library = Path(__file__).resolve().parents[1] / "bin/linux-x86_64/libzt.so"
    sdk = SDK(library)
    assert sdk.lib.zts_node_start.argtypes == []
    assert len(sdk.lib.zts_connect.argtypes) == 4


def test_zerotier_stream_handles_partial_native_send():
    import ctypes
    from core.mesh_zerotier_worker import Stream

    captured = []

    def send(fd, buffer, size, flags):
        n = min(size, 2)
        captured.append(ctypes.string_at(buffer, n))
        return n

    class Native:
        zts_send = staticmethod(send)

        @staticmethod
        def zts_close(fd):
            return 0

    sdk = SimpleNamespace(lib=Native(), check=lambda n: n)
    stream = Stream(sdk, 7)
    stream.sendall(b"original-image-data")
    stream.close()
    stream.close()
    assert b"".join(captured) == b"original-image-data"


def test_mesh_commands_are_bounded_and_compatible_with_python_310():
    import sys
    from core.mesh import run_command

    async def run():
        code, out, error = await run_command(
            [
                sys.executable,
                "-c",
                'import sys; print("ok"); print("diagnostic",file=sys.stderr)',
            ]
        )
        assert code == 0 and out.strip() == "ok" and error.strip() == "diagnostic"
        with pytest.raises(asyncio.TimeoutError):
            await run_command(
                [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05
            )

    asyncio.run(run())
