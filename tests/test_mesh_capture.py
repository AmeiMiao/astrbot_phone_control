"""经组网地址设置代理，手动信任模式保持无 root 与独立 reverse。"""

import asyncio
import shlex
from types import SimpleNamespace

from core.traffic_capture import TrafficCapture


class Adb:
    def __init__(self):
        self.commands = []
        self.proxy = "192.0.2.9:8888"
        self.runs = []

    async def shell(self, command, **kw):
        self.commands.append(command)
        if command == "settings get global http_proxy":
            return self.proxy
        if command.startswith("settings put global http_proxy "):
            self.proxy = shlex.split(command)[-1]
        return ""

    async def run(self, args, **kw):
        self.runs.append(args)
        return ""


class Lease:
    task = None

    async def arm(self, pid, *, cleanup):
        self.cleanup = cleanup
        self.task = SimpleNamespace(done=lambda: False)


def test_mesh_manual_capture_uses_overlay_and_restores_existing_proxy(tmp_path):
    async def run():
        adb = Adb()
        calls = []

        async def expose(port):
            calls.append(("expose", port))
            return "100.90.0.1:18889"

        async def hide():
            calls.append(("hide",))

        mesh = SimpleNamespace(expose_proxy=expose, hide_proxy=hide)
        capture = TrafficCapture(adb=adb, data_dir=tmp_path, mesh=mesh)
        capture._transport = "mesh"
        capture._ca_mode = "manual"
        capture._port = 18080
        lease = Lease()
        capture._device_lease = lease
        await capture._setup_device()
        assert adb.proxy == "100.90.0.1:18889"
        assert not adb.runs
        assert not any("nsenter" in c or "id -u" in c for c in adb.commands)
        assert "100.90.0.1:18889" in lease.cleanup and "nsenter" not in lease.cleanup
        await capture._restore_device_locked()
        assert adb.proxy == "192.0.2.9:8888"
        assert calls == [("expose", 18080), ("hide",)]

    asyncio.run(run())


def test_mesh_cleanup_preserves_unrelated_proxy_and_reverse(tmp_path):
    async def run():
        adb = Adb()
        capture = TrafficCapture(adb=adb, data_dir=tmp_path)
        capture._transport = "mesh"
        capture._ca_mode = "manual"
        await capture.cleanup_device_state(port=18080)
        assert adb.proxy == "192.0.2.9:8888" and not adb.runs

    asyncio.run(run())


def test_restore_preserves_proxy_changed_after_capture_started(tmp_path):
    async def run():
        adb = Adb()
        capture = TrafficCapture(adb=adb, data_dir=tmp_path)
        capture._transport = "mesh"
        capture._ca_mode = "manual"
        capture._proxy_value = "100.90.0.1:18889"
        capture._prev_proxy = ":0"
        await capture._restore_device_locked()
        assert adb.proxy == "192.0.2.9:8888"

    asyncio.run(run())


def test_capture_process_still_stops_when_overlay_port_cleanup_fails(
    tmp_path, monkeypatch
):
    import pytest
    from test_traffic import _CaptureHarness

    async def run():
        harness = _CaptureHarness(tmp_path, monkeypatch)
        await harness.capture.start(port=18080)
        capture = harness.capture
        capture._device_configured = True
        capture._transport = "mesh"
        capture._mesh_proxy = True

        async def fail():
            raise OSError("network control unavailable")

        capture.mesh = SimpleNamespace(hide_proxy=fail)
        with pytest.raises(RuntimeError, match="清理未全部完成"):
            await capture.stop()
        assert not capture.running and harness.procs[0].returncode is not None

    asyncio.run(run())
