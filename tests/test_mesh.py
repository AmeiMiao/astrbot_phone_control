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
    remaining = b"original-image-data"
    while remaining:
        sent = stream.send(remaining)
        assert sent == min(len(remaining), 2)
        remaining = remaining[sent:]
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


def test_wireguard_start_and_control_share_mesh_conflict_guard(tmp_path):
    from core.wireguard import WireGuardManager

    async def run():
        manager = WireGuardManager(
            data_dir=tmp_path, blocking_reason=lambda: "请先关闭 Tailscale/ZeroTier"
        )
        result = await manager.start()
        assert not result["ok"] and "Tailscale/ZeroTier" in result["reason"]
        for action in ["start", "restart"]:
            result = await manager.control(action)
            assert not result["ok"] and "Tailscale/ZeroTier" in result["text"]
        assert not manager.running

    asyncio.run(run())


def test_stop_closes_real_active_forward_connection(tmp_path):
    async def run():
        m = MeshManager(service(tmp_path, phone_ip="100.90.0.1"))
        accepted = asyncio.Event()
        peers = []

        async def sink(reader, writer):
            peers.append(writer)
            accepted.set()
            await reader.read()
            writer.close()
            await writer.wait_closed()

        target = await asyncio.start_server(sink, "127.0.0.1", 0)

        async def connect(host, port):
            return await asyncio.open_connection(
                "127.0.0.1", target.sockets[0].getsockname()[1]
            )

        m.connect = connect
        m.server = await asyncio.start_server(m.forward, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", m.server.sockets[0].getsockname()[1]
        )
        await accepted.wait()
        await asyncio.wait_for(m.stop(), 3)
        assert await asyncio.wait_for(reader.read(), 1) == b""
        writer.close()
        await writer.wait_closed()
        target.close()
        await target.wait_closed()
        assert m.server is None and not m.connections

    asyncio.run(run())


def test_zerotier_bridge_large_duplex_payload_and_half_close():
    """大于组网 MTU 的双向数据与半关闭保持完整，SDK socket 只归一个线程。"""
    import select
    import socket
    import threading
    from core.mesh_zerotier_worker import Stream, bridge

    owners = set()

    class Virtual(Stream):
        def __init__(self, sock):
            self.sock = sock
            sock.setblocking(False)

        def recv(self, size):
            owners.add(threading.get_ident())
            return self.sock.recv(size)

        def send(self, data):
            owners.add(threading.get_ident())
            return self.sock.send(data[:997])

        def poll(self, reading, writing):
            owners.add(threading.get_ident())
            r, w, _ = select.select([self.sock] if reading else [],
                                     [self.sock] if writing else [], [], 0)
            return bool(r), bool(w)

        def shutdown(self, how):
            owners.add(threading.get_ident())
            self.sock.shutdown(how)

        def close(self):
            owners.add(threading.get_ident())
            self.sock.close()

    client, native = socket.socketpair()
    virtual, peer = socket.socketpair()
    client.settimeout(10)
    peer.settimeout(10)
    outgoing = bytes(range(256)) * 8192
    response = b"response" * 300000
    failures = []

    def receive(sock):
        result = bytearray()
        while chunk := sock.recv(65536):
            result.extend(chunk)
        return bytes(result)

    def serve():
        try:
            assert receive(peer) == outgoing
            peer.sendall(response)
            peer.shutdown(socket.SHUT_WR)
        except BaseException as exc:
            failures.append(exc)
        finally:
            peer.close()

    worker = threading.Thread(target=bridge, args=(native, Virtual(virtual)))
    server = threading.Thread(target=serve)
    worker.start()
    server.start()
    try:
        client.sendall(outgoing)
        client.shutdown(socket.SHUT_WR)
        assert receive(client) == response
    finally:
        client.close()
        worker.join(10)
        server.join(10)
    assert not worker.is_alive() and not server.is_alive()
    assert not failures
    assert len(owners) == 1


def test_mesh_stop_cleans_proxy_after_capture_process_already_exited(tmp_path):
    async def run():
        calls = []
        s = service(tmp_path)
        async def stop_capture():
            calls.append("restore_phone_proxy")
        s.traffic = SimpleNamespace(running=False, status=lambda:{"transport":"mesh"}, stop=stop_capture)
        manager = MeshManager(s)
        manager.exposed_port = 18889
        async def hide():
            calls.append("remove_overlay_listener")
            manager.exposed_port = 0
        manager.hide_proxy = hide
        await manager.stop()
        assert calls == ["restore_phone_proxy", "remove_overlay_listener"]
        assert manager.exposed_port == 0
    asyncio.run(run())


def test_zerotier_accept_uses_exact_sdk_address_buffer_and_reports_invalid_arguments():
    import ctypes
    from core.mesh_zerotier_worker import SDK, Stream
    native = SimpleNamespace()
    calls = []
    def accept(fd, address, length):
        assert address is None and length is None
        calls.append(("accept", fd))
        return 9
    def peername(fd, buffer, length, port):
        calls.append(("peer", fd, length))
        if length != 46:
            return -3
        peer = b"192.0.2.18\0"
        ctypes.memmove(buffer, peer, len(peer))
        return 0
    native.zts_bsd_accept = accept
    native.zts_getpeername = peername
    sdk = SDK.__new__(SDK)
    sdk.lib = native
    listener = Stream(sdk, 7)
    stream, peer = listener.accept()
    assert peer == "192.0.2.18" and stream.fd == 9
    assert calls == [("accept", 7), ("peer", 9, 46)]
    with pytest.raises(OSError, match="code -3"):
        sdk.check(-3)
    calls.clear()
    native.zts_bsd_accept = lambda *args: -2
    with pytest.raises(OSError, match="code -2"):
        listener.accept()
    assert not calls
