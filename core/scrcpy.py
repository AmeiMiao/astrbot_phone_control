"""scrcpy v5.0.1 控制 + 低延迟视频客户端（纯标准库实现）。

只实现插件需要的协议子集：

* 启动插件内置的 ``scrcpy-server-v5.0.1``（``app_process`` 启动，``raw_stream=true``
  ``tunnel_forward=true``），通过 ``adb forward`` 暴露本地 TCP 端口；
* ``video=True``（默认）：先连接 **video** socket、再连接 **control** socket，
  raw_stream 模式下 video 直接是 annex-b H.264 字节流，交给解码器输出 JPEG 帧；
* ``video=False``：server 以 ``video=false`` 启动，只连接 **control** socket，
  不启动解码器、不产生 H264 编解码开销（``capabilities.video/decoder=False``、
  ``latest_frame()`` 恒为 None），用于纯控制场景；
* 控制通道按 scrcpy v5.0.1 协议发送 key/text/touch/back 等大端二进制消息；
* 每次启动使用随机 ``scid`` 生成独立 abstract socket 名，避免与残留 server
  或同机其他实例抢 ``scrcpy`` socket；
* 视频流断开会自动清理并重连一次；``stop()`` 只 kill 本次启动记录的 server pid
  并移除 forward，远端 jar/log 保留供下次启动复用（避免重复 ``adb push``）。

坐标约定：控制 API 对外使用**设备真实像素**（``wm size``）。``video=True`` 时若
``max_size`` 使 server 的视频尺寸缩小（例如 900x1600 + max_size=1024 ->
576x1024），内部会把设备坐标换算到视频坐标，并按协议把 ``screen_size`` 填成
视频尺寸（server 的 PositionMapper 要求两者严格一致，否则事件会被忽略）。
``video=False`` 时 server 没有 PositionMapper，触摸事件直接使用设备像素坐标，
``screen_size`` 填设备尺寸。

模块不导入 astrbot；``adb`` 参数需要是 ``core.adb.AdbClient`` 或提供
``shell()`` / ``_run_adb()`` / ``serial`` / ``_adb_path`` 的鸭子类型。
"""

from __future__ import annotations

from .identity import PLUGIN_NAME

import asyncio
from contextlib import suppress
import os
import random
import re
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .process_lease import DeviceLease, guarded_command, INTERVAL

from .adb import parse_wm_size

LogFn = Callable[[str], None]

# scrcpy server 相关常量
DEFAULT_SCRCPY_VERSION = "5.0.1"
_REMOTE_DIR = "/data/local/tmp"
_REMOTE_JAR_TEMPLATE = _REMOTE_DIR + "/scrcpy-server-{version}.jar"
_REMOTE_LOG = _REMOTE_DIR + "/scrcpy.log"
_SOCKET_NAME_PREFIX = "scrcpy"
_SERVER_CLASS = "com.genymobile.scrcpy.Server"
_SERVER_PROCESS_NEEDLE = "app_process / " + _SERVER_CLASS
_DEFAULT_BUFFER_FRAMES = 3
_START_TIMEOUT_S = 10.0
_CONNECT_TIMEOUT_S = 2.0
_CONNECT_RETRY_INTERVAL_S = 0.02
_DUMMY_READ_TIMEOUT_S = 2.0
_DECODER_FILE_NAME = "scrcpy_live.jpg"
_DECODER_LOG_NAME = "scrcpy_decoder.log"
_FRAME_POLL_INTERVAL_S = 0.05

# 控制协议消息类型（scrcpy v5.0.1 ControlMessage.TYPE_*）
TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TEXT = 1
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_INJECT_SCROLL_EVENT = 3
TYPE_BACK_OR_SCREEN_ON = 4
TYPE_GET_CLIPBOARD = 8
TYPE_ROTATE_DEVICE = 11

# Android 事件动作
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

# 触摸压力（u16 定点：0 ~ 65535）
PRESSURE_MAX = 0xFFFF
PRESSURE_NONE = 0

# 文本注入最大字节数（scrcpy SC_CONTROL_MSG_INJECT_TEXT_MAX_LENGTH）
TEXT_MAX_BYTES = 300

# Android keycode（与 core.actions._KEY_CODES 保持一致，另接受整数 keycode）
KEYCODES: dict[str, int] = {
    "back": 4,
    "home": 3,
    "enter": 66,
    "return": 66,
    "recents": 187,
    "app_switch": 187,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "tab": 61,
    "delete": 67,
    "backspace": 67,
    "escape": 111,
    "menu": 82,
    "search": 84,
    "space": 62,
    "camera": 27,
    "wakeup": 224,
    "sleep": 223,
}

# JPEG SOF 标记（含尺寸信息的帧头）
_JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}

# adb forward --list 行形如：serial tcp:1234 localabstract:scrcpy
_FORWARD_LINE_RE = re.compile(r"^(\S+)\s+(tcp:\d+)\s+(\S+)\s*$")


class ScrcpyError(RuntimeError):
    """scrcpy 不可用、协议错误或启动/连接失败。"""


@dataclass(frozen=True)
class ScrcpyFrame:
    """一帧解码后的视频画面（JPEG）。"""

    data: bytes
    ts: float
    width: int
    height: int
    mime: str = "image/jpeg"


# ----------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------
async def _await_if_needed(value: Any) -> Any:
    """如果 value 是 awaitable 则 await，否则原样返回。"""
    if hasattr(value, "__await__"):
        return await value
    return value


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", "replace").strip()


def parse_forward_list(text: str) -> list[tuple[str, str, str]]:
    """解析 ``adb forward --list`` 输出为 ``[(serial, tcp:port, remote), ...]``。"""
    forwards: list[tuple[str, str, str]] = []
    for line in (text or "").splitlines():
        match = _FORWARD_LINE_RE.match(line.strip())
        if match:
            forwards.append((match.group(1), match.group(2), match.group(3)))
    return forwards


