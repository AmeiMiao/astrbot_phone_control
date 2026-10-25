"""手机抓包的配置、认证上传、启动回滚与代理所有权回归。"""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from core.app_traffic import AppTrafficCapture, populate_capture_options
from core.config import PhoneConfig
from core.traffic import render_traffic


def certificate():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timedelta, timezone

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Capture Fixture")])
    now = datetime.now(timezone.utc)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return ca.public_bytes(serialization.Encoding.PEM).decode()


class FakeBridge:
    def __init__(self):
        self.writer = object()
        self.peer = {"version": "0.4.2", "features": ["traffic_proxy"]}
        self.proxy_port = 0
        self.proxy_capture_id = ""
        self.calls = []
        self.capture_id = ""
        self.ca = certificate()
        self.traffic_handler = None

    async def call(self, op, **params):
        self.calls.append((op, params))
        if op == "traffic_prepare":
            return {"ca": self.ca}
        if op == "traffic_start":
            self.capture_id = params["capture_id"]
            return {"running": True, "capture_id": self.capture_id}
        if op == "traffic_status":
            return {"running": True, "capture_id": self.capture_id, "failures": 0}
        return {"running": False}

    async def close_proxies(self):
        self.calls.append(("close_proxies", {}))


class FakeAdb:
    def __init__(self):
        self.commands = []

    async def shell(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("id -u"):
            return "2000\n"
        return "null\n" if command == "settings get global http_proxy" else ""


class FakeLease:
    instances = []

    def __init__(self, adb, name):
        self.cleanup = ""
        self.closed = False
        self.__class__.instances.append(self)

    async def prepare(self):
        pass

    async def arm(self, pid, *, cleanup):
        self.cleanup = cleanup

    async def close(self):
        self.closed = True


def make(tmp_path, monkeypatch, ca_mode="manual", provider="app"):
    monkeypatch.setattr("core.app_traffic.DeviceLease", FakeLease)
    bridge, adb = FakeBridge(), FakeAdb()
    config = PhoneConfig.from_mapping(
        {"adb": {"mode": "app"}, "traffic": {"provider": provider, "ca_mode": ca_mode}}
    )
    capture = AppTrafficCapture(
        adb=adb,
        bridge=bridge,
        data_dir=tmp_path,
        settings_provider=lambda: config.traffic,
    )
    return capture, bridge, adb


def row():
    return {
        "method": "GET",
        "url": "https://fixture.test/api?token=secret-fixture",
        "status": 200,
        "ctype": "application/json",
        "reqbody": "",
        "body": '{"captured":true}',
    }


def test_capture_choice_follows_saved_connection_without_overwriting_hidden_value():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
    )

    class Config(dict):
        pass

    raw = Config(adb={"mode": "app"}, traffic={"provider": "App 抓包"})
    raw.schema = copy.deepcopy(schema)
    populate_capture_options(raw)
    assert raw.schema["traffic"]["items"]["provider"]["options"] == [
        "mitmproxy",
        "App 抓包",
    ]
    assert PhoneConfig.from_mapping(raw).traffic.provider == "app"
    raw["adb"]["mode"] = "直连"
    populate_capture_options(raw)
    assert raw.schema["traffic"]["items"]["provider"]["options"] == ["mitmproxy"]
    assert raw["traffic"]["provider"] == "App 抓包"
    assert PhoneConfig.from_mapping(raw).traffic.provider == "mitmproxy"
    raw["adb"]["mode"] = "app"
    assert PhoneConfig.from_mapping(raw).traffic.provider == "app"


def test_app_capture_records_current_authenticated_batch_and_restores_lease(
    tmp_path, monkeypatch
):
    async def run():
        capture, bridge, adb = make(tmp_path, monkeypatch)
        result = await capture.start()
        assert result["ok"] and result["provider"] == "app"
        assert capture.host._proc is None
        nonce = capture._capture_id
        bridge.traffic_handler({"capture_id": "old", "records": [row()]})
        assert not capture.log_path.exists()
        bridge.traffic_handler({"capture_id": nonce, "records": [row()], "dropped": 2})
        flows = capture.recent()
        assert len(flows) == 1 and flows[0]["body"] == '{"captured":true}'
        assert "secret-fixture" not in render_traffic(flows)
        assert capture.status()["received"] == 1 and capture.status()["dropped"] == 2
        assert capture.log_path.stat().st_mode & 0o777 == 0o600
        assert all(
            "reverse" not in cmd and "settings put" not in cmd for cmd in adb.commands
        )
        await capture.stop()
        assert (
            not capture.running
            and not bridge.proxy_capture_id
            and FakeLease.instances[-1].closed
        )
        assert any(op == "traffic_stop" for op, _ in bridge.calls)

    asyncio.run(run())


