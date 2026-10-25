"""App 控制传输、配置兼容和取消的回归。"""

import asyncio
import base64
import pytest
from core.config import PhoneConfig
from core.app_bridge import AppBridge
from core.app_adb import AppAdbClient
from core.errors import ActionFailedError, DeviceOfflineError


def test_existing_adb_defaults_and_hidden_values():
    direct = PhoneConfig.from_mapping({"adb": {"host": "device", "port": 5555}})
    app = PhoneConfig.from_mapping(
        {"adb": {"mode": "app", "host": "device", "port": 5555}}
    )
    assert direct.adb.mode == "direct" and direct.adb.target == "device:5555"
    assert app.adb.configured and app.adb.host == "device" and app.adb.port == 5555
    assert app.adb.target == "Relay App"


def test_app_never_runs_host_adb(tmp_path):
    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        adb = AppAdbClient(bridge, tmp_path)
        with pytest.raises(DeviceOfflineError):
            await adb.shell("input tap 1 1")
        with pytest.raises(ActionFailedError, match="文件传输"):
            await adb.run(["push", "a", "b"])
        with pytest.raises(ActionFailedError):
            await adb.exec_out("cat", "/data/private")

    asyncio.run(run())


def test_app_shell_preserves_exit_code_without_retry(tmp_path):
    class Bridge:
        calls = 0

        async def call(self, op, **params):
            self.calls += 1
            return {
                "rc": 7,
                "stdout": base64.b64encode(b"out").decode(),
                "stderr": base64.b64encode(b"failed").decode(),
            }

    async def run():
        bridge = Bridge()
        adb = AppAdbClient(bridge, tmp_path)
        result = await adb.shell_result("false", timeout=1)
        assert result == {"exit_code": 7, "stdout": "out", "stderr": "failed"}
        assert bridge.calls == 1

    asyncio.run(run())


def test_app_timeout_sends_cancel_without_replay(tmp_path):
    class Writer:
        pass

    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        bridge.writer = Writer()
        sent = []

        async def send(writer, value):
            sent.append(value)

        bridge.send = send
        with pytest.raises(ActionFailedError, match="不重试"):
            await bridge.call("shell", timeout=0.02, command="input tap 1 1")
        assert [x["type"] for x in sent] == ["call", "cancel"]
        assert not bridge.pending

    asyncio.run(run())


def test_tls_identity_binding_and_disconnect(tmp_path):
    import ssl

    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        await bridge.start(0)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        async def connect(token, device):
            r, w = await asyncio.open_connection("127.0.0.1", bridge.port, ssl=ctx)
            import hashlib

            assert (
                hashlib.sha256(
                    w.get_extra_info("ssl_object").getpeercert(binary_form=True)
                ).hexdigest()
                == bridge.fingerprint
            )
            await bridge.send(w, {"v": 1, "token": token, "device_id": device})
            return r, w

        try:
            r, w = await connect("wrong", "device")
            assert await r.read() == b""
            w.close()
            await w.wait_closed()
            r, w = await connect(bridge._state["token"], "device")
            assert (await bridge.read(r))["type"] == "ready"
            assert bridge._state["device_id"] == "device"
            call = asyncio.create_task(bridge.call("shell", command="id"))
            request = await bridge.read(r)
            assert request["params"]["command"] == "id"
            w.close()
            await w.wait_closed()
            with pytest.raises(DeviceOfflineError):
                await call
            r, w = await connect(bridge._state["token"], "another-device")
            assert await r.read() == b""
            w.close()
            await w.wait_closed()
        finally:
            await bridge.close()

    asyncio.run(run())


def test_app_nodes_use_single_dump(tmp_path):
    class Bridge:
        async def call(self, op, **params):
            assert op == "nodes"
            return {"rc": 0, "stdout": base64.b64encode(b"<hierarchy/>").decode()}

    async def run():
        client = AppAdbClient(Bridge(), tmp_path)
        assert (
            await client.exec_out("uiautomator", "dump", "/dev/tty") == b"<hierarchy/>"
        )

    asyncio.run(run())
