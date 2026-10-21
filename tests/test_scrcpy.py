"""core/scrcpy.py 单元测试。

覆盖：
* 控制消息 struct 编码（对照 scrcpy v5.0.1 客户端 ``test_control_msg_serialize.c`` 期望值）；
* raw H.264 / JPEG 流解析、尺寸计算、forward 解析；
* 启动/停止/幂等/错误路径（mock ADB、socket、ffmpeg 进程）；
* 视频解码管道（shell 管道 + 单文件 JPEG tail）与 latest_frame 环形缓冲；
* 断流后最多一次自动重连。

真实设备用例标记 ``integration``，仅在设置 ``PHONE_ADB_HOST`` 时执行。
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import socket
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.scrcpy as scrcpy_module  # noqa: E402
from core.errors import AdbCommandError  # noqa: E402
from core.scrcpy import (  # noqa: E402
    ScrcpyClient,
    ScrcpyError,
    ScrcpyFrame,
    compute_video_size,
    count_annex_b_nals,
    encode_back,
    encode_key,
    encode_rotate,
    encode_scroll,
    encode_text,
    encode_touch,
    is_annex_b_stream,
    parse_forward_list,
    parse_jpeg_size,
    resolve_keycode,
    MjpegFrameParser,
)


def run(coro):
    """在独立事件循环中执行协程（测试环境未安装 pytest-asyncio）。"""
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 测试素材
# ----------------------------------------------------------------------
def make_test_jpeg(width: int = 576, height: int = 1024) -> bytes:
    """构造包含 SOI + SOF0 的最小 JPEG（足够尺寸解析/MJPEG 分帧测试）。"""
    sof_payload = (
        b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    sof = b"\xff\xc0" + struct.pack(">H", len(sof_payload) + 2) + sof_payload
    body = b"\x00" * 256
    return b"\xff\xd8" + sof + body + b"\xff\xd9"


def make_mjpeg_stream(*sizes: tuple[int, int]) -> bytes:
    return b"".join(make_test_jpeg(width, height) for width, height in sizes)


_SERVER_NEEDLE = "app_process / com.genymobile.scrcpy.Server"


class FakeAdb:
    """ScrcpyClient 所需的最小 ADB 鸭子类型（shell + run_host）。"""

    def __init__(
        self,
        *,
        wm_output: str = "Physical size: 900x1600\n",
        ps_output: str = "",
        pid: int = 4321,
        forward_list: str = "",
        unix_count: str = "1\n",
        fail_shell: bool = False,
        fail_host: bool = False,
        jar_present: bool = True,
    ) -> None:
        self.wm_output = wm_output
        self.ps_output = ps_output
        self.pid = pid
        self.forward_list = forward_list
        self.unix_count = unix_count
        self.fail_shell = fail_shell
        self.fail_host = fail_host
        # 远端 jar 是否已存在且大小一致（False 时启动脚本会要求 adb push）
        self.jar_present = jar_present
        self.shell_calls: list[str] = []
        self.launch_calls = 0
        self.host_calls: list[tuple[str, ...]] = []

    @property
    def serial(self) -> str:
        return "192.168.250.10:16448"

    async def ensure_ready(self) -> None:
        return None

    async def shell(self, command, *, timeout=None):
        self.shell_calls.append(command)
        if self.fail_shell:
            raise AdbCommandError("adb shell 失败")
        if "__JAR_SIZE__" in command:
            # 合并启动脚本：清理残留 + 校验远端 jar + 启动 server
            match = re.search(r'if \[ "\$SIZE" = "(\d+)" \]', command)
            expected = match.group(1) if match else "0"
            if self.jar_present:
                self.launch_calls += 1
                return f"__JAR_SIZE__={expected}\nPID={self.pid}\nGUARD_PID=9999\n"
            return "__JAR_SIZE__=-1\n__JAR_MISSING__\n"
        if command.startswith("CLASSPATH=") and _SERVER_NEEDLE in command:
            self.launch_calls += 1
            return f"PID={self.pid}\nGUARD_PID=9999\n"
        if "wm size" in command:
            return self.wm_output
        if command.startswith("ps -A -o PID,ARGS"):
            return self.ps_output
        if "app_process / com.genymobile.scrcpy.Server" in command:
            return f"PID={self.pid}\nGUARD_PID=9999\n"
        if "cat /proc/net/unix" in command:
            return self.unix_count
        if command.startswith("kill"):
            return ""
        if command.startswith("rm -f") or command.startswith("chmod"):
            return ""
        if "tail" in command:
            return ""
        return ""

    async def run_host(self, *args, timeout=None):
        self.host_calls.append(tuple(str(arg) for arg in args))
        if self.fail_host:
            return (1, b"", b"forward failed")
        if args[:1] == ("push",):
            self.jar_present = True
            return (0, b"", b"")
        if tuple(args[:2]) == ("forward", "--list"):
            return (0, self.forward_list.encode(), b"")
        if args[:1] == ("forward",):
            return (0, b"", b"")
        return (0, b"", b"")

    # 便捷断言
    def pushed(self) -> list[str]:
        return [call[0] for call in self.host_calls if call and call[0] == "push"]

    def forwards(self) -> list[tuple[str, ...]]:
        return [call for call in self.host_calls if call and call[0] == "forward"]


class FakeProcess:
    """模拟解码管道 Popen 进程。"""

    def __init__(self, *, pid: int = 5555, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.waited = 0
        self.stdin = io.BytesIO()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waited += 1
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakeSocket:
    """记录 sendall 的 socket 替身（用于控制通道断言）。"""

    def __init__(self) -> None:
        self.sent = bytearray()
        self.closed = False
        self.timeout = None

    def sendall(self, payload: bytes) -> None:
        self.sent.extend(payload)

    def settimeout(self, value) -> None:
        self.timeout = value

    def fileno(self) -> int:
        return -1

    def recv(self, size: int, flags: int = 0) -> bytes:  # pragma: no cover - 未使用
        return b""

    def close(self) -> None:
        self.closed = True


def make_client(tmp_path: Path, adb=None, **overrides) -> ScrcpyClient:
    kwargs = {
        "server_jar": tmp_path / "scrcpy-server-v5.0.1",
        "max_size": 1024,
        "data_dir": tmp_path / "scrcpy-data",
    }
    kwargs.update(overrides)
    jar = kwargs["server_jar"]
    Path(jar).parent.mkdir(parents=True, exist_ok=True)
    if not Path(jar).exists():
        Path(jar).write_bytes(b"fake-jar")
    return ScrcpyClient(adb or FakeAdb(), **kwargs)


async def _fake_connect_sockets_success(client: ScrcpyClient, port: int):
    """替换 _connect_sockets：视频模式返回 video+control，仅控制模式只返回 control。"""
    control_parent, control_peer = socket.socketpair()
    client._test_control_peer = control_peer  # type: ignore[attr-defined]
    if not client.video:
        return None, control_parent
    video_parent, video_peer = socket.socketpair()
    client._test_video_peer = video_peer  # type: ignore[attr-defined]
    client._video_stream_head_bytes = b"\x00\x00\x00\x01\x67"
    return video_parent, control_parent


async def _start_with_fakes(
    tmp_path: Path,
    monkeypatch,
    *,
    adb=None,
    enable_decoder: bool = True,
    ffmpeg: str | None = "/usr/bin/fake-ffmpeg",
    process: FakeProcess | None = None,
    video: bool = True,
):
    """启动一个使用假 socket/假 ffmpeg 进程的 ScrcpyClient。"""
    adb = adb or FakeAdb()
    client = make_client(tmp_path, adb=adb, video=video)
    monkeypatch.setattr(
        ScrcpyClient, "_connect_sockets", _fake_connect_sockets_success, raising=True
    )
    monkeypatch.setattr(
        ScrcpyClient,
        "_resolve_ffmpeg",
        (lambda self: ffmpeg) if enable_decoder else (lambda self: None),
        raising=True,
    )
    # 测试 ffmpeg 管道路径：容器若装了 PyAV 也强制走 ffmpeg 分支
    monkeypatch.setattr(
        ScrcpyClient,
        "_av_decoder_available",
        staticmethod(lambda: False),
        raising=True,
    )
    fake_process = process or FakeProcess()
    monkeypatch.setattr(
        scrcpy_module.subprocess,
        "Popen",
        lambda *args, **kwargs: fake_process,
        raising=True,
    )
    await client.start()
    return client, adb, fake_process


# ----------------------------------------------------------------------
# 控制消息编码（对照 scrcpy 客户端测试向量）
# ----------------------------------------------------------------------
def test_encode_key_matches_scrcpy_test_vector():
    # test_serialize_inject_keycode: UP / ENTER(66) / repeat 5 / SHIFT_ON|SHIFT_LEFT_ON(0x41)
    payload = encode_key(1, 66, repeat=5, metastate=0x41)
    assert payload == bytes(
        [
            0,
            0x01,
            0x00,
            0x00,
            0x00,
            0x42,
            0x00,
            0x00,
            0x00,
            0x05,
            0x00,
            0x00,
            0x00,
            0x41,
        ]
    )
    assert len(payload) == 14


def test_encode_text_matches_scrcpy_test_vector():
    payload = encode_text("hello, world!")
    expected = bytes([1, 0x00, 0x00, 0x00, 0x0D]) + b"hello, world!"
    assert payload == expected
    assert len(payload) == 18


def test_encode_text_truncates_to_300_bytes():
    payload = encode_text("a" * 400)
    assert len(payload) == 1 + 4 + 300
    assert payload[1:5] == struct.pack(">I", 300)


def test_encode_touch_matches_scrcpy_test_vector():
    payload = encode_touch(
        0,
        100,
        200,
        1080,
        1920,
        pressure=0xFFFF,
        pointer_id=0x1234567887654321,
        action_button=1,
        buttons=1,
    )
    expected = bytes(
        [
            2,
            0,
            0x12,
            0x34,
            0x56,
            0x78,
            0x87,
            0x65,
            0x43,
            0x21,
            0x00,
            0x00,
            0x00,
            0x64,
            0x00,
            0x00,
            0x00,
            0xC8,
            0x04,
            0x38,
            0x07,
            0x80,
            0xFF,
            0xFF,
            0x00,
            0x00,
            0x00,
            0x01,
            0x00,
            0x00,
            0x00,
            0x01,
        ]
    )
    assert payload == expected
    assert len(payload) == 32


def test_encode_scroll_matches_scrcpy_test_vector():
    payload = encode_scroll(260, 1026, 1080, 1920, hscroll=16, vscroll=-16, buttons=1)
    expected = bytes(
        [
            3,
            0x00,
            0x00,
            0x01,
            0x04,
            0x00,
            0x00,
            0x04,
            0x02,
            0x04,
            0x38,
            0x07,
            0x80,
            0x7F,
            0xFF,
            0x80,
            0x00,
            0x00,
            0x00,
            0x00,
            0x01,
        ]
    )
    assert payload == expected
    assert len(payload) == 21


def test_encode_back_and_rotate():
    assert encode_back(0) == b"\x04\x00"
    assert encode_back(1) == b"\x04\x01"
    assert encode_rotate() == b"\x0b"


def test_encode_touch_rejects_bad_dimensions():
    with pytest.raises(ScrcpyError):
        encode_touch(0, 0, 0, 70000, 100)


def test_resolve_keycode():
    assert resolve_keycode(3) == 3
    assert resolve_keycode("3") == 3
    assert resolve_keycode("enter") == 66
    assert resolve_keycode("HOME") == 3
    assert resolve_keycode("volume_up") == 24
    with pytest.raises(ScrcpyError):
        resolve_keycode("teleport")
    with pytest.raises(ScrcpyError):
        resolve_keycode("")
    with pytest.raises(ScrcpyError):
        resolve_keycode(True)


# ----------------------------------------------------------------------
# 流/尺寸/forward 解析
# ----------------------------------------------------------------------
def test_compute_video_size():
    assert compute_video_size(900, 1600, 1024) == (576, 1024)
    assert compute_video_size(1080, 1920, 1024) == (576, 1024)
    assert compute_video_size(1600, 900, 1024) == (1024, 576)
    assert compute_video_size(900, 1600, 0) == (900, 1600)
    assert compute_video_size(900, 1600, 2000) == (900, 1600)
    assert compute_video_size(0, 0, 1024) == (1, 1)


def test_parse_jpeg_size():
    assert parse_jpeg_size(make_test_jpeg(576, 1024)) == (576, 1024)
    assert parse_jpeg_size(make_test_jpeg(320, 240)) == (320, 240)
    assert parse_jpeg_size(b"\x00" * 32) is None
    assert parse_jpeg_size(b"") is None


def test_mjpeg_parser_single_and_split_frames():
    frame = make_test_jpeg()
    parser = MjpegFrameParser()
    assert parser.feed(frame) == [frame]
    parser = MjpegFrameParser()
    assert parser.feed(frame[:40]) == []
    assert parser.feed(frame[40:]) == [frame]


def test_mjpeg_parser_multiple_frames_and_garbage():
    first = make_test_jpeg(320, 240)
    second = make_test_jpeg(640, 480)
    parser = MjpegFrameParser()
    frames = parser.feed(b"leading-garbage" + first + second)
    assert frames == [first, second]


def test_mjpeg_parser_partial_buffer_not_returned():
    frame = make_test_jpeg()
    parser = MjpegFrameParser()
    assert parser.feed(frame[:-2]) == []
    assert parser.feed(frame[-2:]) == [frame]


def test_annex_b_helpers():
    stream = b"\x00\x00\x00\x01\x67abc\x00\x00\x00\x01\x68def"
    assert is_annex_b_stream(stream)
    assert is_annex_b_stream(b"\x00\x00\x01\x67")
    assert not is_annex_b_stream(b"\x00\x00\x02")
    assert count_annex_b_nals(stream) == 2


def test_parse_forward_list():
    text = (
        "192.168.250.10:16448 tcp:27183 localabstract:scrcpy\n"
        "emulator-5554 tcp:28000 localabstract:minitouch\n"
        "garbage line\n"
    )
    forwards = parse_forward_list(text)
    assert forwards == [
        ("192.168.250.10:16448", "tcp:27183", "localabstract:scrcpy"),
        ("emulator-5554", "tcp:28000", "localabstract:minitouch"),
    ]
    assert parse_forward_list("") == []


# ----------------------------------------------------------------------
# 启动/停止（mock ADB + socket + 解码进程）
# ----------------------------------------------------------------------
def test_start_and_stop_basic(tmp_path, monkeypatch):
    async def scenario():
        client, adb, process = await _start_with_fakes(tmp_path, monkeypatch)
        try:
            assert client.started is True
            assert client.server_pid == 4321
            assert client.device_size == (900, 1600)
            assert client.video_size == (576, 1024)
            assert client.video_stream_head.startswith(b"\x00\x00\x00\x01")
            caps = client.capabilities
            assert caps["control"] is True
            assert caps["decoder"] is True
            assert caps["video"] is False  # 还没有解码出帧
            assert caps["server_jar"].endswith("scrcpy-server-v5.0.1")
        finally:
            await client.stop()
        assert client.started is False
        assert process.terminated or process.killed
        # 清理动作：移除 forward + kill server；远端 jar/log 保留供下次启动复用
        assert any(call[:2] == ("forward", "--remove") for call in adb.host_calls)
        assert any("kill 4321" in command for command in adb.shell_calls)
        assert not any(
            command.startswith("rm -f /data/local/tmp/scrcpy-server")
            for command in adb.shell_calls
        )
        for attr in ("_video_socket", "_control_socket"):
            assert getattr(client, attr) is None

    run(scenario())


def test_video_start_has_no_dummy_byte(tmp_path, monkeypatch):
    """视频模式 raw_stream 默认不写 dummy，用 MSG_PEEK 等首字节（不消费 H264）。"""

    async def scenario():
        adb = FakeAdb()
        client, adb, _process = await _start_with_fakes(tmp_path, monkeypatch, adb=adb)
        try:
            scripts = [c for c in adb.shell_calls if "__JAR_SIZE__" in c]
            assert scripts
            assert "send_dummy_byte" not in scripts[0]
            assert "video=false" not in scripts[0]
            assert "max_size=" in scripts[0]
        finally:
            await client.stop()

    run(scenario())


def test_control_ready_reads_dummy_byte(monkeypatch):
    """control-only：连接后读 1 字节 dummy 判定就绪。"""
    client = make_client(Path("/tmp/scrcpy-control-dummy"), video=False)
    parent, peer = socket.socketpair()
    monkeypatch.setattr(
        ScrcpyClient, "_connect_tcp", staticmethod(lambda port, timeout: parent), raising=True
    )
    peer.sendall(b"\x00")
    sock = client._connect_ready_control(12345)
    assert sock is parent
    sock.settimeout(0.2)
    # dummy 已被消费，后续无数据
    with pytest.raises(socket.timeout):
        sock.recv(1)
    sock.close()
    peer.close()


def test_video_connect_peeks_without_consuming_stream(monkeypatch):
    """视频模式就绪判定用 MSG_PEEK，不得消费 H264 首字节。"""
    client = make_client(Path("/tmp/scrcpy-video-peek"))
    video_parent, video_peer = socket.socketpair()
    control_parent, control_peer = socket.socketpair()
    head = b"\x00\x00\x00\x01\x67\x42"
    video_peer.sendall(head)
    sockets = iter([video_parent, control_parent])

    def fake_connect_tcp(port, timeout):
        return next(sockets)

    monkeypatch.setattr(
        ScrcpyClient, "_connect_tcp", staticmethod(fake_connect_tcp), raising=True
    )
    video, control = run(client._connect_sockets(12345))
    assert video is video_parent and control is control_parent
    # 流头未被消费：仍然是完整首包
    assert ScrcpyClient._wait_video_readable(video, 0.5) == head
    video.close()
    control.close()
    video_peer.close()
    control_peer.close()


def test_start_is_idempotent(tmp_path, monkeypatch):
    async def scenario():
        client, adb, _process = await _start_with_fakes(tmp_path, monkeypatch)
        try:
            push_count = len(adb.pushed())
            assert push_count == 0  # 远端 jar 大小一致时跳过 adb push
            await client.start()
            assert len(adb.pushed()) == push_count
            assert adb.launch_calls == 1
        finally:
            await client.stop()

    run(scenario())


def test_start_requires_server_jar(tmp_path):
    client = ScrcpyClient(FakeAdb(), server_jar=tmp_path / "missing-jar")
    with pytest.raises(ScrcpyError) as excinfo:
        run(client.start())
    assert "server jar" in str(excinfo.value)


def test_start_cleans_stale_servers_and_forwards(tmp_path, monkeypatch):
    adb = FakeAdb(
        ps_output=(
            "  PID ARGS\n"
            " 111 app_process / com.genymobile.scrcpy.Server 5.0.1 log_level=info\n"
            " 333 some.other.process\n"
        ),
        forward_list=(
            "192.168.250.10:16448 tcp:30001 localabstract:scrcpy\n"
            "192.168.250.10:16448 tcp:30002 localabstract:other\n"
        ),
    )

    async def scenario():
        client, _adb, _process = await _start_with_fakes(tmp_path, monkeypatch, adb=adb)
        try:
            # 合并启动脚本：一次 adb shell 里完成残留 server 清理 + jar 校验 + 启动
            scripts = [
                command for command in adb.shell_calls if "__JAR_SIZE__" in command
            ]
            assert scripts
            script = scripts[0]
            assert "ps -A -o PID,ARGS" in script
            assert "app_process / com.genymobile.scrcpy.Serve[r]" in script
            assert "kill $PIDS" in script
            # 脚本自身命令行含 launch 字面量，必须过滤 $$ 避免 kill 掉当前 adb shell
            assert "awk -v self=$$" in script
            # 只移除指向 scrcpy 的残留 forward，不动无关 forward
            assert ("forward", "--remove", "tcp:30001") in adb.host_calls
            assert ("forward", "--remove", "tcp:30002") not in adb.host_calls
            # 端口 forward 指向本次 scid 的独立 socket 名
            forward_adds = [
                call
                for call in adb.host_calls
                if call[:1] == ("forward",) and call[1:2] != ("--list",)
                and call[1:2] != ("--remove",)
            ]
            assert forward_adds
            remote = forward_adds[0][-1]
            assert remote.startswith("localabstract:scrcpy_")
            assert remote.endswith(f"{client._scid:08x}")
        finally:
            await client.stop()

    run(scenario())


def test_start_without_ffmpeg_keeps_control(tmp_path, monkeypatch):
    async def scenario():
        client, _adb, _process = await _start_with_fakes(
            tmp_path, monkeypatch, enable_decoder=False
        )
        try:
            caps = client.capabilities
            assert caps["decoder"] is False
            assert caps["control"] is True
            assert caps["video"] is False
            assert "ffmpeg" in client.last_error
        finally:
            await client.stop()

    run(scenario())


def test_start_failure_when_connect_never_succeeds(tmp_path, monkeypatch):
    adb = FakeAdb()
    client = make_client(tmp_path, adb=adb)
    monkeypatch.setattr(scrcpy_module, "_START_TIMEOUT_S", 0.3, raising=True)

    async def fake_connect_fail(self, port):
        raise ScrcpyError("连接 scrcpy 端口失败：Connection refused")

    monkeypatch.setattr(ScrcpyClient, "_connect_sockets", fake_connect_fail)
    with pytest.raises(ScrcpyError):
        run(client.start())
    assert client.started is False
    # 失败后仍尽力移除 forward / kill server
    assert any(call[:2] == ("forward", "--remove") for call in adb.host_calls)


def test_connect_retries_until_server_ready(monkeypatch):
    """首次连接收不到视频数据（server 未就绪）时重试，成功后立即返回。"""
    client = make_client(Path("/tmp/scrcpy-connect-retry"))

    pairs = [socket.socketpair() for _ in range(4)]
    control_parent, control_peer = socket.socketpair()
    sockets = iter(
        [
            pairs[0][0],
            pairs[1][0],
            pairs[2][0],
            control_parent,
        ]
    )

    def fake_connect_tcp(port, timeout):
        return next(sockets)

    peeks = {"count": 0}

    def fake_wait_video_readable(sock, timeout):
        peeks["count"] += 1
        return b"" if peeks["count"] == 1 else b"\x00\x00\x00\x01"

    monkeypatch.setattr(
        ScrcpyClient, "_connect_tcp", staticmethod(fake_connect_tcp), raising=True
    )
    monkeypatch.setattr(
        ScrcpyClient, "_wait_video_readable", staticmethod(fake_wait_video_readable)
    )
    monkeypatch.setattr(scrcpy_module, "_CONNECT_RETRY_INTERVAL_S", 0.001, raising=True)
    video, control = run(client._connect_sockets(12345))
    assert peeks["count"] == 2
    assert video is pairs[2][0]
    assert control is control_parent
    for parent, peer in pairs:
        if parent is not video:
            try:
                parent.close()
            except OSError:
                pass
        peer.close()
    video.close()
    control.close()
    control_peer.close()


def test_connect_control_only_single_socket(monkeypatch):
    """仅控制通道：只连接 control socket，不读视频、不连第二个 socket。"""
    client = make_client(Path("/tmp/scrcpy-connect-control"), video=False)
    control_parent, control_peer = socket.socketpair()
    calls = {"ready": 0, "tcp": 0}

    def fake_connect_ready_control(self, port):
        calls["ready"] += 1
        return control_parent

    def fake_tcp(port, timeout):
        calls["tcp"] += 1
        return socket.socketpair()[0]

    monkeypatch.setattr(ScrcpyClient, "_connect_ready_control", fake_connect_ready_control)
    monkeypatch.setattr(ScrcpyClient, "_connect_tcp", staticmethod(fake_tcp), raising=True)
    video, control = run(client._connect_sockets(12345))
    assert video is None
    assert control is control_parent
    assert calls == {"ready": 1, "tcp": 0}
    control.close()
    control_peer.close()


def test_wait_video_readable_timeout():
    client = make_client(Path("/tmp/scrcpy-peek-test"))
    a, b = socket.socketpair()
    try:
        assert ScrcpyClient._wait_video_readable(a, 0.2) == b""
        b.sendall(b"\x00\x00\x00\x01")
        assert ScrcpyClient._wait_video_readable(a, 0.5) == b"\x00\x00\x00\x01"
        # MSG_PEEK 不消费数据
        assert ScrcpyClient._wait_video_readable(a, 0.5) == b"\x00\x00\x00\x01"
    finally:
        a.close()
        b.close()


# ----------------------------------------------------------------------
# 解码管道 / 帧缓冲
# ----------------------------------------------------------------------
def test_decoder_tail_reads_latest_frame(tmp_path, monkeypatch):
    async def scenario():
        client, _adb, _process = await _start_with_fakes(tmp_path, monkeypatch)
        try:
            decoder_file = client._decoder_file
            assert decoder_file is not None
            assert decoder_file.name == "scrcpy_live.jpg"
            decoder_file.write_bytes(make_test_jpeg(576, 1024))
            for _ in range(50):
                await asyncio.sleep(0.02)
                frame = client.latest_frame()
                if frame is not None:
                    break
            frame = client.latest_frame()
            assert frame is not None
            assert (frame.width, frame.height) == (576, 1024)
            assert frame.data.startswith(b"\xff\xd8")
            assert frame.mime == "image/jpeg"
            assert client.frame_count >= 1
            assert client.capabilities["video"] is True
            assert client.video_size == (576, 1024)
        finally:
            await client.stop()

    run(scenario())


def test_latest_frame_max_age(tmp_path, monkeypatch):
    async def scenario():
        client, _adb, _process = await _start_with_fakes(tmp_path, monkeypatch)
        try:
            client._store_frame(make_test_jpeg(320, 240))
            frame = client.latest_frame()
            assert frame is not None and (frame.width, frame.height) == (320, 240)
            assert client.latest_frame(max_age_ms=10_000) is not None
            await asyncio.sleep(0.05)
            assert client.latest_frame(max_age_ms=10) is None
        finally:
            await client.stop()

    run(scenario())


def test_latest_frame_empty():
    client = ScrcpyClient(FakeAdb(), server_jar=Path("/tmp/whatever-jar"))
    assert client.latest_frame() is None


def test_decoder_missing_ffmpeg_starts_drain_only(tmp_path, monkeypatch):
    async def scenario():
        client, _adb, _process = await _start_with_fakes(
            tmp_path, monkeypatch, enable_decoder=False
        )
        try:
            assert client._decoder_process is None
            assert client._drain_thread is not None
            assert client.last_error
        finally:
            await client.stop()

    run(scenario())


def test_decoder_exit_schedules_single_reconnect(tmp_path, monkeypatch):
    async def scenario():
        client, adb, process = await _start_with_fakes(tmp_path, monkeypatch)
        reconnect_calls: list[int] = []
        original = client._reconnect_once

        async def fake_reconnect():
            reconnect_calls.append(1)

        monkeypatch.setattr(client, "_reconnect_once", fake_reconnect)
        try:
            process.returncode = 1
            client._on_decoder_lost("解码管道退出（code=1）")
            await asyncio.sleep(0.1)
            assert len(reconnect_calls) == 1
            # 只允许一次
            client._on_decoder_lost("再次中断")
            await asyncio.sleep(0.1)
            assert len(reconnect_calls) == 1
            assert "解码中断" in client.last_error
        finally:
            client._stopping = True
            await client.stop()
            monkeypatch.setattr(client, "_reconnect_once", original)

    run(scenario())


# ----------------------------------------------------------------------
# 控制动作（发送字节断言）
# ----------------------------------------------------------------------
def _peer_bytes(sock: socket.socket, expected_len: int, timeout: float = 1.0) -> bytes:
    sock.settimeout(timeout)
    data = b""
    while len(data) < expected_len:
        chunk = sock.recv(expected_len - len(data))
        if not chunk:
            break
        data += chunk
    return data


def test_tap_sends_scaled_touch(tmp_path, monkeypatch):
    async def scenario():
        client, _adb, _process = await _start_with_fakes(tmp_path, monkeypatch)
        control_peer: socket.socket = client._test_control_peer  # type: ignore[attr-defined]
        try:
            await client.tap(184, 1043)
            payload = _peer_bytes(control_peer, 64)
        finally:
            await client.stop()
        # 设备 900x1600 × 视频 576x1024：x=184 -> 118，y=1043 -> 668
        down = encode_touch(0, 118, 668, 576, 1024, pressure=0xFFFF)
        up = encode_touch(1, 118, 668, 576, 1024, pressure=0)
        assert payload == down + up

    run(scenario())


def test_long_press_press_key_back_inject_text(tmp_path, monkeypatch):
    async def scenario():
        client, _adb, _process = await _start_with_fakes(tmp_path, monkeypatch)
        control_peer: socket.socket = client._test_control_peer  # type: ignore[attr-defined]
        try:
            await client.press_key("home")
            assert _peer_bytes(control_peer, 28) == encode_key(0, 3) + encode_key(1, 3)
            await client.back()
            assert _peer_bytes(control_peer, 4) == b"\x04\x00\x04\x01"
            assert await client.inject_text("hello") is True
            assert _peer_bytes(control_peer, 10) == encode_text("hello")
            assert await client.inject_text("你好") is False
            assert await client.inject_text("a\nb") is False
        finally:
            await client.stop()

    run(scenario())


def test_invalid_control_arguments(tmp_path, monkeypatch):
    async def scenario():
        client, _adb, _process = await _start_with_fakes(tmp_path, monkeypatch)
        try:
            with pytest.raises(ScrcpyError):
                await client.tap(900, 100)  # 越界
            with pytest.raises(ScrcpyError):
                await client.tap(-1, 0)
            with pytest.raises(ScrcpyError):
                await client.press_key("teleport")
            with pytest.raises(ScrcpyError):
                await client.swipe_direction("diagonal")
            with pytest.raises(ScrcpyError):
                await client.long_press(100, 100, duration_ms=120_000)
        finally:
            await client.stop()

    run(scenario())


def test_control_requires_started():
    client = ScrcpyClient(FakeAdb(), server_jar=Path("/tmp/whatever-jar"))
    with pytest.raises(ScrcpyError):
        run(client.tap(10, 10))
    assert run(client.inject_text("hello")) is False


def test_swipe_sends_down_moves_up(tmp_path, monkeypatch):
    async def scenario():
        client, _adb, _process = await _start_with_fakes(tmp_path, monkeypatch)
        control_peer: socket.socket = client._test_control_peer  # type: ignore[attr-defined]
        try:
            await client.swipe(450, 1200, 450, 400, duration_ms=100)
            payload = _peer_bytes(control_peer, 32 * 8, timeout=2.0)
        finally:
            await client.stop()
        assert len(payload) % 32 == 0
        messages = [payload[i : i + 32] for i in range(0, len(payload), 32)]
        assert len(messages) >= 4
        assert messages[0][:2] == bytes([2, 0])  # touch down
        assert messages[-1][:2] == bytes([2, 1])  # touch up
        # 中间为 move 事件
        assert any(message[:2] == bytes([2, 2]) for message in messages)

    run(scenario())


def test_get_device_size_cached(tmp_path, monkeypatch):
    async def scenario():
        client, adb, _process = await _start_with_fakes(tmp_path, monkeypatch)
        try:
            before = sum(1 for command in adb.shell_calls if "wm size" in command)
            assert await client.get_device_size() == (900, 1600)
            assert await client.get_device_size() == (900, 1600)
            after = sum(1 for command in adb.shell_calls if "wm size" in command)
            assert after == before
        finally:
            await client.stop()

    run(scenario())


# ----------------------------------------------------------------------
# 仅控制通道（video=False）
# ----------------------------------------------------------------------
def test_control_only_start_caps_and_single_socket(tmp_path, monkeypatch):
    async def scenario():
        adb = FakeAdb()
        client, adb, _process = await _start_with_fakes(
            tmp_path, monkeypatch, adb=adb, video=False
        )
        try:
            scripts = [c for c in adb.shell_calls if "__JAR_SIZE__" in c]
            assert scripts
            script = scripts[0]
            assert "video=false " in script
            assert "max_size=" not in script
            assert "scid=" in script
            assert "control=true" in script
            # raw_stream 会关闭 dummy；control-only 必须在其后显式重开用于就绪判定
            assert "raw_stream=true send_dummy_byte=true" in script
            caps = client.capabilities
            assert caps["control"] is True
            assert caps["video"] is False
            assert caps["decoder"] is False
            assert client.latest_frame() is None
            assert client.video_stream_head == b""
            assert client._decoder_process is None
            assert client._drain_thread is None
            assert client._video_socket is None
            assert client._control_socket is not None
            # forward 指向本次 scid 的 socket 名
            forward_adds = [
                call
                for call in adb.host_calls
                if call[:1] == ("forward",) and call[1:2] not in {("--list",), ("--remove",)}
            ]
            assert forward_adds
            remote = forward_adds[0][-1]
            assert remote == f"localabstract:scrcpy_{client._scid:08x}"
            # 懒查询设备分辨率：仅控制通道以设备像素作为控制坐标
            assert await client.get_device_size() == (900, 1600)
            assert client.video_size == (900, 1600)
        finally:
            await client.stop()
        assert client.started is False

    run(scenario())


def test_control_only_tap_uses_device_pixels(tmp_path, monkeypatch):
    async def scenario():
        client, _adb, _process = await _start_with_fakes(
            tmp_path, monkeypatch, video=False
        )
        control_peer: socket.socket = client._test_control_peer  # type: ignore[attr-defined]
        try:
            await client.tap(184, 1043)
            payload = _peer_bytes(control_peer, 64)
        finally:
            await client.stop()
        # video=false 时 server 无 PositionMapper，直接用设备像素坐标与设备尺寸
        down = encode_touch(0, 184, 1043, 900, 1600, pressure=0xFFFF)
        up = encode_touch(1, 184, 1043, 900, 1600, pressure=0)
        assert payload == down + up

    run(scenario())


def test_control_only_does_not_start_decoder(tmp_path, monkeypatch):
    async def scenario():
        client, _adb, process = await _start_with_fakes(
            tmp_path, monkeypatch, video=False
        )
        try:
            assert client._decoder_process is None
            assert process.poll() is None  # 假 Popen 未被启动
            assert client.latest_frame(max_age_ms=1000) is None
        finally:
            await client.stop()
        assert process.terminated or process.killed or process.poll() is None

    run(scenario())


def test_set_video_enabled_switches_next_start(tmp_path):
    client = make_client(tmp_path, video=False)
    assert client.video is False
    client.set_video_enabled(True)
    assert client.video is True
    assert client.capabilities["decoder"] in {True, False}
    assert client.latest_frame() is None


def test_start_pushes_jar_when_remote_missing_or_size_mismatch(tmp_path, monkeypatch):
    async def scenario():
        adb = FakeAdb(jar_present=False)
        client, adb, _process = await _start_with_fakes(
            tmp_path, monkeypatch, adb=adb
        )
        try:
            assert len(adb.pushed()) == 1  # 远端 jar 缺失时 push 一次
            assert adb.launch_calls == 1
            assert client.started is True
        finally:
            await client.stop()

    run(scenario())


# ----------------------------------------------------------------------
# 真实设备集成测试（默认跳过）
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
PLUGIN_DIR = Path(__file__).resolve().parents[1]

integration = pytest.mark.skipif(not ADB_HOST, reason="需要 PHONE_ADB_HOST 环境变量指向真实设备")


def _real_adb():
    from core.adb import AdbClient

    return AdbClient(
        ADB_PATH,
        ADB_HOST,
        ADB_PORT,
        ADB_HOME,
        connect_timeout=20.0,
        command_timeout=30.0,
    )


@pytest.mark.integration
@integration
def test_real_device_control_scrcpy():
    """真机：启动 scrcpy，验证 touch/back/key 控制注入与截图无关的清理。"""
    from core.actions import DeviceActions
    from core.ui import dump_ui, find_nodes

    async def scenario():
        adb = _real_adb()
        await adb.connect()
        client = ScrcpyClient(
            adb,
            server_jar=PLUGIN_DIR / "bin" / "scrcpy-server-v5.0.1",
            max_size=1024,
            logger=lambda message: None,
            data_dir=Path("/tmp/scrcpy-it-control"),
        )
        actions = DeviceActions(adb)
        try:
            await client.start()
            assert client.started is True
            caps = client.capabilities
            assert caps["control"] is True
            assert client.server_pid is not None
            assert (await client.get_device_size())[0] > 0

            await actions.open_app("com.android.settings")
            await asyncio.sleep(2.5)
            snapshot = await dump_ui(adb, timeout=20.0)
            nodes = find_nodes(snapshot, text="显示")
            assert nodes, "设置页应存在“显示”入口"
            cx, cy = nodes[0].bounds.center

            await client.tap(cx, cy)
            await asyncio.sleep(1.5)
            assert (await actions.foreground())[1].endswith("SubSettings")

            await client.back()
            await asyncio.sleep(1.2)
            assert (await actions.foreground())[1].endswith(".Settings")

            await client.press_key("home")
            await asyncio.sleep(1.2)
            assert (await actions.foreground())[0] == "app.lawnchair"

            await client.swipe_direction("up", duration_ms=300)
        finally:
            await client.stop()
            await adb.close()

    run(scenario())


@pytest.mark.integration
@integration
def test_real_device_video_pipeline_with_fake_ffmpeg(tmp_path):
    """真机：用假 ffmpeg（写固定 JPEG）验证 fd 透传 + shell 管道 + 文件 tail。"""
    import base64

    from core.adb import AdbClient

    jpeg_payload = base64.b64encode(make_test_jpeg(576, 1024)).decode("ascii")
    fake_ffmpeg = tmp_path / "fake-ffmpeg"
    fake_ffmpeg.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, sys, time\n"
        f"DATA = base64.b64decode({jpeg_payload!r})\n"
        "out = sys.argv[-1]\n"
        "for _ in range(200):\n"
        "    with open(out, 'wb') as fh:\n"
        "        fh.write(DATA)\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    fake_ffmpeg.chmod(0o755)

    async def scenario():
        adb = AdbClient(
            ADB_PATH,
            ADB_HOST,
            ADB_PORT,
            ADB_HOME,
            connect_timeout=20.0,
            command_timeout=30.0,
        )
        await adb.connect()
        client = ScrcpyClient(
            adb,
            server_jar=PLUGIN_DIR / "bin" / "scrcpy-server-v5.0.1",
            max_size=1024,
            ffmpeg_path=str(fake_ffmpeg),
            logger=lambda message: None,
            data_dir=tmp_path / "decoder",
        )
        try:
            await client.start()
            deadline = time.monotonic() + 10.0
            frame = None
            while time.monotonic() < deadline:
                frame = client.latest_frame()
                if frame is not None:
                    break
                await asyncio.sleep(0.1)
            assert frame is not None, f"未产生解码帧：{client.last_error}"
            assert frame.data.startswith(b"\xff\xd8")
            assert (frame.width, frame.height) == (576, 1024)
            assert client.capabilities["video"] is True
            assert client.capabilities["decoder"] is True
        finally:
            await client.stop()
            await adb.close()

    run(scenario())


@pytest.mark.integration
@integration
def test_real_device_video_pipeline_with_real_ffmpeg(tmp_path):
    """真机 + 真实 ffmpeg：验证真实 H264 解码出 JPEG 帧（本地无 ffmpeg 时跳过）。"""
    import shutil

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("本机未安装 ffmpeg，真实视频解码由 UAT/prod 容器环境覆盖")

    from core.adb import AdbClient

    async def scenario():
        adb = AdbClient(
            ADB_PATH,
            ADB_HOST,
            ADB_PORT,
            ADB_HOME,
            connect_timeout=20.0,
            command_timeout=30.0,
        )
        await adb.connect()
        client = ScrcpyClient(
            adb,
            server_jar=PLUGIN_DIR / "bin" / "scrcpy-server-v5.0.1",
            max_size=1024,
            ffmpeg_path=ffmpeg,
            logger=lambda message: None,
            data_dir=tmp_path / "decoder-real",
        )
        try:
            await client.start()
            deadline = time.monotonic() + 15.0
            frame = None
            while time.monotonic() < deadline:
                frame = client.latest_frame()
                if frame is not None:
                    break
                await asyncio.sleep(0.1)
            assert frame is not None, f"未产生解码帧：{client.last_error}"
            assert frame.width > 0 and frame.height > 0
        finally:
            await client.stop()
            await adb.close()

    run(scenario())


def test_start_decoder_prefers_pyav_when_available(tmp_path, monkeypatch):
    """PyAV 可用时优先启动 PyAV 实时解码线程。"""
    client = make_client(tmp_path)
    called: list[object] = []
    monkeypatch.setattr(
        ScrcpyClient,
        "_av_decoder_available",
        staticmethod(lambda: True),
        raising=True,
    )
    monkeypatch.setattr(
        ScrcpyClient,
        "_start_av_decoder",
        lambda self, sock: called.append(sock),
        raising=True,
    )

    class FakeSocket:
        pass

    sock = FakeSocket()
    client._start_decoder(sock)
    assert called == [sock]