def test_system_ca_without_root_fails_before_phone_proxy_changes(tmp_path, monkeypatch):
    async def run():
        capture, bridge, adb = make(tmp_path, monkeypatch, ca_mode="system")
        result = await capture.start()
        assert not result["ok"] and result["unavailable"] and "root" in result["reason"]
        assert not any(op == "traffic_start" for op, _ in bridge.calls)
        assert not any("settings put" in command for command in adb.commands)
        assert capture.host._proc is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(body="x" * 8001),
        lambda value: value.update(url="file:///private"),
        lambda value: value.update(status=True),
    ],
)
def test_invalid_phone_records_are_rejected_before_writing(
    tmp_path, monkeypatch, mutation
):
    async def run():
        capture, bridge, _ = make(tmp_path, monkeypatch)
        assert (await capture.start())["ok"]
        value = row()
        mutation(value)
        with pytest.raises(ValueError):
            bridge.traffic_handler(
                {"capture_id": capture._capture_id, "records": [value]}
            )
        assert not capture.log_path.exists()
        await capture.stop()

    asyncio.run(run())


def test_disconnect_stops_reporting_running_and_old_version_requests_update(
    tmp_path, monkeypatch
):
    async def run():
        capture, bridge, _ = make(tmp_path, monkeypatch)
        assert (await capture.start())["ok"]
        bridge.writer = None
        assert not capture.status()["running"]
        await capture.stop()
        bridge.writer = object()
        bridge.peer["features"] = []
        result = await capture.start()
        assert not result["ok"] and "更新" in result["error"]

    asyncio.run(run())


def test_system_ca_guard_precedes_mount_and_cleanup_checks_ownership(tmp_path):
    import shlex
    import subprocess
    from core.app_ca import AppCaTrust

    class RootAdb:
        def __init__(self):
            self.commands = []

        async def shell(self, command, **kwargs):
            self.commands.append(command)
            if command.startswith("id -u"):
                return "0\n"
            script = shlex.split(command)[-1]
            assert (
                subprocess.run(
                    ["bash", "-n", "-c", script], capture_output=True
                ).returncode
                == 0
            )
            if script.startswith("if [ -d"):
                return "/apex/com.android.conscrypt/cacerts\n"
            return "CA_READY\n" if "echo CA_READY" in script else ""

    async def run():
        cert = tmp_path / "ca.pem"
        cert.write_text(certificate())
        adb = RootAdb()
        guarded = []

        async def before_install(cleanup):
            assert not any("mount --bind" in command for command in adb.commands)
            guarded.append(cleanup)

        trust = AppCaTrust(adb, "a" * 32, before_install=before_install)
        await trust.install(cert)
        assert guarded and "/apex/com.android.conscrypt/cacerts" in guarded[0]
        cleanup = shlex.split(trust.cleanup_command())[-1]
        assert '"$CURRENT" = "$D"' in cleanup and ".owned-cert" in cleanup
        assert "rm -rf" not in cleanup
        await trust.close()

    asyncio.run(run())


def test_reconnected_control_session_does_not_reuse_capture_state(
    tmp_path, monkeypatch
):
    async def run():
        capture, bridge, _ = make(tmp_path, monkeypatch)
        bridge.session = "first"
        assert (await capture.start())["ok"]
        nonce = capture._capture_id
        bridge.session = "next"
        assert not capture.running
        bridge.traffic_handler({"capture_id": nonce, "records": [row()]})
        assert not capture.log_path.exists()
        await capture.stop()

    asyncio.run(run())


def test_cancel_start_closes_lease_and_preserves_cancellation(tmp_path, monkeypatch):
    async def run():
        capture, bridge, _ = make(tmp_path, monkeypatch)
        original = bridge.call
        entered = asyncio.Event()

        async def call(operation, **params):
            if operation == "traffic_start":
                entered.set()
                await asyncio.Event().wait()
            return await original(operation, **params)

        bridge.call = call
        task = asyncio.create_task(capture.start())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert FakeLease.instances[-1].closed
        assert not capture.running and not capture._capture_id

    asyncio.run(run())
