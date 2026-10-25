"""App 控制传输、配置兼容和取消的回归。"""
import asyncio
import base64
from pathlib import Path
import pytest
from core.config import PhoneConfig
from core.app_bridge import AppBridge
from core.app_adb import AppAdbClient
from core.errors import ActionFailedError, DeviceOfflineError


def test_existing_adb_defaults_and_hidden_values():
    direct = PhoneConfig.from_mapping({"adb": {"host": "device", "port": 5555}})
    app = PhoneConfig.from_mapping({"adb": {"mode": "app", "host": "device", "port": 5555}})
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
            return {"rc": 7, "stdout": base64.b64encode(b"out").decode(), "stderr": base64.b64encode(b"failed").decode()}
    async def run():
        bridge = Bridge()
        adb = AppAdbClient(bridge, tmp_path)
        result = await adb.shell_result("false", timeout=1)
        assert result == {"exit_code": 7, "stdout": "out", "stderr": "failed"}
        assert bridge.calls == 1
    asyncio.run(run())


def test_app_timeout_sends_cancel_without_replay(tmp_path):
    class Writer: pass
    async def run():
        bridge = AppBridge(tmp_path, tmp_path / "app.apk")
        bridge.writer = Writer()
        sent = []
        async def send(writer, value): sent.append(value)
        bridge.send = send
        with pytest.raises(ActionFailedError, match="不重试"):
            await bridge.call("shell", timeout=.02, command="input tap 1 1")
        assert [x["type"] for x in sent] == ["call", "cancel"]
        assert not bridge.pending
    asyncio.run(run())
