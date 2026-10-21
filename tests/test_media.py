"""core.media 单元测试：音频采集协议、WAV/关键帧/转写辅助与配置解析。

全部用例不依赖真实设备/AstrBot：adb 与 socket 都用鸭子类型/桩替换。
"""

from __future__ import annotations

import asyncio
import struct
import wave
from pathlib import Path

import pytest

from core import media
from core.config import PhoneConfig
from core.errors import AdbCommandError, MediaCaptureError


# ----------------------------------------------------------------------
# 桩对象
# ----------------------------------------------------------------------


class FakeAdb:
    """记录 adb 调用并按命令类型返回可配置结果的桩。"""

    def __init__(
        self,
        *,
        jar_size: int = 1024,
        remote_size: int | None = None,
        launch_output: str = "PID=4321",
        log_tail: str = "",
        fail_forward: bool = False,
    ) -> None:
        self.jar_size = jar_size
        self.remote_size = jar_size if remote_size is None else remote_size
        self.launch_output = launch_output
        self.log_tail = log_tail
        self.fail_forward = fail_forward
        self.commands: list[tuple] = []
        self.pushed: list[list[str]] = []
        self.ready = 0

    async def ensure_ready(self) -> None:
        self.ready += 1

    async def shell(self, command: str, timeout: float = 30.0, retry: bool = True) -> str:
        self.commands.append(("shell", command))
        if command.startswith("wc -c"):
            return str(self.remote_size)
        if "app_process /" in command:
            return self.launch_output + "\nGUARD_PID=9999"
        if command.startswith("tail"):
            return self.log_tail
        return ""

    async def run(self, args, timeout: float = 30.0) -> str:
        self.commands.append(("run", list(args)))
        if args and args[0] == "push":
            self.pushed.append(list(args))
        if args and args[0] == "forward" and "--remove" not in args and self.fail_forward:
            raise AdbCommandError("forward 失败")
        return ""

    def launched_commands(self) -> list[str]:
        return [cmd for kind, cmd in self.commands if kind == "shell" and "app_process /" in cmd]

    def remove_forward_calls(self) -> list[list[str]]:
        return [
            args
            for kind, args in self.commands
            if kind == "run" and args and args[0] == "forward" and "--remove" in args
        ]

    def kill_commands(self) -> list[str]:
        return [cmd for kind, cmd in self.commands if kind == "shell" and cmd.startswith("kill ")]


class FakeSocket:
    """按顺序返回 dummy 与 PCM 片段，耗尽后返回 EOF。"""

    def __init__(self, *, dummy: bytes = b"\x00", chunks=()) -> None:
        self.parts = ([dummy] if dummy is not None else []) + list(chunks)
        self.timeout = None
        self.closed = False

    def settimeout(self, value) -> None:
        self.timeout = value

    def recv(self, size: int) -> bytes:
        if not self.parts:
            return b""
        data = self.parts.pop(0)
        if len(data) > size:
            self.parts.insert(0, data[size:])
            return data[:size]
        return data

    def close(self) -> None:
        self.closed = True


def _jar(tmp_path: Path) -> Path:
    path = tmp_path / "scrcpy-server-v5.0.1"
    path.write_bytes(b"x" * 1024)
    return path


def _pcm(seconds: float, value: int = 16) -> bytes:
    frames = int(seconds * media.SAMPLE_RATE)
    return struct.pack("<h", value) * (frames * media.CHANNELS)


def _client(tmp_path: Path, adb: FakeAdb) -> media.MediaAudioCapture:
    return media.MediaAudioCapture(
        adb,
        server_jar=_jar(tmp_path),
        data_dir=tmp_path / "media",
    )


def _patch_socket(monkeypatch, *sockets: FakeSocket) -> None:
    queue = list(sockets)

    def _connect(port: int, timeout: float = media._CONNECT_TIMEOUT_S):
        return queue.pop(0)

    monkeypatch.setattr(media, "_connect_socket", _connect)


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------


