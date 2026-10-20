"""core/minitouch.py 单元测试。

minitouch 为可选后端（用户自备 binary），全部用例使用 fake ADB + fake socket，
不依赖真机；真实设备用例标记 ``integration`` 且需 ``PHONE_MINITOUCH_BINARY``。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.minitouch import (  # noqa: E402
    MinitouchClient,
    MinitouchError,
    parse_banner,
    scale_axis,
)


def run(coro):
    return asyncio.run(coro)


BANNER = "v 1\n^ 10 32767 32767 255\n$ 999\n"


class FakeMinitouchSocket:
    """记录写入命令的 socket 替身，recv 返回 banner。"""

    def __init__(self, banner: str = BANNER) -> None:
        self._banner = banner.encode()
        self.sent = bytearray()
        self.closed = False
        self._delivered = False

    def recv(self, size: int) -> bytes:
        if self._delivered:
            raise AssertionError("banner 只应读取一次")
        self._delivered = True
        return self._banner

    def sendall(self, payload: bytes) -> None:
        self.sent.extend(payload)

    def settimeout(self, value) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def lines(self) -> list[str]:
        return [line for line in self.sent.decode().split("\n") if line]


class FakeAdb:
    """minitouch 所需的最小 ADB 鸭子类型。"""

    def __init__(
        self,
        *,
        wm_output: str = "Physical size: 900x1600\n",
        ps_output: str = "",
        pid: int = 888,
        forward_list: str = "",
        fail_push: bool = False,
    ) -> None:
        self.wm_output = wm_output
        self.ps_output = ps_output
        self.pid = pid
        self.forward_list = forward_list
        self.fail_push = fail_push
        self.shell_calls: list[str] = []
        self.host_calls: list[tuple[str, ...]] = []

    async def ensure_ready(self) -> None:
        return None

    async def shell(self, command, *, timeout=None):
        self.shell_calls.append(command)
        if "wm size" in command:
            return self.wm_output
        if command.startswith("ps -A -o PID,ARGS"):
            return self.ps_output
        if command.startswith("/data/local/tmp/minitouch"):
            return f"PID={self.pid}\n"
        if "chmod" in command:
            return ""
        if command.startswith("kill") or command.startswith("rm -f"):
            return ""
        return ""

    async def run_host(self, *args, timeout=None):
        self.host_calls.append(tuple(str(arg) for arg in args))
        if args[:1] == ("push",):
            return (1, b"", b"push failed") if self.fail_push else (0, b"", b"")
        if tuple(args[:2]) == ("forward", "--list"):
            return (0, self.forward_list.encode(), b"")
        if args[:1] == ("forward",):
            return (0, b"", b"")
        return (0, b"", b"")


def make_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "minitouch"
    binary.write_bytes(b"\x7fELF-fake")
    return binary


def _patch_connect(monkeypatch, sock, client_cls=MinitouchClient):
    monkeypatch.setattr(
        client_cls,
        "_connect_tcp",
        staticmethod(lambda port, timeout: sock),
        raising=True,
    )


def test_parse_banner():
    banner = parse_banner(BANNER)
    assert banner == {
        "version": 1,
        "max_contacts": 10,
        "max_x": 32767,
        "max_y": 32767,
        "max_pressure": 255,
        "pid": 999,
    }
    with pytest.raises(MinitouchError):
        parse_banner("v 1\n$ 123\n")
    with pytest.raises(MinitouchError):
        parse_banner("v 1\n^ 10 0 100 255\n$ 1\n")


def test_scale_axis():
    assert scale_axis(450, 900, 32767) == 16384
    assert scale_axis(800, 1600, 32767) == 16384
    assert scale_axis(0, 900, 32767) == 0
    assert scale_axis(899, 900, 32767) == 32731
    assert scale_axis(1599, 1600, 32767) == 32747
    assert scale_axis(999, 900, 32767) == 32767  # clamp
    with pytest.raises(MinitouchError):
        scale_axis(0, 0, 100)


def _start_client(tmp_path, monkeypatch, adb=None, banner=BANNER):
    adb = adb or FakeAdb()
    binary = make_binary(tmp_path)
    client = MinitouchClient(adb, binary_path=binary, data_dir=tmp_path / "mt-data")
    sock = FakeMinitouchSocket(banner)
    _patch_connect(monkeypatch, sock)
    return client, adb, sock


def test_start_and_stop(tmp_path, monkeypatch):
    client, adb, sock = _start_client(tmp_path, monkeypatch)

    async def scenario():
        await client.start()
        assert client.started is True
        assert client.max_x == 32767
        assert client.max_y == 32767
        assert client.max_pressure == 255
        assert client.max_contacts == 10
        assert client.server_pid == 999  # banner 的 $ pid 优先于 echo PID
        assert client.device_size == (900, 1600)
        assert any(call[0] == "push" for call in adb.host_calls)
        assert any(call[0] == "forward" and "localabstract:minitouch" in call for call in adb.host_calls)
        await client.stop()
        assert client.started is False
        assert sock.closed is True
        assert any("kill 999" in command for command in adb.shell_calls)
        assert any(call[:2] == ("forward", "--remove") for call in adb.host_calls)
        assert any(command.startswith("rm -f /data/local/tmp/minitouch") for command in adb.shell_calls)

    run(scenario())


def test_start_is_idempotent(tmp_path, monkeypatch):
    client, adb, _sock = _start_client(tmp_path, monkeypatch)

    async def scenario():
        await client.start()
        push_count = len([call for call in adb.host_calls if call[0] == "push"])
        await client.start()
        assert len([call for call in adb.host_calls if call[0] == "push"]) == push_count
        await client.stop()

    run(scenario())


def test_start_missing_binary(tmp_path):
    client = MinitouchClient(FakeAdb(), binary_path=tmp_path / "missing-minitouch")
    with pytest.raises(MinitouchError) as excinfo:
        run(client.start())
    assert "不存在" in str(excinfo.value)


def test_start_invalid_banner_cleans_up(tmp_path, monkeypatch):
    client, adb, _sock = _start_client(tmp_path, monkeypatch, banner="v 1\n$ 1\n")
    with pytest.raises(MinitouchError):
        run(client.start())
    assert client.started is False
    assert any(call[:2] == ("forward", "--remove") for call in adb.host_calls)


def test_start_cleans_stale_processes_and_forwards(tmp_path, monkeypatch):
    adb = FakeAdb(
        ps_output=(
            "  PID ARGS\n"
            " 321 /data/local/tmp/minitouch\n"
            " 322 /data/local/tmp/minitouch-other\n"
            " 323 unrelated\n"
        ),
        forward_list="192.168.250.10:16448 tcp:31000 localabstract:minitouch\n",
    )
    client, _adb, _sock = _start_client(tmp_path, monkeypatch, adb=adb)

    async def scenario():
        await client.start()
        try:
            assert any(command.startswith("kill 321") for command in adb.shell_calls)
            assert ("forward", "--remove", "tcp:31000") in adb.host_calls
        finally:
            await client.stop()

    run(scenario())


def test_tap_protocol(tmp_path, monkeypatch):
    client, _adb, sock = _start_client(tmp_path, monkeypatch)

    async def scenario():
        await client.start()
        try:
            await client.tap(450, 800)
        finally:
            await client.stop()

    run(scenario())
    assert sock.lines() == [
        "d 0 16384 16384 255",
        "c",
        "u 0",
        "c",
    ]


def test_long_press_protocol(tmp_path, monkeypatch):
    client, _adb, sock = _start_client(tmp_path, monkeypatch)

    async def scenario():
        await client.start()
        try:
            await client.long_press(100, 200, duration_ms=20)
        finally:
            await client.stop()

    run(scenario())
    assert sock.lines() == ["d 0 3641 4096 255", "c", "u 0", "c"]


def test_swipe_coordinates_protocol(tmp_path, monkeypatch):
    client, _adb, sock = _start_client(tmp_path, monkeypatch)

    async def scenario():
        await client.start()
        try:
            await client.swipe(0, 0, 899, 1599, duration_ms=64)
        finally:
            await client.stop()

    run(scenario())
    commands = sock.lines()
    assert commands[0].startswith("d 0 0 0 255")
    assert commands[1] == "c"
    moves = [line for line in commands if line.startswith("m 0")]
    assert len(moves) >= 2
    assert moves[-1].split()[1:] == ["0", "32731", "32747", "255"]
    assert commands[-2:] == ["u 0", "c"]


def test_swipe_direction_supported(tmp_path, monkeypatch):
    client, _adb, sock = _start_client(tmp_path, monkeypatch)

    async def scenario():
        await client.start()
        try:
            # 兼容 plugin-agent 后端管理器的 swipe(direction, duration_ms=...) 调用
            await client.swipe("up", duration_ms=64)
        finally:
            await client.stop()

    run(scenario())
    commands = sock.lines()
    assert commands[0].startswith("d 0")
    assert any(line.startswith("m 0") for line in commands)
    assert commands[-2:] == ["u 0", "c"]


def test_invalid_arguments(tmp_path, monkeypatch):
    client, _adb, _sock = _start_client(tmp_path, monkeypatch)

    async def scenario():
        with pytest.raises(MinitouchError):
            await client.tap(10, 10)  # 未启动
        await client.start()
        try:
            with pytest.raises(MinitouchError):
                await client.tap(900, 100)  # 越界
            with pytest.raises(MinitouchError):
                await client.long_press(10, 10, duration_ms=120_000)
            with pytest.raises(MinitouchError):
                await client.swipe("diagonal")
        finally:
            await client.stop()

    run(scenario())


def test_stop_is_idempotent(tmp_path, monkeypatch):
    client, _adb, _sock = _start_client(tmp_path, monkeypatch)

    async def scenario():
        await client.start()
        await client.stop()
        await client.stop()
        assert client.started is False

    run(scenario())


def test_start_push_failure_raises(tmp_path, monkeypatch):
    adb = FakeAdb(fail_push=True)
    client, _adb, _sock = _start_client(tmp_path, monkeypatch, adb=adb)
    with pytest.raises(MinitouchError) as excinfo:
        run(client.start())
    assert "push" in str(excinfo.value)


# ----------------------------------------------------------------------
# 真实设备集成测试（可选，默认跳过）
# ----------------------------------------------------------------------
ADB_HOST = os.environ.get("PHONE_ADB_HOST", "")
ADB_PORT = int(os.environ.get("PHONE_ADB_PORT", "16448"))
ADB_PATH = os.environ.get(
    "PHONE_ADB_PATH",
    "/home/dev/acat-workspace/.cache/work/android-platform-tools/platform-tools/adb",
)
ADB_HOME = os.environ.get(
    "PHONE_ADB_HOME",
    "/home/dev/acat-workspace/.cache/work/android-home",
)
MINITOUCH_BINARY = os.environ.get("PHONE_MINITOUCH_BINARY", "")


@pytest.mark.integration
@pytest.mark.skipif(
    not ADB_HOST or not MINITOUCH_BINARY,
    reason="需要 PHONE_ADB_HOST 与 PHONE_MINITOUCH_BINARY（用户自备 minitouch 二进制）",
)
def test_real_device_minitouch_optional():
    from core.adb import AdbClient

    async def scenario():
        adb = AdbClient(ADB_PATH, ADB_HOST, ADB_PORT, ADB_HOME, connect_timeout=20.0)
        await adb.connect()
        client = MinitouchClient(
            adb,
            binary_path=MINITOUCH_BINARY,
            logger=lambda message: None,
            data_dir=Path("/tmp/minitouch-it"),
        )
        try:
            await client.start()
            assert client.started is True
            assert client.max_x > 0 and client.max_y > 0
            await client.tap(450, 800)
        finally:
            await client.stop()
            await adb.close()

    run(scenario())