def free_local_port() -> int:
    """向系统申请一个空闲 TCP 端口（关闭探测 socket 后返回端口号）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def compute_video_size(device_width: int, device_height: int, max_size: int) -> tuple[int, int]:
    """按 scrcpy server 的尺寸约束计算视频尺寸（保持宽高比，默认 1 像素对齐）。

    与 server ``Size.constrain(preserveAspectRatio=true)`` 一致：
    长边超过 ``max_size`` 时缩放到长边等于 ``max_size``，短边按比例取整。
    """
    width = max(1, int(device_width))
    height = max(1, int(device_height))
    limit = int(max_size or 0)
    if limit <= 0 or (width <= limit and height <= limit):
        return width, height
    if width > height:
        return limit, max(1, height * limit // width)
    return max(1, width * limit // height), limit


def is_annex_b_stream(data: bytes) -> bool:
    """判断字节流是否以 annex-b 起始码（00 00 00 01 / 00 00 01）开头。"""
    return data.startswith(b"\x00\x00\x00\x01") or data.startswith(b"\x00\x00\x01")


def count_annex_b_nals(data: bytes, limit: int = 0) -> int:
    """统计 annex-b 起始码数量（用于日志/证据，不解析 NAL 内容）。"""
    count = 0
    position = 0
    while True:
        index = data.find(b"\x00\x00\x00\x01", position)
        if index < 0:
            break
        count += 1
        position = index + 4
        if limit and count >= limit:
            break
    return count


def parse_jpeg_size(data: bytes) -> tuple[int, int] | None:
    """从 JPEG 字节中解析 ``(width, height)``，失败返回 None。"""
    if not data or not data.startswith(b"\xff\xd8"):
        return None
    position = 2
    length = len(data)
    while position + 4 <= length:
        if data[position] != 0xFF:
            position += 1
            continue
        marker = data[position + 1]
        # 填充字节 0xFF 直接跳过
        if marker == 0xFF:
            position += 1
            continue
        # 无长度的独立标记
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            position += 2
            continue
        segment_length = struct.unpack_from(">H", data, position + 2)[0]
        if segment_length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if position + 9 > length:
                return None
            height, width = struct.unpack_from(">HH", data, position + 5)
            if width > 0 and height > 0:
                return int(width), int(height)
            return None
        position += 2 + segment_length
    return None


class MjpegFrameParser:
    """增量解析 MJPEG 字节流，按 JPEG SOI/EOI 提取完整帧。"""

    def __init__(self, *, max_buffer: int = 16 * 1024 * 1024) -> None:
        self._buffer = bytearray()
        self._max_buffer = max(1024, int(max_buffer))

    def feed(self, chunk: bytes) -> list[bytes]:
        """喂入新字节，返回本次新解析出的完整 JPEG 帧列表。"""
        if chunk:
            self._buffer.extend(chunk)
        frames: list[bytes] = []
        while True:
            start = self._buffer.find(b"\xff\xd8")
            if start < 0:
                # 保留最后 1 字节，避免 SOI 跨块被丢弃
                if len(self._buffer) > 1:
                    del self._buffer[:-1]
                break
            if start > 0:
                del self._buffer[:start]
            end = self._buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(self._buffer) > self._max_buffer:
                    raise ScrcpyError("MJPEG 帧缓冲超过上限，解码流异常")
                break
            frames.append(bytes(self._buffer[: end + 2]))
            del self._buffer[: end + 2]
        if len(self._buffer) > self._max_buffer:
            raise ScrcpyError("MJPEG 帧缓冲超过上限，解码流异常")
        return frames


# ----------------------------------------------------------------------
# 控制消息编码（对照 scrcpy app/tests/test_control_msg_serialize.c）
# ----------------------------------------------------------------------
def encode_key(action: int, keycode: int, repeat: int = 0, metastate: int = 0) -> bytes:
    """``type=0`` key 消息：type(1) action(1) keycode(4) repeat(4) metastate(4)。"""
    return struct.pack(
        ">BBIII",
        TYPE_INJECT_KEYCODE,
        int(action) & 0xFF,
        int(keycode) & 0xFFFFFFFF,
        int(repeat) & 0xFFFFFFFF,
        int(metastate) & 0xFFFFFFFF,
    )


def encode_text(text: str) -> bytes:
    """``type=1`` text 消息：type(1) length(4) utf8 bytes（最多 300 字节）。"""
    raw = str(text).encode("utf-8")
    if len(raw) > TEXT_MAX_BYTES:
        raw = _truncate_utf8(raw, TEXT_MAX_BYTES)
    return struct.pack(">BI", TYPE_INJECT_TEXT, len(raw)) + raw


def _truncate_utf8(raw: bytes, max_bytes: int) -> bytes:
    """按 UTF-8 字符边界截断字节序列。"""
    if len(raw) <= max_bytes:
        return raw
    cut = raw[:max_bytes]
    try:
        cut.decode("utf-8")
        return cut
    except UnicodeDecodeError as exc:
        # 截断点落在多字节字符中间：退到该字符起点
        return cut[: exc.start]


def encode_touch(
    action: int,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    pressure: int = PRESSURE_MAX,
    pointer_id: int = 0,
    action_button: int = 0,
    buttons: int = 0,
) -> bytes:
    """``type=2`` touch 消息（32 字节，大端）。

    字段顺序：type, action, pointer_id(u64), x(i32), y(i32), width(u16),
    height(u16), pressure(u16), action_button(u32), buttons(u32)。
    """
    if not (0 <= int(width) <= 0xFFFF and 0 <= int(height) <= 0xFFFF):
        raise ScrcpyError(f"scrcpy 视频尺寸超出 u16 范围：{width}x{height}")
    return struct.pack(
        ">BBQiiHHHII",
        TYPE_INJECT_TOUCH_EVENT,
        int(action) & 0xFF,
        int(pointer_id) & 0xFFFFFFFFFFFFFFFF,
        int(x),
        int(y),
        int(width),
        int(height),
        max(0, min(PRESSURE_MAX, int(pressure))),
        int(action_button) & 0xFFFFFFFF,
        int(buttons) & 0xFFFFFFFF,
    )


def encode_scroll(
    x: int,
    y: int,
    width: int,
    height: int,
    hscroll: int = 0,
    vscroll: int = 0,
    *,
    buttons: int = 0,
) -> bytes:
    """``type=3`` scroll 消息（21 字节）。

    hscroll/vscroll 参数接受 scrcpy 客户端的 [-16, 16] 整数刻度，内部归一化
    为 [-1, 1] 后按 16 位定点编码。
    """
    if not (0 <= int(width) <= 0xFFFF and 0 <= int(height) <= 0xFFFF):
        raise ScrcpyError(f"scrcpy 视频尺寸超出 u16 范围：{width}x{height}")
    return struct.pack(
        ">BiiHHhhI",
        TYPE_INJECT_SCROLL_EVENT,
        int(x),
        int(y),
        int(width),
        int(height),
        _scroll_fixed_point(hscroll),
        _scroll_fixed_point(vscroll),
        int(buttons) & 0xFFFFFFFF,
    )


def _scroll_fixed_point(value: int) -> int:
    """把 [-16, 16] 的滚动刻度归一化为 16 位定点（+16->32767，-16->-32768）。"""
    clamped = max(-16, min(16, int(value)))
    if clamped >= 0:
        return int(round(clamped / 16 * 0x7FFF))
    return int(round(clamped / 16 * 0x8000))


def encode_back(action: int) -> bytes:
    """``type=4`` back_or_screen_on 消息：type(1) action(1)。"""
    return struct.pack(">BB", TYPE_BACK_OR_SCREEN_ON, int(action) & 0xFF)


def encode_rotate() -> bytes:
    """``type=11`` 旋转设备消息（单字节）。"""
    return bytes([TYPE_ROTATE_DEVICE])


def resolve_keycode(key: int | str) -> int:
    """把按键名（``"enter"``）或 keycode 数字统一成整数 keycode。"""
    if isinstance(key, bool):
        raise ScrcpyError("按键不能是布尔值")
    if isinstance(key, int):
        if not 0 <= key <= 0x7FFFFFFF:
            raise ScrcpyError(f"keycode 超出范围：{key}")
        return key
    name = str(key or "").strip().lower()
    if not name:
        raise ScrcpyError("按键不能为空")
    if name.isdigit():
        return resolve_keycode(int(name))
    keycode = KEYCODES.get(name)
    if keycode is None:
        raise ScrcpyError(
            f"不支持的按键：{key}（可选：{'/'.join(sorted(KEYCODES))}）"
        )
    return keycode


# ----------------------------------------------------------------------
# 客户端
# ----------------------------------------------------------------------
class ScrcpyClient:
    """scrcpy v5.0.1 控制与视频客户端。"""

    def __init__(
        self,
        adb: Any,
        *,
        server_jar: str | Path,
        max_size: int = 1024,
        video: bool = True,
        ffmpeg_path: str | None = None,
        logger: LogFn | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        self._adb = adb
        self.server_jar = Path(server_jar).expanduser()
        self.max_size = max(0, int(max_size))
        # 视频通道开关：False 时 server 以 video=false 启动、只连 control socket。
        # 在 start() 前可改（见 set_video_enabled）；运行中修改需停止后重启才生效。
        self.video = bool(video)
        self.ffmpeg_path = str(ffmpeg_path).strip() if ffmpeg_path else ""
        self._logger = logger
        self.data_dir = Path(data_dir).expanduser() if data_dir else None
        self._owns_data_dir = False

        self._started = False
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._state_lock = threading.RLock()
        self._control_send_lock = threading.Lock()
        self._control_receive_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._frames: deque[ScrcpyFrame] = deque(maxlen=_DEFAULT_BUFFER_FRAMES)

        self._asyncio_lock_obj: asyncio.Lock | None = None
        self._asyncio_lock_loop: asyncio.AbstractEventLoop | None = None

        self._video_socket: socket.socket | None = None
        self._control_socket: socket.socket | None = None
        self._drain_thread: threading.Thread | None = None
        self._decoder_process: subprocess.Popen | None = None
        self._decoder_thread: threading.Thread | None = None
        self._av_decoder = False
        self._decoder_file: Path | None = None
        self._decoder_log: Path | None = None
        self._device_lease = None
        self._decoder_heartbeat = None
        self._server_pid: int | None = None
        self._forward_port: int | None = None
        self._remote_jar = ""
        self._remote_log = _REMOTE_LOG
        self._scid = 0
        self._socket_name = _SOCKET_NAME_PREFIX
        self._device_size: tuple[int, int] | None = None
        self._video_size: tuple[int, int] | None = None
        self._decoded_size: tuple[int, int] | None = None
        self._video_stream_head_bytes = b""
        self._stream_failed = False
        self._decoder_failed = False
        self._frame_count = 0
        self._reconnect_available = True
        self._stop_requested = False
        self._reconnect_task = None
        self._ffmpeg_checked = False
        self._ffmpeg_resolved: str | None = None
        # 供 status/工具层展示的可读错误（解码失败等，不吞异常）
        self._last_error = ""

    # ------------------------------------------------------------------
    # 基础属性/日志
    # ------------------------------------------------------------------
    def set_video_enabled(self, enabled: bool) -> None:
        """设置视频通道开关（仅对下一次 ``start()`` 生效）。"""
        self.video = bool(enabled)

    def _log(self, message: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger(message)
        except Exception:  # noqa: BLE001 - 日志回调不允许打断协议流程
            pass

    @property
    def started(self) -> bool:
        return self._started

    @property
    def capabilities(self) -> dict[str, Any]:
        """能力快照：video/control/decoder/server_jar。

        仅控制通道（``video=False``）时 video/decoder 恒为 False。
        """
        video_enabled = bool(self.video)
        decoder = video_enabled and (
            self._resolve_ffmpeg() is not None or self._av_decoder_available()
        )
        with self._state_lock:
            control = bool(
                self._started and self._control_socket is not None and not self._stream_failed
            )
            decoder_alive = (
                self._av_decoder
                if self._av_decoder
                else (
                    self._decoder_process is not None
                    and self._decoder_process.poll() is None
                )
            )
            video = bool(
                video_enabled
                and self._started
                and not self._stream_failed
                and not self._decoder_failed
                and decoder
                and decoder_alive
                and self._frame_count > 0
            )
        return {
            "video": video,
            "control": control,
            "decoder": decoder,
            "server_jar": str(self.server_jar),
        }

    @property
    def video_size(self) -> tuple[int, int] | None:
        """当前视频（编码/解码）尺寸；未知时返回 None。"""
        with self._state_lock:
            return self._decoded_size or self._video_size

    @property
    def device_size(self) -> tuple[int, int] | None:
        """设备真实像素尺寸（``wm size`` 缓存）。"""
        with self._state_lock:
            return self._device_size

    @property
    def server_pid(self) -> int | None:
        """本次启动的 scrcpy server 进程 pid（用于诊断）。"""
        return self._server_pid

    @property
    def video_stream_head(self) -> bytes:
        """视频流最前面的若干字节（诊断用，通常以 annex-b 起始码开头）。"""
        with self._state_lock:
            return self._video_stream_head_bytes

    @property
    def frame_count(self) -> int:
        """已成功解析的解码帧数量。"""
        with self._state_lock:
            return self._frame_count

    @property
    def last_error(self) -> str:
        """最近一次可读错误（启动/解码/断流重连失败），供 status 展示（只读）。"""
        with self._state_lock:
            return self._last_error

    # ------------------------------------------------------------------
    # ADB 适配（AdbClient 或测试鸭子类型）
    # ------------------------------------------------------------------
    async def _adb_ensure_ready(self) -> None:
        ensure = getattr(self._adb, "ensure_ready", None)
        if callable(ensure):
            await _await_if_needed(ensure())

    async def _adb_shell(self, command: str, *, timeout: float | None = None) -> str:
        shell = getattr(self._adb, "shell", None)
        if not callable(shell):
            raise ScrcpyError("ADB 客户端不支持 shell 命令")
        try:
            return await _await_if_needed(shell(command, timeout=timeout))
        except TypeError:
            return await _await_if_needed(shell(command))

    async def _adb_host(self, *args: str, timeout: float | None = None) -> tuple[int, bytes, bytes]:
        """执行 adb 主机侧命令（push/forward 等），返回 (rc, stdout, stderr)。"""
        run_host = getattr(self._adb, "run_host", None)
        if callable(run_host):
            result = await _await_if_needed(run_host(*args, timeout=timeout))
            if isinstance(result, tuple) and len(result) == 3:
                return result
            raise ScrcpyError("ADB run_host 返回值格式不正确")
        runner = getattr(self._adb, "_run_adb", None)
        adb_path = getattr(self._adb, "_adb_path", None)
        serial = getattr(self._adb, "serial", "")
        if callable(runner) and adb_path:
            argv = [str(adb_path), "-s", str(serial), *[str(arg) for arg in args]]
            try:
                result = await _await_if_needed(runner(argv, timeout=timeout))
            except Exception as exc:  # noqa: BLE001 - 统一转 ScrcpyError
                raise ScrcpyError(f"adb 主机命令失败：{exc}") from exc
            if isinstance(result, tuple) and len(result) == 3:
                return result
        raise ScrcpyError("ADB 客户端不支持主机侧命令（push/forward），无法使用 scrcpy")

    async def _adb_host_ok(self, *args: str, timeout: float | None = None) -> str:
        rc, stdout, stderr = await self._adb_host(*args, timeout=timeout)
        if rc != 0:
            raise ScrcpyError(
                f"adb {' '.join(args)} 失败（退出码 {rc}）：{_decode(stderr) or _decode(stdout)}"
            )
        return _decode(stdout)

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def _asyncio_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._asyncio_lock_obj is None or self._asyncio_lock_loop is not loop:
            self._asyncio_lock_obj = asyncio.Lock()
            self._asyncio_lock_loop = loop
        return self._asyncio_lock_obj

    def _server_version(self) -> str:
        match = re.search(r"(\d+\.\d+(?:\.\d+)?)", self.server_jar.name)
        if not match:
            raise ScrcpyError(
                f"无法从 server jar 文件名解析 scrcpy 版本：{self.server_jar.name}"
            )
        return match.group(1)

    async def start(self) -> None:
        """启动 server、建立 video/control 连接并开始解码；失败抛 ``ScrcpyError``。"""
        async with self._asyncio_lock():
            with self._state_lock:
                if self._started:
                    return
                self._stopping = False
                self._stop_requested = False
                self._reconnect_available = True
                self._stream_failed = False
                self._decoder_failed = False
                self._frame_count = 0
                self._last_error = ""
                self._loop = asyncio.get_running_loop()
            try:
                await self._start_internal()
            except BaseException as exc:  # 取消启动时同样回收设备资源
                await self._cleanup_resources()
                if isinstance(exc, (ScrcpyError, asyncio.CancelledError)):
                    raise
                raise ScrcpyError(f"scrcpy 启动失败：{exc}") from exc

    async def _start_internal(self) -> None:
        jar = self.server_jar
        if not jar.is_file():
            raise ScrcpyError(f"scrcpy server jar 不存在：{jar}")
        jar_size = jar.stat().st_size
        if jar_size <= 0:
            raise ScrcpyError(f"scrcpy server jar 为空文件：{jar}")
        version = self._server_version()
        self._remote_jar = _REMOTE_JAR_TEMPLATE.format(version=version)
        self._remote_log = _REMOTE_LOG
        self._scid = random.getrandbits(31)
        self._socket_name = f"{_SOCKET_NAME_PREFIX}_{self._scid:08x}"
        self._log(
            f"启动 scrcpy {version}（server={jar.name}，"
            f"mode={'video+control' if self.video else 'control-only'}，"
            f"max_size={self.max_size if self.video else 0}）"
        )

        await self._adb_ensure_ready()
        self._device_lease = DeviceLease(self._adb, "scrcpy")
        await self._device_lease.prepare()
        await self._remove_stale_forwards()

        port = free_local_port()
        await self._adb_host_ok(
            "forward", f"tcp:{port}", f"localabstract:{self._socket_name}"
        )
        self._forward_port = port

        # 单次 adb shell 完成「清理残留 server + 校验远端 jar + 启动 server」。
        # jar 大小一致时跳过重复 adb push（实测 stat/wc 检查已包含在同一次 shell
        # 往返内，比单独 push 或单独探测都便宜）。
        output = await self._adb_shell(
            self._start_script(version=version, jar_size=jar_size), timeout=30.0
        )
        if "__JAR_MISSING__" in output:
            await self._adb_host_ok(
                "push", str(jar), self._remote_jar, timeout=120.0
            )
            output = await self._adb_shell(self._launch_command(version), timeout=30.0)
        pid_match = re.search(r"PID=(\d+)", output)
        if pid_match:
            self._server_pid = int(pid_match.group(1))
            self._log(
                f"scrcpy server 已启动（pid={self._server_pid}，"
                f"scid={self._scid:x}）"
            )
        else:
            self._log(f"未能从启动输出解析 server pid：{output!r}")

        if self._server_pid is None:
            raise ScrcpyError("scrcpy server 未返回 PID")
        self._device_lease.observe(output)
        video_socket, control_socket = await self._connect_sockets(port)
        self._video_socket = video_socket
        self._control_socket = control_socket
        with self._state_lock:
            self._started = True
            self._stream_failed = False

        if self.video and video_socket is not None:
            # 视频模式：启动解码（不阻塞控制能力），再查询设备分辨率供坐标换算。
            try:
                self._prepare_data_dir()
                self._start_decoder(video_socket)
            except Exception as exc:  # noqa: BLE001 - 解码不可用不影响控制能力
                self._mark_decoder_unavailable(f"启动视频解码失败：{exc}")
            await self._query_device_size()

        self._log(
            "scrcpy 已启动："
            f"video={'on' if self.video else 'off'}，control=on，"
            f"视频尺寸={self.video_size}"
        )

    def _launch_command(self, version: str) -> str:
        """scrcpy server 启动命令（后台运行并回显 PID）。

        ``raw_stream=true`` 会同时关闭 ``send_dummy_byte``（scrcpy v5.0.1
        Options 解析语义），仅控制通道时没有视频首字节可用于就绪判定，因此在其
        之后显式追加 ``send_dummy_byte=true``，让 server 在首个 accept 的 control
        socket 上写 1 字节 dummy 供连接就绪检测。视频模式保持关闭，改用
        MSG_PEEK 等待视频首字节（不消费 H264 流）。
        """
        max_size_arg = (
            f" max_size={self.max_size}" if (self.video and self.max_size > 0) else ""
        )
        video_arg = "" if self.video else "video=false "
        dummy_arg = "" if self.video else " send_dummy_byte=true"
        command = (
            f"CLASSPATH={self._remote_jar} app_process / {_SERVER_CLASS} {version} "
            "log_level=info "
            f"{video_arg}audio=false control=true clipboard_autosync=false raw_stream=true{dummy_arg} "
            f"tunnel_forward=true cleanup=false scid={self._scid:x}{max_size_arg} "
            f"< /dev/null > {self._remote_log} 2>&1 & echo PID=$!"
        )

        return self._device_lease.wrap_launch(command) if self._device_lease else command

    @staticmethod
    def _self_safe_grep(needle: str) -> str:
        """构造不匹配自身命令行的 grep 模式（末字符用 ``[]`` 包裹）。"""
        return needle[:-1] + "[" + needle[-1] + "]"

    def _start_script(self, *, version: str, jar_size: int) -> str:
        """单次 adb shell：清理残留 server + 校验远端 jar + 启动 server。"""
        grep = self._self_safe_grep(_SERVER_PROCESS_NEEDLE)
        launch = self._launch_command(version)
        # 注意：本脚本自身的命令行里含有 launch 中的字面量 needle，
        # ``ps`` 会把它当成 scrcpy server 进程匹配到；必须用 ``$$`` 过滤掉自身，
        # 否则 ``kill $PIDS`` 会把当前 adb shell 自己杀掉（退出码 143）。
        return (
            "PIDS=$(ps -A -o PID,ARGS 2>/dev/null | grep '"
            + grep
            + "' | grep 'control=true' | awk -v self=$$ '$1 != self {print $1}'); "
            + "if [ -n \"$PIDS\" ]; then kill $PIDS 2>/dev/null; sleep 0.3; fi; "
            + f"SIZE=$(wc -c < {self._remote_jar} 2>/dev/null || echo -1); "
            + "echo __JAR_SIZE__=$SIZE; "
            + f"if [ \"$SIZE\" = \"{int(jar_size)}\" ]; then "
            + launch
            + "; else echo __JAR_MISSING__; fi"
        )

    async def _query_device_size(self) -> None:
        """查询并缓存设备真实分辨率（wm size）。"""
        try:
            output = await self._adb_shell("wm size", timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - 取不到时留空，后续按视频帧兜底
            self._log(f"查询设备分辨率失败：{exc}")
            return
        width, height = parse_wm_size(output)
        if width > 0 and height > 0:
            with self._state_lock:
                self._device_size = (width, height)
                if self.video:
                    self._video_size = compute_video_size(width, height, self.max_size)
                else:
                    # 仅控制通道：server 无 PositionMapper，触摸直接使用设备像素坐标
                    self._video_size = (width, height)
            self._log(
                f"设备分辨率 {width}x{height}，"
                f"{'视频尺寸' if self.video else '控制坐标'} {self._video_size}"
            )

    async def _remove_stale_forwards(self) -> None:
        """清理指向 scrcpy abstract socket 的残留 forward。"""
        try:
            output = await self._adb_host_ok("forward", "--list", timeout=10.0)
        except ScrcpyError as exc:
            self._log(f"查询 adb forward 失败：{exc}")
            return
        active_sockets = await self._adb_shell("cat /proc/net/unix", timeout=10.0)
        prefix = f"localabstract:{_SOCKET_NAME_PREFIX}"
        for _serial, local, remote in parse_forward_list(output):
            if _serial != str(getattr(self._adb, "serial", "")) or not remote.startswith(prefix):
                continue
            if "@" + remote.split(":", 1)[1] in str(active_sockets):
                continue
            try:
                await self._adb_host_ok("forward", "--remove", local, timeout=10.0)
                self._log(f"已移除残留 forward：{local} -> {remote}")
            except ScrcpyError as exc:
                self._log(f"移除残留 forward 失败：{exc}")

    async def _connect_sockets(
        self, port: int
    ) -> tuple[socket.socket | None, socket.socket]:
        """连接 server：视频模式先连 video、再连 control；仅控制模式只连 control。

        tunnel forward 下本地 TCP connect 即使 server 还没就绪也会成功，因此需要
        应用层就绪判定：

        * 仅控制通道：启动参数显式 ``send_dummy_byte=true``，server accept 首个
          control socket 后写 1 字节 dummy，读到该字节即视为就绪；
        * 视频模式：server 先 accept video 再 accept control（都 accept 完才开始
          编码），因此必须先连两个 socket，再用 ``MSG_PEEK`` 等待 video socket
          出现视频首字节（不消费 H264 流）；读不到则关闭重试。
        """
        deadline = time.monotonic() + _START_TIMEOUT_S
        last_error = ""
        while time.monotonic() < deadline:
            if not self.video:
                try:
                    control_socket = await asyncio.to_thread(
                        self._connect_ready_control, port
                    )
                except OSError as exc:
                    last_error = str(exc)
                    await asyncio.sleep(_CONNECT_RETRY_INTERVAL_S)
                    continue
                self._log(
                    f"scrcpy control socket 已连接（port={port}，control-only）"
                )
                return None, control_socket
            video_socket: socket.socket | None = None
            try:
                video_socket = await asyncio.to_thread(
                    self._connect_tcp, port, _CONNECT_TIMEOUT_S
                )
                control_socket = await asyncio.to_thread(
                    self._connect_tcp, port, _CONNECT_TIMEOUT_S
                )
            except OSError as exc:
                last_error = str(exc)
                if video_socket is not None:
                    video_socket.close()
                await asyncio.sleep(_CONNECT_RETRY_INTERVAL_S)
                continue
            remaining = max(0.2, min(2.0, deadline - time.monotonic()))
            head = await asyncio.to_thread(
                self._wait_video_readable, video_socket, remaining
            )
            if not head:
                last_error = "socket 已连接但未收到视频数据"
                for sock in (video_socket, control_socket):
                    sock.close()
                await asyncio.sleep(_CONNECT_RETRY_INTERVAL_S)
                continue
            video_socket.settimeout(None)
            control_socket.settimeout(None)
            with self._state_lock:
                # MSG_PEEK 未消费字节：记录流头仅用于诊断
                self._video_stream_head_bytes = head
            self._log(
                f"scrcpy video/control socket 已连接（port={port}，"
                f"视频 head={head[:8].hex()}）"
            )
            return video_socket, control_socket
        log_tail = await self._server_log_tail()
        raise ScrcpyError(
            f"连接 scrcpy 端口失败：{last_error or '超时'}"
            + (f"；server 日志：{log_tail}" if log_tail else "")
        )

    def _connect_ready_control(self, port: int) -> socket.socket:
        """连接 control socket 并读取 server 写入的 1 字节 dummy byte 判定就绪。"""
        sock = self._connect_tcp(port, _CONNECT_TIMEOUT_S)
        sock.settimeout(_DUMMY_READ_TIMEOUT_S)
        try:
            dummy = sock.recv(1)
        except socket.timeout as exc:
            sock.close()
            raise OSError("scrcpy server 未在超时内就绪") from exc
        except OSError:
            sock.close()
            raise
        if not dummy:
            sock.close()
            raise OSError("scrcpy server 连接被关闭（尚未就绪）")
        sock.settimeout(None)
        return sock

    @staticmethod
    def _connect_tcp(port: int, timeout: float) -> socket.socket:
        return socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)

    @staticmethod
    def _wait_video_readable(sock: socket.socket, timeout: float) -> bytes:
        """等待视频 socket 可读（MSG_PEEK 不消费数据，随后交给解码管道）。"""
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                readable, _writable, _errors = select.select([sock], [], [], min(0.1, remaining))
            except OSError:
                return b""
            if not readable:
                continue
            try:
                return sock.recv(64, socket.MSG_PEEK)
            except OSError:
                return b""
        return b""

    async def _server_log_tail(self, lines: int = 20) -> str:
        try:
            output = await self._adb_shell(
                f"tail -{int(lines)} {self._remote_log} 2>/dev/null", timeout=10.0
            )
        except Exception:  # noqa: BLE001 - 诊断信息拿不到不影响主流程
            return ""
        return output.strip()[:1000]

    # ------------------------------------------------------------------
    # 视频解码：helper 进程 + shell 管道 + 单文件 tail
    # ------------------------------------------------------------------
    def _prepare_data_dir(self) -> None:
        """准备本地解码产物目录（数据目录/临时目录）。"""
        if self.data_dir is None:
            self.data_dir = Path(tempfile.mkdtemp(prefix="astrbot-scrcpy-"))
            self._owns_data_dir = True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._decoder_file = self.data_dir / _DECODER_FILE_NAME
        self._decoder_log = self.data_dir / _DECODER_LOG_NAME
        for path in (self._decoder_file, self._decoder_log):
            try:
                path.unlink()
            except OSError:
                pass

    def _start_decoder(self, video_socket: socket.socket) -> None:
        """启动视频解码：PyAV 可用时实时解码，否则走 ``scrcpy_feed.py | ffmpeg | scrcpy_frame_writer.py`` 管道。

        管道采用 shell 管道 + image2pipe 逐帧原子替换单文件 JPEG，规避 Python 直接
        Popen 管道流式解码在容器里的兼容性问题；后台线程 tail 该文件解析最新帧。
        """
        if self._av_decoder_available():
            self._start_av_decoder(video_socket)
            return
        ffmpeg = self._resolve_ffmpeg()
        if not ffmpeg:
            self._mark_decoder_unavailable("未找到 ffmpeg，视频解码不可用（控制仍可用）")
            return
        helper = Path(__file__).resolve().parents[1] / "bin" / "scrcpy_feed.py"
        if not helper.is_file():
            self._mark_decoder_unavailable(f"缺少解码管道助手：{helper}")
            return
        frame_writer = (
            Path(__file__).resolve().parents[1] / "bin" / "scrcpy_frame_writer.py"
        )
        if not frame_writer.is_file():
            self._mark_decoder_unavailable(f"缺少帧写出助手：{frame_writer}")
            return
        if self._decoder_file is None or self._decoder_log is None:
            self._mark_decoder_unavailable("本地解码目录未准备好")
            return
        try:
            fd = video_socket.fileno()
        except OSError as exc:
            self._mark_decoder_unavailable(f"获取视频 socket fd 失败：{exc}")
            return
        command = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(helper))} {int(fd)}"
            f" | {shlex.quote(ffmpeg)} -hide_banner -loglevel error"
            " -flags low_delay -threads 1 -probesize 32 -analyzeduration 0"
            " -f h264 -i pipe:0 -f image2pipe -vcodec mjpeg -threads 1 -q:v 4 -flush_packets 1 pipe:1"
            f" 2>> {shlex.quote(str(self._decoder_log))}"
            f" | {shlex.quote(sys.executable)} {shlex.quote(str(frame_writer))}"
            f" {shlex.quote(str(self.data_dir))}"
            f" 2>> {shlex.quote(str(self._decoder_log))}"
        )
        try:
            self._decoder_process = subprocess.Popen(
                guarded_command(["sh", "-c", command], self.data_dir / "decoder.lock"),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(int(fd),),
                start_new_session=True,
            )
        except OSError as exc:
            self._mark_decoder_unavailable(f"启动解码管道失败：{exc}")
            return
        async def heartbeat():
            process = self._decoder_process
            try:
                while process is not None and process.poll() is None:
                    process.stdin.write(b".\n")
                    process.stdin.flush()
                    await asyncio.sleep(INTERVAL)
            finally:
                if process is not None:
                    process.stdin.close()
        self._decoder_heartbeat = asyncio.create_task(
            heartbeat(), name=f"{PLUGIN_NAME}_decoder_heartbeat")
        self._decoder_thread = threading.Thread(
            target=self._decoder_tail_loop,
            args=(self._decoder_process, self._decoder_file, self._decoder_log),
            name="scrcpy-decoder-tail",
            daemon=True,
        )
        self._decoder_thread.start()
        self._log("scrcpy 解码管道已启动（shell pipeline + 单文件 JPEG tail）")

    @staticmethod
    def _av_decoder_available() -> bool:
        """PyAV 是否可用（环境安装了 ``av`` 时启用，缺失时回退 ffmpeg 管道）。"""
        try:
            import av  # noqa: F401

            return True
        except Exception:  # noqa: BLE001 - 缺依赖时回退 ffmpeg 管道
            return False

    def _start_av_decoder(self, video_socket: socket.socket) -> None:
        """PyAV 实时解码：按 H.264 解析器解包，无需等待输入 EOF。

        ffmpeg 的 h264 裸流 demuxer 在输入不结束时（静态画面）会缓冲首帧不输出，
        实时监控/截图因此拿不到画面；PyAV 直接解析 NAL 并逐包解码可即时出帧。
        """
        self._av_decoder = True
        self._decoder_thread = threading.Thread(
            target=self._av_decode_loop,
            args=(video_socket,),
            name="scrcpy-av-decoder",
            daemon=True,
        )
        self._decoder_thread.start()
        self._log("scrcpy 解码：PyAV 实时解码已启动")

    def _av_decode_loop(self, sock: socket.socket) -> None:
        """读取裸 H.264 流并用 PyAV 解码成 JPEG 帧写入环形缓冲。"""
        from io import BytesIO

        import av
        from PIL import Image  # noqa: F401 - to_image 依赖 Pillow

        codec = av.CodecContext.create("h264", "r")
        try:
            sock.settimeout(1.0)
        except OSError:
            pass
        while not self._stopping:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stopping:
                    self._on_stream_lost(f"视频 socket 读取失败：{exc}")
                return
            if not chunk:
                if not self._stopping:
                    self._on_stream_lost("视频 socket 关闭")
                return
            try:
                packets = codec.parse(chunk)
            except Exception as exc:  # noqa: BLE001 - 解析失败继续下一块
                with self._state_lock:
                    self._last_error = f"scrcpy H.264 解析失败：{exc}"[:300]
                continue
            for packet in packets:
                try:
                    frames = codec.decode(packet)
                except Exception as exc:  # noqa: BLE001 - 坏帧跳过
                    with self._state_lock:
                        self._last_error = f"scrcpy H.264 解码失败：{exc}"[:300]
                    continue
                for frame in frames:
                    try:
                        image = frame.to_image()
                        buffer = BytesIO()
                        image.save(buffer, format="JPEG", quality=72)
                        self._store_frame(buffer.getvalue())
                    except Exception as exc:  # noqa: BLE001 - 单帧转换失败跳过
                        with self._state_lock:
                            self._last_error = f"scrcpy 帧转 JPEG 失败：{exc}"[:300]
        if not self._stopping:
            self._on_stream_lost("PyAV 解码线程退出")

    def _mark_decoder_unavailable(self, reason: str) -> None:
        """解码能力不可用：保留控制能力，视频 socket 用丢弃线程保持 server 健康。"""
        with self._state_lock:
            self._decoder_failed = True
            self._last_error = f"scrcpy 视频解码不可用：{reason}"[:300]
        self._log(self.last_error)
        if self._video_socket is not None and self._drain_thread is None:
            self._drain_thread = threading.Thread(
                target=self._drain_video_loop,
                args=(self._video_socket,),
                name="scrcpy-video-drain",
                daemon=True,
            )
            self._drain_thread.start()

    def _drain_video_loop(self, sock: socket.socket) -> None:
        """无解码器时丢弃视频字节，避免 server 端 socket 缓冲阻塞。"""
        try:
            while not self._stopping:
                chunk = sock.recv(65536)
                if not chunk:
                    break
        except OSError:
            pass
        finally:
            if not self._stopping:
                self._on_stream_lost("视频 socket 关闭")

    def _decoder_tail_loop(
        self,
        process: subprocess.Popen,
        file_path: Path,
        log_path: Path,
    ) -> None:
        """后台 tail 解码输出文件，解析最新 JPEG 帧放入环形缓冲。"""
        last_signature: tuple[int, int] | None = None
        started = time.monotonic()
        logged_empty = False
        while not self._stopping:
            exit_code = process.poll()
            if exit_code is not None:
                self._try_read_decoder_file(file_path)
                if not self._stopping:
                    detail = self._decoder_log_tail(log_path)
                    self._on_decoder_lost(
                        f"解码管道退出（code={exit_code}）" + (f"：{detail}" if detail else "")
                    )
                return
            signature: tuple[int, int] | None = None
            try:
                stat = file_path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                signature = None
            if signature is not None and signature != last_signature:
                last_signature = signature
                self._try_read_decoder_file(file_path)
            elif not logged_empty and time.monotonic() - started > 5.0 and self._frame_count == 0:
                logged_empty = True
                self._log("scrcpy 解码管道启动 5s 内未产生 JPEG 帧（继续观察）")
            time.sleep(_FRAME_POLL_INTERVAL_S)

    def _try_read_decoder_file(self, file_path: Path) -> bool:
        """读取解码输出文件，解析出 JPEG 帧则写入环形缓冲并返回 True。"""
        try:
            data = file_path.read_bytes()
        except OSError:
            return False
        if not data or len(data) < 128:
            return False
        parser = MjpegFrameParser()
        try:
            frames = parser.feed(data)
        except ScrcpyError as exc:
            self._log(f"解析解码输出文件失败：{exc}")
            return False
        if not frames:
            return False
        return self._store_frame(frames[-1])

    def _store_frame(self, payload: bytes) -> bool:
        """把一帧 JPEG 放入环形缓冲，并更新视频尺寸/帧计数。"""
        size = parse_jpeg_size(payload)
        if not size:
            return False
        width, height = size
        with self._state_lock:
            self._decoded_size = (width, height)
            self._video_size = (width, height)
            self._frame_count += 1
        frame = ScrcpyFrame(
            data=payload,
            ts=time.time(),
            width=width,
            height=height,
        )
        with self._frame_lock:
            self._frames.append(frame)
        return True

    @staticmethod
    def _decoder_log_tail(log_path: Path, max_chars: int = 400) -> str:
        try:
            data = log_path.read_bytes()
        except OSError:
            return ""
        return data.decode("utf-8", "replace").strip()[-max_chars:]

    def _on_decoder_lost(self, reason: str) -> None:
        """解码中断：记录可读错误，并按需调度一次整连接自动重连。"""
        self._on_stream_lost(f"视频解码中断：{reason}", decoder=True)

    def _on_stream_lost(self, reason: str, *, decoder: bool = False) -> None:
        """视频流/解码/控制通道中断：记录可读错误，并按需调度一次整连接自动重连。"""
        with self._state_lock:
            if decoder:
                self._decoder_failed = True
            self._stream_failed = True
            self._last_error = f"scrcpy {reason}"[:300]
            if self._stop_requested or self._stopping or not self._started or not self._reconnect_available:
                loop = None
            else:
                self._reconnect_available = False
                loop = self._loop
        self._log(self.last_error)
        if loop is None or loop.is_closed():
            return
        try:
            def schedule():
                if not self._stop_requested:
                    self._reconnect_task = asyncio.create_task(
                        self._reconnect_once(), name=f"{PLUGIN_NAME}_scrcpy_reconnect")
            loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            pass

    async def _reconnect_once(self) -> None:
        """断流后清理并重连一次（最多一次，失败则保持 stopped 由上层回退 ADB）。"""
        try:
            async with self._asyncio_lock():
                if self._stop_requested:
                    return
                await self._stop_internal()
                if self._stop_requested:
                    return
                with self._state_lock:
                    self._decoder_failed = False
                    self._stream_failed = False
                    self._frame_count = 0
                    self._last_error = ""
                await self._start_internal()
            self._log("scrcpy 自动重连成功")
        except asyncio.CancelledError:
            await self._cleanup_resources()
            raise
        except Exception as exc:  # noqa: BLE001 - 重连失败保持 stopped，由上层回退 ADB
            with self._state_lock:
                self._started = False
                self._last_error = f"scrcpy 自动重连失败：{exc}"[:300]
            self._log(self.last_error)

    # ------------------------------------------------------------------
    # 停止/清理
    # ------------------------------------------------------------------
    def quiesce(self) -> None:
        """关闭重连许可，供插件撤回心跳前同步调用。"""
        self._stop_requested = True
        self._reconnect_available = False

    async def stop(self) -> None:
        """停止解码与连接，kill server、移除 forward（幂等）。

        远端 jar/log 保留在设备上供下次启动复用，避免重复 ``adb push``。
        """
        self.quiesce()
        task = self._reconnect_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._reconnect_task = None
        async with self._asyncio_lock():
            await self._stop_internal()

    async def close(self) -> None:
        """``stop`` 的别名（便于后端管理器统一调用）。"""
        await self.stop()

    async def _stop_internal(self) -> None:
        with self._state_lock:
            self._stopping = True
            self._started = False
        try:
            await self._stop_decoder()
            self._close_sockets()
            await self._remove_forward()
            await self._kill_server()
        finally:
            with self._state_lock:
                self._stopping = False
                self._stream_failed = False
                self._frame_count = 0
                self._video_stream_head_bytes = b""
                if self._device_size is None:
                    self._video_size = None
                elif self.video:
                    self._video_size = compute_video_size(
                        *self._device_size, self.max_size
                    )
                else:
                    self._video_size = (
                        int(self._device_size[0]),
                        int(self._device_size[1]),
                    )
            with self._frame_lock:
                self._frames.clear()
            self._decoded_size = None
            self._cleanup_decoder_files()
            self._log("scrcpy 已停止")

    async def _cleanup_resources(self) -> None:
        """启动失败时的尽力清理。"""
        with self._state_lock:
            self._stopping = True
            self._started = False
        try:
            await self._stop_decoder()
            self._close_sockets()
            await self._remove_forward()
            await self._kill_server()
        finally:
            with self._state_lock:
                self._stopping = False
            self._cleanup_decoder_files()

    async def _stop_decoder(self) -> None:
        from .process_lease import stop_heartbeat
        await stop_heartbeat(self._decoder_heartbeat)
        self._decoder_heartbeat = None
        process = self._decoder_process
        self._decoder_process = None
        if process is not None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    process.terminate()
                except OSError:
                    pass
            await asyncio.to_thread(self._wait_process, process, 2.0)
            if process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    try:
                        process.kill()
                    except OSError:
                        pass
        self._join_thread(self._decoder_thread, "_decoder_thread")
        self._join_thread(self._drain_thread, "_drain_thread")

    @staticmethod
    def _wait_process(process: subprocess.Popen, timeout: float) -> None:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def _join_thread(self, thread: threading.Thread | None, attr: str) -> None:
        if thread is not None and thread is not threading.current_thread():
            if thread.is_alive():
                thread.join(timeout=2.0)
        setattr(self, attr, None)

    def _close_sockets(self) -> None:
        for attr in ("_video_socket", "_control_socket"):
            sock = getattr(self, attr)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(self, attr, None)

    def _cleanup_decoder_files(self) -> None:
        if self._owns_data_dir and self.data_dir is not None:
            shutil.rmtree(self.data_dir, ignore_errors=True)
            self._owns_data_dir = False
        self._decoder_file = None
        self._decoder_log = None

    async def _remove_forward(self) -> None:
        port = self._forward_port
        self._forward_port = None
        if port is None:
            return
        try:
            await self._adb_host_ok("forward", "--remove", f"tcp:{port}", timeout=10.0)
            self._log(f"已移除 scrcpy forward tcp:{port}")
        except ScrcpyError as exc:
            self._log(f"移除 scrcpy forward 失败：{exc}")

    async def _kill_server(self) -> None:
        if self._device_lease is not None:
            await self._device_lease.close()
            self._device_lease = None
        pid = self._server_pid
        self._server_pid = None
        if pid is None:
            return
        try:
            await self._adb_shell(f"kill {int(pid)} 2>/dev/null; true", timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - 清理失败只记录
            self._log(f"kill scrcpy server（pid={pid}）失败：{exc}")
            return
        self._log(f"已 kill scrcpy server（pid={pid}）")

    # ------------------------------------------------------------------
    # 解码能力
    # ------------------------------------------------------------------
    def _resolve_ffmpeg(self) -> str | None:
        if self._ffmpeg_checked:
            return self._ffmpeg_resolved
        self._ffmpeg_checked = True
        candidate = self.ffmpeg_path
        if candidate:
            found = candidate if Path(candidate).is_file() else shutil.which(candidate)
            if not found:
                self._log(f"指定的 ffmpeg 不存在：{candidate}，视频解码能力关闭")
                self._ffmpeg_resolved = None
                return None
            self._ffmpeg_resolved = str(found)
            return self._ffmpeg_resolved
        found = shutil.which("ffmpeg")
        self._ffmpeg_resolved = str(found) if found else None
        return self._ffmpeg_resolved

    # ------------------------------------------------------------------
    # 帧读取
    # ------------------------------------------------------------------
    def latest_frame(self, *, max_age_ms: int = 0) -> ScrcpyFrame | None:
        """返回环形缓冲中的最新帧；仅控制通道或 ``max_age_ms>0`` 过期时返回 None。"""
        if not self.video:
            return None
        with self._frame_lock:
            if not self._frames:
                return None
            frame = self._frames[-1]
        if max_age_ms and max_age_ms > 0:
            age_ms = (time.time() - frame.ts) * 1000
            if age_ms > max_age_ms:
                return None
        return frame

    # ------------------------------------------------------------------
    # 坐标/设备尺寸
    # ------------------------------------------------------------------
    async def get_device_size(self) -> tuple[int, int]:
        """返回设备真实像素分辨率（缓存；取不到时抛 ``ScrcpyError``）。"""
        with self._state_lock:
            size = self._device_size
        if size is not None:
            return size
        await self._query_device_size()
        with self._state_lock:
            size = self._device_size
            video = self._video_size
        if size is not None:
            return size
        if video is not None:
            return video
        raise ScrcpyError("无法获取设备分辨率（wm size 失败）")

    def _control_video_size(self) -> tuple[int, int]:
        """控制消息中使用的视频尺寸（必须与 server 当前 videoSize 一致）。"""
        with self._state_lock:
            size = self._decoded_size or self._video_size or self._device_size
        if size is None:
            raise ScrcpyError("scrcpy 视频尺寸未知，无法发送触摸事件")
        return int(size[0]), int(size[1])

    def _control_device_size(self) -> tuple[int, int]:
        with self._state_lock:
            if self._device_size is not None:
                return self._device_size
        return self._control_video_size()

    def _device_to_video_point(self, x: int, y: int) -> tuple[int, int, int, int]:
        """把设备像素坐标换算为视频坐标，返回 (vx, vy, video_w, video_h)。"""
        try:
            x_int, y_int = int(x), int(y)
        except (TypeError, ValueError) as exc:
            raise ScrcpyError(f"坐标必须是整数：({x!r}, {y!r})") from exc
        device_w, device_h = self._control_device_size()
        video_w, video_h = self._control_video_size()
        if device_w <= 0 or device_h <= 0:
            raise ScrcpyError("设备分辨率未知，无法换算 scrcpy 触摸坐标")
        if not (0 <= x_int < device_w and 0 <= y_int < device_h):
            raise ScrcpyError(
                f"坐标越界：({x_int}, {y_int})，设备尺寸 {device_w}x{device_h}"
            )
        vx = int(round(x_int * video_w / device_w))
        vy = int(round(y_int * video_h / device_h))
        vx = max(0, min(video_w - 1, vx))
        vy = max(0, min(video_h - 1, vy))
        return vx, vy, video_w, video_h

    # ------------------------------------------------------------------
    # 控制通道
    # ------------------------------------------------------------------
    def _require_control(self) -> socket.socket:
        if not self._started or self._control_socket is None:
            raise ScrcpyError("scrcpy 控制通道未连接")
        return self._control_socket

    async def _send_control(self, payload: bytes) -> None:
        sock = self._require_control()

        def _send() -> None:
            with self._control_send_lock:
                sock.sendall(payload)

        try:
            await asyncio.to_thread(_send)
        except OSError as exc:
            self._on_stream_lost(f"控制通道写入失败：{exc}")
            raise ScrcpyError(f"scrcpy 控制通道写入失败：{exc}") from exc

    async def request_frame(self) -> None:
        """使用 scrcpy RESET_VIDEO 刷新静态画面，保持现有服务和控制连接。"""
        if not self.video:
            raise ScrcpyError("视频通道未启用")
        await self._send_control(bytes((17,)))

    async def get_clipboard(self, *, timeout: float = 3.0) -> str:
        """请求设备当前剪贴板，逐帧读取回应；不触发复制按键或改变剪贴板。"""
        def receive() -> str:
            with self._control_receive_lock:
                sock = self._require_control()
                deadline = time.monotonic() + timeout

                def exact(size: int) -> bytes:
                    data = bytearray()
                    while len(data) < size:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not select.select([sock], [], [], remaining)[0]:
                            raise ScrcpyError("读取剪贴板超时，设备未回应")
                        chunk = sock.recv(size - len(data))
                        if not chunk:
                            raise ScrcpyError("读取剪贴板时控制通道已关闭")
                        data.extend(chunk)
                    return bytes(data)

                with self._control_send_lock:
                    sock.sendall(bytes((TYPE_GET_CLIPBOARD, 0)))
                while True:
                    kind = exact(1)[0]
                    if kind == 0:
                        length = struct.unpack(">I", exact(4))[0]
                        if length > (1 << 18) - 5:
                            raise ScrcpyError("剪贴板回应长度超出协议限制")
                        return exact(length).decode("utf-8")
                    if kind == 1:
                        exact(8)
                    elif kind == 2:
                        _id, length = struct.unpack(">HH", exact(4))
                        exact(length)
                    else:
                        raise ScrcpyError(f"未知设备控制消息：{kind}")
        try:
            return await asyncio.to_thread(receive)
        except asyncio.CancelledError:
            sock = self._control_socket
            if sock is not None:
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
            self._on_stream_lost("剪贴板读取已取消")
            raise
        except (OSError, ValueError, UnicodeError, ScrcpyError) as exc:
            # 关闭本次流，避免迟到回应被下一次读取误认为新剪贴板。
            sock = self._control_socket
            if sock is not None:
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
            self._on_stream_lost(f"剪贴板读取失败：{exc}")
            raise ScrcpyError(f"剪贴板读取失败：{exc}") from exc

    async def _send_touch(
        self,
        action: int,
        x: int,
        y: int,
        *,
        pressure: int = PRESSURE_MAX,
        pointer_id: int = 0,
    ) -> None:
        # 懒查询设备分辨率（缓存命中时开销极小）；仅控制通道需要它做坐标换算
        await self.get_device_size()
        vx, vy, video_w, video_h = self._device_to_video_point(x, y)
        payload = encode_touch(
            action,
            vx,
            vy,
            video_w,
            video_h,
            pressure=pressure,
            pointer_id=pointer_id,
        )
        await self._send_control(payload)

    async def tap(self, x: int, y: int) -> None:
        """在设备像素坐标 ``(x, y)`` 上点击。"""
        self._require_control()
        await self._send_touch(ACTION_DOWN, x, y, pressure=PRESSURE_MAX)
        await asyncio.sleep(0.02)
        await self._send_touch(ACTION_UP, x, y, pressure=PRESSURE_NONE)

    async def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        """长按设备像素坐标 ``(x, y)`` ``duration_ms`` 毫秒。"""
        duration = max(1, int(duration_ms))
        if duration > 60_000:
            raise ScrcpyError(f"长按时长过长：{duration}ms")
        self._require_control()
        await self._send_touch(ACTION_DOWN, x, y, pressure=PRESSURE_MAX)
        await asyncio.sleep(duration / 1000.0)
        await self._send_touch(ACTION_UP, x, y, pressure=PRESSURE_NONE)

    async def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 400,
    ) -> None:
        """从设备像素坐标 ``(x1,y1)`` 滑到 ``(x2,y2)``。"""
        duration = max(1, int(duration_ms))
        if duration > 60_000:
            raise ScrcpyError(f"滑动时长过长：{duration}ms")
        self._require_control()
        await self._send_touch(ACTION_DOWN, x1, y1, pressure=PRESSURE_MAX)
        steps = max(2, min(120, int(round(duration / 16.0))))
        delay = duration / steps / 1000.0
        for index in range(1, steps + 1):
            ratio = index / steps
            point_x = int(round(x1 + (x2 - x1) * ratio))
            point_y = int(round(y1 + (y2 - y1) * ratio))
            await asyncio.sleep(delay)
            await self._send_touch(ACTION_MOVE, point_x, point_y, pressure=PRESSURE_MAX)
        await asyncio.sleep(delay)
        await self._send_touch(ACTION_UP, x2, y2, pressure=PRESSURE_NONE)

    async def swipe_direction(
        self,
        direction: str,
        *,
        duration_ms: int = 400,
        ratio: float = 0.6,
    ) -> None:
        """按方向（``up``/``down``/``left``/``right``）在屏幕中部滑动。"""
        name = str(direction or "").strip().lower()
        if name not in ("up", "down", "left", "right"):
            raise ScrcpyError(
                f"滑动方向非法：{direction!r}（可选 up/down/left/right）"
            )
        ratio_value = float(ratio)
        if not 0 < ratio_value <= 1:
            raise ScrcpyError(f"ratio 必须在 (0, 1] 区间内：{ratio}")
        width, height = await self.get_device_size()
        center_x, center_y = width // 2, height // 2
        delta_x = int(width * ratio_value / 2)
        delta_y = int(height * ratio_value / 2)
        if name == "up":
            start_y = min(height - 1, center_y + delta_y)
            end_y = max(0, center_y - delta_y)
            await self.swipe(center_x, start_y, center_x, end_y, duration_ms)
        elif name == "down":
            start_y = max(0, center_y - delta_y)
            end_y = min(height - 1, center_y + delta_y)
            await self.swipe(center_x, start_y, center_x, end_y, duration_ms)
        elif name == "left":
            start_x = min(width - 1, center_x + delta_x)
            end_x = max(0, center_x - delta_x)
            await self.swipe(start_x, center_y, end_x, center_y, duration_ms)
        else:
            start_x = max(0, center_x - delta_x)
            end_x = min(width - 1, center_x + delta_x)
            await self.swipe(start_x, center_y, end_x, center_y, duration_ms)

    async def press_key(self, key: int | str) -> None:
        """按下并松开按键（接受 keycode 整数或按键名，如 ``"enter"``）。"""
        keycode = resolve_keycode(key)
        self._require_control()
        await self._send_control(encode_key(ACTION_DOWN, keycode))
        await asyncio.sleep(0.02)
        await self._send_control(encode_key(ACTION_UP, keycode))

    async def back(self) -> None:
        """返回键（scrcpy ``back_or_screen_on`` 消息；屏幕熄灭时会点亮）。"""
        self._require_control()
        await self._send_control(encode_back(ACTION_DOWN))
        await asyncio.sleep(0.02)
        await self._send_control(encode_back(ACTION_UP))

    async def inject_text(self, text: str) -> bool:
        """注入文本；仅支持可映射的 ASCII，返回 False 表示应回退 ADBKeyboard。"""
        if not self._started or self._control_socket is None:
            return False
        value = str(text or "")
        if not value:
            return True
        if not value.isascii() or any(not 0x20 <= ord(char) <= 0x7E for char in value):
            return False
        try:
            await self._send_control(encode_text(value))
        except ScrcpyError as exc:
            self._log(f"scrcpy 注入文本失败：{exc}")
            return False
        return True