def test_clamp_seconds_bounds_and_invalid():
    assert media.clamp_seconds(10) == 10.0
    assert media.clamp_seconds(0) == media.DEFAULT_SECONDS
    assert media.clamp_seconds(-5) == media.DEFAULT_SECONDS
    assert media.clamp_seconds("bad") == media.DEFAULT_SECONDS
    assert media.clamp_seconds(1) == media.MIN_SECONDS
    assert media.clamp_seconds(999, maximum=60) == 60.0


def test_plan_frame_times_spreads_over_duration():
    assert media.plan_frame_times(0) == []
    frames = media.plan_frame_times(20, interval=4, max_frames=4)
    assert frames == [4.0, 8.0, 12.0, 16.0]
    assert all(0 < item < 20 for item in frames)
    assert media.plan_frame_times(10, interval=4, max_frames=4) == [2.5, 5.0, 7.5]
    assert media.plan_frame_times(60, interval=4, max_frames=4) == [12.0, 24.0, 36.0, 48.0]
    assert len(media.plan_frame_times(3, interval=1, max_frames=8)) == 3


def test_write_wav_and_pcm_helpers(tmp_path: Path):
    pcm = _pcm(1.0)
    path = tmp_path / "a.wav"
    seconds = media.write_wav(path, pcm)
    assert seconds == pytest.approx(1.0, abs=0.01)
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == media.CHANNELS
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == media.SAMPLE_RATE
        assert handle.getnframes() == media.SAMPLE_RATE
    assert media.pcm_duration(len(pcm)) == pytest.approx(1.0)
    assert media.pcm_peak(pcm) == 16
    assert media.pcm_peak(b"") == 0
    # 奇数长度写入时按帧对齐截断
    assert media.write_wav(tmp_path / "b.wav", b"\x01\x02\x03") == pytest.approx(0.0)


def test_clean_transcript_and_vision_prompt():
    assert media.clean_transcript("  第一句  \n\n 第二句\t ") == "第一句\n第二句"
    assert media.clean_transcript("x" * 50, limit=10).endswith("…")
    prompt = media.build_vision_prompt(3, question="讲了什么", audio_available=False)
    assert "3 张" in prompt and "讲了什么" in prompt and "没有可用的声音" in prompt
    assert "不要编造" in prompt


def test_parse_server_version_and_socket_name():
    assert media._parse_server_version("scrcpy-server-v5.0.1") == "5.0.1"
    assert media._parse_server_version("scrcpy-server") == media.SERVER_VERSION_FALLBACK
    assert media.MediaAudioCapture._socket_name(0x0ABC1234) == "scrcpy_0abc1234"


# ----------------------------------------------------------------------
# 采集
# ----------------------------------------------------------------------


def test_capture_success_writes_wav_and_cleans_up(tmp_path: Path, monkeypatch):
    adb = FakeAdb(remote_size=1024)
    sock = FakeSocket(chunks=[_pcm(3.0)])
    _patch_socket(monkeypatch, sock)
    client = _client(tmp_path, adb)

    clip = asyncio.run(client.capture(3.0))

    assert clip.seconds == pytest.approx(3.0, abs=0.01)
    assert clip.pcm_bytes == len(_pcm(3.0))
    assert clip.peak == 16 and not clip.silent and not clip.truncated
    assert Path(clip.path).is_file()
    assert sock.closed is True
    assert adb.ready == 1
    # 启动参数：raw_stream 必须在 send_dummy_byte 之前，仅音频通道
    command = adb.launched_commands()[0]
    assert "audio=true" in command and "video=false" in command and "control=false" in command
    assert "audio_codec=raw" in command and "audio_source=output" in command
    assert command.index("raw_stream=true") < command.index("send_dummy_byte=true")
    assert "scid=" in command
    # 只清理本次 PID/forward
    assert adb.kill_commands() == ["kill 4321 2>/dev/null || true"]
    removed = adb.remove_forward_calls()
    assert len(removed) == 1 and removed[0][1] == "--remove"
    # forward 指向本次 scid 的 abstract socket
    forward = [args for kind, args in adb.commands if kind == "run" and args[:1] == ["forward"] and "--remove" not in args]
    assert forward and forward[0][1].startswith("tcp:") and forward[0][2].startswith("localabstract:scrcpy_")


def test_capture_source_mic_is_passed_through(tmp_path: Path, monkeypatch):
    adb = FakeAdb()
    _patch_socket(monkeypatch, FakeSocket(chunks=[_pcm(3.0)]))
    client = _client(tmp_path, adb)

    clip = asyncio.run(client.capture(3.0, source="mic"))

    assert clip.source == "mic"
    assert "audio_source=mic" in adb.launched_commands()[0]


def test_capture_pushes_jar_when_remote_size_differs(tmp_path: Path, monkeypatch):
    adb = FakeAdb(remote_size=-1)
    _patch_socket(monkeypatch, FakeSocket(chunks=[_pcm(3.0)]))
    client = _client(tmp_path, adb)

    asyncio.run(client.capture(3.0))

    assert adb.pushed and adb.pushed[0][0] == "push"
    assert "/data/local/tmp/scrcpy-server-5.0.1.jar" in adb.pushed[0][2]


def test_capture_no_pid_raises_and_removes_forward(tmp_path: Path, monkeypatch):
    adb = FakeAdb(launch_output="no pid here")
    client = _client(tmp_path, adb)

    with pytest.raises(MediaCaptureError):
        asyncio.run(client.capture(3.0))

    assert adb.remove_forward_calls()


def test_capture_short_audio_raises_with_log_tail(tmp_path: Path, monkeypatch):
    adb = FakeAdb(log_tail="[server] ERROR audio capture failed")
    _patch_socket(monkeypatch, FakeSocket(chunks=[b"\x00\x01"]))
    client = _client(tmp_path, adb)

    with pytest.raises(MediaCaptureError) as info:
        asyncio.run(client.capture(3.0))

    assert "audio capture failed" in str(info.value) or "audio capture failed" in info.value.detail


def test_capture_retries_until_server_ready(tmp_path: Path, monkeypatch):
    adb = FakeAdb()
    first = FakeSocket(dummy=b"")
    second = FakeSocket(dummy=b"\x00", chunks=[_pcm(3.0)])
    monkeypatch.setattr(media, "_CONNECT_RETRY_S", 0.001)
    _patch_socket(monkeypatch, first, second)
    client = _client(tmp_path, adb)

    clip = asyncio.run(client.capture(3.0))

    assert clip.seconds == pytest.approx(3.0, abs=0.01)
    assert first.closed is True
    assert second.closed is True


def test_capture_empty_dummy_times_out(tmp_path: Path, monkeypatch):
    adb = FakeAdb()
    monkeypatch.setattr(media, "_CONNECT_TIMEOUT_S", 0.06)
    monkeypatch.setattr(media, "_CONNECT_RETRY_S", 0.01)
    monkeypatch.setattr(media, "_connect_socket", lambda port, timeout=1.0: FakeSocket(dummy=b""))
    client = _client(tmp_path, adb)

    with pytest.raises(MediaCaptureError) as info:
        asyncio.run(client.capture(3.0))

    assert "连接设备音频通道超时" in str(info.value)
    assert adb.kill_commands()


def test_capture_removes_remote_log_after_success(tmp_path: Path, monkeypatch):
    adb = FakeAdb()
    _patch_socket(monkeypatch, FakeSocket(chunks=[_pcm(3.0)]))
    client = _client(tmp_path, adb)

    asyncio.run(client.capture(3.0))

    removed = [
        cmd
        for kind, cmd in adb.commands
        if kind == "shell" and cmd.startswith("rm -f /data/local/tmp/scrcpy-media-")
    ]
    assert removed


def test_pcm_peak_samples_spread_windows():
    quiet_then_loud = _pcm(1.0, value=0) + _pcm(1.0, value=1200)
    assert media.pcm_peak(quiet_then_loud) >= 1200
    assert media.pcm_peak(_pcm(2.0, value=0)) == 0


def test_capture_truncated_marks_warning(tmp_path: Path, monkeypatch):
    adb = FakeAdb()
    _patch_socket(monkeypatch, FakeSocket(chunks=[_pcm(1.0)]))
    client = _client(tmp_path, adb)

    clip = asyncio.run(client.capture(5.0))

    assert clip.truncated is True
    assert "提前结束" in clip.warning


def test_capture_silent_marks_warning(tmp_path: Path, monkeypatch):
    adb = FakeAdb()
    _patch_socket(monkeypatch, FakeSocket(chunks=[_pcm(3.0, value=0)]))
    client = _client(tmp_path, adb)

    clip = asyncio.run(client.capture(3.0))

    assert clip.silent is True
    assert "静音" in clip.warning


def test_capture_connect_timeout_cleans_up(tmp_path: Path, monkeypatch):
    adb = FakeAdb()
    client = _client(tmp_path, adb)
    monkeypatch.setattr(media, "_CONNECT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(media, "_CONNECT_RETRY_S", 0.01)

    def _fail(port: int, timeout: float = 1.0):
        raise OSError("connection refused")

    monkeypatch.setattr(media, "_connect_socket", _fail)

    with pytest.raises(MediaCaptureError) as info:
        asyncio.run(client.capture(3.0))

    assert "连接设备音频通道超时" in str(info.value)
    assert adb.kill_commands() and adb.remove_forward_calls()


def test_capture_forward_failure_raises(tmp_path: Path):
    adb = FakeAdb(fail_forward=True)
    client = _client(tmp_path, adb)

    with pytest.raises(MediaCaptureError) as info:
        asyncio.run(client.capture(3.0))

    assert "建立音频端口转发失败" in str(info.value)


def test_capture_serializes_concurrent_calls(tmp_path: Path, monkeypatch):
    adb = FakeAdb()
    first = FakeSocket(chunks=[_pcm(3.0)])
    second = FakeSocket(chunks=[_pcm(3.0)])
    _patch_socket(monkeypatch, first, second)
    client = _client(tmp_path, adb)

    async def _run() -> tuple:
        return await asyncio.gather(client.capture(3.0), client.capture(3.0))

    clips = asyncio.run(_run())

    assert len(clips) == 2
    assert first.closed and second.closed
    # 两个 server 使用不同 scid/端口，互不覆盖
    commands = adb.launched_commands()
    assert len(commands) == 2
    assert commands[0] != commands[1]


def test_invalid_source_falls_back_to_output(tmp_path: Path, monkeypatch):
    adb = FakeAdb()
    _patch_socket(monkeypatch, FakeSocket(chunks=[_pcm(3.0)]))
    client = _client(tmp_path, adb)

    clip = asyncio.run(client.capture(3.0, source="unknown"))

    assert clip.source == "output"
    assert "audio_source=output" in adb.launched_commands()[0]


def test_missing_jar_raises(tmp_path: Path):
    adb = FakeAdb()
    client = media.MediaAudioCapture(
        adb,
        server_jar=tmp_path / "missing-server",
        data_dir=tmp_path / "media",
    )

    with pytest.raises(MediaCaptureError):
        asyncio.run(client.capture(3.0))


# ----------------------------------------------------------------------
# 配置解析
# ----------------------------------------------------------------------


def test_media_config_defaults():
    config = PhoneConfig.from_mapping(None)
    assert config.media.enabled is True
    assert config.media.audio_enabled is True
    assert config.media.audio_source == "output"
    assert config.media.seconds == 20
    assert config.media.max_seconds == 60
    assert config.media.frame_interval_seconds == 4.0
    assert config.media.max_frames == 4
    assert config.media.keep_audio is False


def test_media_config_clamps_and_fallbacks():
    config = PhoneConfig.from_mapping(
        {
            "media": {
                "enabled": "false",
                "audio_enabled": 0,
                "audio_source": "bad",
                "seconds": 999,
                "max_seconds": 60,
                "frame_interval_seconds": 999,
                "max_frames": 99,
                "asr_timeout_seconds": 1,
                "vision_timeout_seconds": 9999,
                "keep_audio": "true",
            }
        }
    )
    assert config.media.enabled is False
    assert config.media.audio_enabled is False
    assert config.media.audio_source == "output"
    assert config.media.seconds == 60
    assert config.media.frame_interval_seconds == 60.0
    assert config.media.max_frames == 8
    assert config.media.asr_timeout_seconds == 5
    assert config.media.vision_timeout_seconds == 300
    assert config.media.keep_audio is True
