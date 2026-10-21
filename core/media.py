"""流媒体内容识别：设备播放音频采集与关键帧/转写辅助。

手机上的视频、直播、音乐看不到也听不到时，主 Agent 只能靠标题猜内容。本模块
用 scrcpy v5.0.1 的音频通道把「设备正在播放的声音」采集成 PCM，再交给 AstrBot
的 STT Provider 转写；画面部分由工具层按间隔抓关键帧交给视觉 Provider 描述。

音频通道实现要点（scrcpy v5.0.1 真机验证）：

* server 以 ``audio=true audio_codec=raw audio_source=output video=false
  control=false raw_stream=true send_dummy_byte=true`` 启动；音频源 ``output``
  对应 ``REMOTE_SUBMIX``，能拿到整个设备输出（Android 11+ 可用，且会把声音从
  扬声器改道到采集端，不依赖 App 是否允许播放捕获）。
* ``raw_stream=true`` 会关闭 stream/frame 元数据（没有 codec id、没有 12 字节
  包头），随后显式追加 ``send_dummy_byte=true`` 才能在首个 socket 上收到 1 字节
  dummy 用于就绪判定；参数顺序不能颠倒。
* 每次采集随机 ``scid``（31 位），socket 名 ``scrcpy_<8位hex>``，独立
  ``adb forward`` 本地端口；结束只 kill 本次 server PID、只移除本次 forward，
  不与控制后端/其他采集抢资源。
* dummy 之后是 48kHz/双声道/s16le PCM，固定 192000 字节/秒。

模块不导入 astrbot，便于本地单测；``adb`` 传入 :class:`core.adb.AdbClient` 或
提供 ``shell()`` / ``run()`` / ``ensure_ready()`` / ``serial`` 的鸭子类型。
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import socket
import struct
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .process_lease import DeviceLease

from .errors import MediaCaptureError, PhoneControlError

SERVER_CLASS = "com.genymobile.scrcpy.Server"
SERVER_VERSION_FALLBACK = "5.0.1"
REMOTE_DIR = "/data/local/tmp"
REMOTE_JAR_TEMPLATE = REMOTE_DIR + "/scrcpy-server-{version}.jar"
REMOTE_LOG_TEMPLATE = REMOTE_DIR + "/scrcpy-media-{scid}.log"
SOCKET_PREFIX = "scrcpy"

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE  # 192000

MIN_SECONDS = 3.0
MAX_SECONDS = 300.0
DEFAULT_SECONDS = 20.0
DEFAULT_MAX_SECONDS = 60.0
DEFAULT_FRAME_INTERVAL_SECONDS = 4.0
DEFAULT_MAX_FRAMES = 4

AUDIO_SOURCES = ("output", "mic")
DEFAULT_AUDIO_SOURCE = "output"

_CONNECT_TIMEOUT_S = 6.0
_CONNECT_RETRY_S = 0.15
_DUMMY_TIMEOUT_S = 4.0
_READ_IDLE_TIMEOUT_S = 4.0
_MIN_VALID_SECONDS = 0.3
_MAX_TRANSCRIPT_CHARS = 4000
_SILENCE_PEAK = 3

LogFn = Callable[[str], None]


# ----------------------------------------------------------------------
# 纯函数：秒数/关键帧/文本/WAV
# ----------------------------------------------------------------------


def clamp_seconds(
    value: Any,
    *,
    default: float = DEFAULT_SECONDS,
    maximum: float = DEFAULT_MAX_SECONDS,
    minimum: float = MIN_SECONDS,
) -> float:
    """把请求时长收敛到 ``[minimum, maximum]``；非法值回退 ``default``。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = float(default)
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = float(default)
    lower = max(0.1, float(minimum))
    upper = max(lower, float(maximum))
    return float(min(upper, max(lower, seconds)))


def plan_frame_times(
    seconds: float,
    *,
    interval: float = DEFAULT_FRAME_INTERVAL_SECONDS,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> list[float]:
    """规划录制期间的关键帧抓取时刻（相对录制开始的秒数，升序）。

    帧数取 ``min(max_frames, ceil(seconds/interval))``，在整段时长内均匀铺开，
    避免只覆盖前几秒；返回值只用于工具层按时刻 sleep，不依赖具体视频内容。
    """
    try:
        total = max(0.0, float(seconds))
    except (TypeError, ValueError):
        total = 0.0
    try:
        every = max(0.5, float(interval))
    except (TypeError, ValueError):
        every = DEFAULT_FRAME_INTERVAL_SECONDS
    try:
        limit = max(1, int(max_frames))
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_FRAMES
    if total <= 0:
        return []
    count = min(limit, max(1, int(math.ceil(total / every))))
    step = total / (count + 1)
    return [round(step * (index + 1), 3) for index in range(count)]


def pcm_duration(pcm_bytes: int) -> float:
    """按 48kHz/双声道/s16le 计算 PCM 时长（秒）。"""
    return max(0, int(pcm_bytes)) / float(BYTES_PER_SECOND)


def write_wav(
    path: str | Path,
    pcm: bytes,
    *,
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
) -> float:
    """把 s16le PCM 写成 WAV，返回实际写入时长（秒）；空数据写空 WAV。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame_bytes = max(1, int(channels)) * BYTES_PER_SAMPLE
    usable = len(pcm) - (len(pcm) % frame_bytes)
    data = pcm[:usable]
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(int(channels))
        handle.setsampwidth(BYTES_PER_SAMPLE)
        handle.setframerate(int(sample_rate))
        handle.writeframes(data)
    return pcm_duration(len(data))


def pcm_peak(pcm: bytes, *, sample_limit: int = 240000) -> int:
    """采样估计 PCM 峰值：在整段音频上分散取若干窗口，避免只看开头误判静音。"""
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0
    budget = max(2, int(sample_limit))
    windows = 4 if usable >= 32768 and budget >= 8192 else 1
    window_bytes = max(2, (budget // windows) // 2 * 2)
    peak = 0
    for index in range(windows):
        if windows == 1:
            start = 0
        else:
            start = int((usable - window_bytes) * index / (windows - 1))
        start -= start % 2
        chunk = pcm[start : start + window_bytes]
        if not chunk:
            continue
        samples = struct.unpack("<%dh" % (len(chunk) // 2), chunk)
        peak = max(peak, max(abs(sample) for sample in samples))
    return peak


def clean_transcript(text: Any, *, limit: int = _MAX_TRANSCRIPT_CHARS) -> str:
    """整理 ASR 返回文本：合并空白、去掉空行，按长度截断。"""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in raw.split("\n")]
    joined = "\n".join(line for line in lines if line).strip()
    if limit > 0 and len(joined) > limit:
        joined = joined[: max(1, int(limit) - 1)].rstrip() + "…"
    return joined


def build_vision_prompt(
    frame_count: int,
    *,
    question: str = "",
    audio_available: bool = True,
) -> str:
    """构造关键帧视觉描述提示词（中文、要求客观、不编造声音内容）。"""
    count = max(1, int(frame_count))
    prompt = (
        f"下面是从手机正在播放的内容中按时间顺序截取的 {count} 张画面。"
        "请客观描述：1) 画面主体/场景/人物与动作变化；2) 屏幕上可见的文字、字幕、"
        "标题、弹幕或数据；3) 画面之间的变化（是否同一段内容、是否有转场）。"
        "只写你实际看到的内容，不要编造声音、旁白或剧情。"
        "用简洁中文分点回答，总字数不超过 300 字。"
    )
    if not audio_available:
        prompt += "这次没有可用的声音信息，请只依据画面作答。"
    focus = " ".join(str(question or "").split())[:200]
    if focus:
        prompt += f"\n重点关注：{focus}"
    return prompt


def safe_unlink(path: str | Path) -> None:
    """删除文件（失败忽略，供收尾清理使用）。"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# ----------------------------------------------------------------------
# 音频采集
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AudioClip:
    """一次设备播放音频采集的结果。"""

    path: Path
    seconds: float
    pcm_bytes: int
    source: str
    scid: int
    peak: int = 0
    truncated: bool = False
    warning: str = ""

    @property
    def silent(self) -> bool:
        """采到的音频是否几乎全为静音（DRM/禁录/设备静音时的典型表现）。"""
        return self.peak <= _SILENCE_PEAK

    def summary(self) -> str:
        """一行可读摘要（日志/工具结果用）。"""
        text = f"{self.seconds:.1f}s/{self.source}"
        if self.truncated:
            text += "（不完整）"
        if self.silent:
            text += "（近乎静音）"
        return text


def free_local_port() -> int:
    """申请一个空闲本地 TCP 端口（关闭探测 socket 后返回）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _connect_socket(port: int, timeout: float = _CONNECT_TIMEOUT_S) -> socket.socket:
    """连接本地 adb forward 端口（同步，供线程/重试使用）。"""
    sock = socket.create_connection(("127.0.0.1", int(port)), timeout=max(0.2, float(timeout)))
    sock.settimeout(_READ_IDLE_TIMEOUT_S)
    return sock


def _read_dummy(sock: socket.socket) -> bytes:
    """读取 server 在首个 socket 上发送的 1 字节 dummy。"""
    sock.settimeout(_DUMMY_TIMEOUT_S)
    data = sock.recv(1)
    return data or b""


def _read_pcm_stream(sock: socket.socket, target_bytes: int) -> bytes:
    """读取原始 PCM，直到达到目标字节数或数据中断。"""
    target = max(0, int(target_bytes))
    buffer = bytearray()
    sock.settimeout(_READ_IDLE_TIMEOUT_S)
    while len(buffer) < target:
        try:
            chunk = sock.recv(min(65536, target - len(buffer)))
        except socket.timeout:
            break
        if not chunk:
            break
        buffer.extend(chunk)
    return bytes(buffer)


def _parse_server_version(jar_name: str) -> str:
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", jar_name)
    return match.group(1) if match else SERVER_VERSION_FALLBACK


async def _maybe_await(value: Any) -> Any:
    """兼容同步/异步鸭子类型返回值。"""
    if asyncio.isfuture(value) or asyncio.iscoroutine(value):
        return await value
    return value


class MediaAudioCapture:
    """用 scrcpy 音频通道采集设备正在播放的声音。

    Args:
        adb: ``AdbClient`` 或鸭子类型（``shell`` / ``run`` / ``ensure_ready``）。
        server_jar: scrcpy server jar 路径，或返回路径的同步/异步 callable
            （配置热应用时由调用方传入 provider）。
        data_dir: 音频文件保存目录（插件数据目录下）。
        logger: 日志回调（可选）。
    """

    def __init__(
        self,
        adb: Any,
        *,
        server_jar: str | Path | Callable[[], Any],
        data_dir: str | Path,
        logger: LogFn | None = None,
    ) -> None:
        self._device_lease = None
        self._adb = adb
        self._server_jar = server_jar
        self.data_dir = Path(data_dir)
        self._logger = logger
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    # ---- 基础 -----------------------------------------------------

    def _log(self, message: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger(message)
        except Exception:  # noqa: BLE001 - 日志回调不允许打断采集
            pass

    def _capture_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def _resolve_jar(self) -> Path:
        jar = self._server_jar() if callable(self._server_jar) else self._server_jar
        jar = await _maybe_await(jar)
        path = Path(str(jar)).expanduser()
        if not path.is_file():
            raise MediaCaptureError(f"scrcpy server jar 不存在：{path}")
        if path.stat().st_size <= 0:
            raise MediaCaptureError(f"scrcpy server jar 为空文件：{path}")
        return path

    async def _ensure_ready(self) -> None:
        ensure = getattr(self._adb, "ensure_ready", None)
        if callable(ensure):
            await _maybe_await(ensure())

    async def _shell(self, command: str, *, timeout: float = 30.0) -> str:
        shell = getattr(self._adb, "shell", None)
        if not callable(shell):
            raise MediaCaptureError("ADB 客户端不支持 shell 命令，无法采集设备音频")
        try:
            result = await _maybe_await(shell(command, timeout=timeout))
        except TypeError:
            result = await _maybe_await(shell(command))
        return str(result or "")

    async def _run_host(self, args: list[str], *, timeout: float = 30.0) -> str:
        """执行 adb 主机侧命令（forward/push/--remove）。"""
        runner = getattr(self._adb, "run", None)
        if callable(runner):
            result = await _maybe_await(runner(args, timeout=timeout))
            return str(result or "")
        runner = getattr(self._adb, "_run_adb", None)
        adb_path = getattr(self._adb, "_adb_path", None)
        serial = getattr(self._adb, "serial", "")
        if callable(runner) and adb_path:
            argv = [str(adb_path), "-s", str(serial), *args]
            rc, stdout, stderr = await _maybe_await(runner(argv, timeout=timeout))
            if int(rc) != 0:
                raise MediaCaptureError(
                    f"adb {' '.join(args)} 失败（退出码 {rc}）",
                    detail=str(stderr or stdout or "")[:300],
                )
            if isinstance(stdout, bytes):
                return stdout.decode("utf-8", "replace")
            return str(stdout or "")
        raise MediaCaptureError("ADB 客户端不支持主机侧命令（forward/push），无法采集设备音频")

    # ---- 启动/清理 -------------------------------------------------

    async def _ensure_remote_jar(self, jar: Path, remote_jar: str) -> None:
        size = jar.stat().st_size
        try:
            output = await self._shell(
                f"wc -c < {remote_jar} 2>/dev/null || echo -1", timeout=10.0
            )
            remote_size = int(str(output).strip().splitlines()[-1])
        except (TypeError, ValueError, IndexError):
            remote_size = -1
        if remote_size == size:
            return
        self._log(f"推送 scrcpy server：{jar.name}（{size} bytes）")
        await self._run_host(["push", str(jar), remote_jar], timeout=120.0)

    @staticmethod
    def _socket_name(scid: int) -> str:
        return f"{SOCKET_PREFIX}_{scid:08x}"

    def _launch_command(
        self,
        *,
        version: str,
        remote_jar: str,
        remote_log: str,
        scid: int,
        source: str,
    ) -> str:
        """构造音频 server 启动命令（raw_stream 必须在 send_dummy_byte 之前）。"""
        command = (
            f"CLASSPATH={remote_jar} app_process / {SERVER_CLASS} {version} "
            "log_level=info video=false control=false audio=true "
            f"audio_codec=raw audio_source={source} "
            "raw_stream=true send_dummy_byte=true "
            f"tunnel_forward=true cleanup=false scid={scid:x} "
            f"< /dev/null > {remote_log} 2>&1 & echo PID=$!"
        )

        return self._device_lease.wrap_launch(command) if self._device_lease else command

    async def _read_log_tail(self, remote_log: str) -> str:
        try:
            output = await self._shell(f"tail -n 6 {remote_log} 2>/dev/null", timeout=10.0)
        except PhoneControlError:
            return ""
        text = " | ".join(
            " ".join(line.split()) for line in str(output or "").splitlines() if line.strip()
        )
        return text[:500]

    async def _remove_remote_log(self, remote_log: str) -> None:
        """删除本次采集的远端日志（失败忽略，成功/已诊断后调用）。"""
        try:
            await self._shell(f"rm -f {remote_log} 2>/dev/null || true", timeout=10.0)
        except PhoneControlError:
            pass

    async def _stop_server(self, pid: int | None, port: int) -> None:
        if self._device_lease is not None:
            await self._device_lease.close()
            self._device_lease = None
        if pid:
            try:
                await self._shell(f"kill {int(pid)} 2>/dev/null || true", timeout=10.0)
            except PhoneControlError as exc:
                self._log(f"停止音频 server 失败：{exc}")
        try:
            await self._run_host(["forward", "--remove", f"tcp:{int(port)}"], timeout=10.0)
        except PhoneControlError as exc:
            self._log(f"移除音频 forward 失败（端口 {port}）：{exc}")

    async def _connect_audio(self, port: int) -> socket.socket:
        """连接音频 socket 并读到就绪 dummy；server 未绑定前 connect 会成功但立即 EOF。"""
        deadline = time.monotonic() + _CONNECT_TIMEOUT_S
        last_error = ""
        while time.monotonic() < deadline:
            sock: socket.socket | None = None
            try:
                sock = await asyncio.to_thread(_connect_socket, port, 1.0)
                dummy = await asyncio.to_thread(_read_dummy, sock)
                if dummy:
                    return sock
                last_error = "连接建立后未收到就绪字节（server 尚未就绪）"
            except OSError as exc:
                last_error = str(exc)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            await asyncio.sleep(_CONNECT_RETRY_S)
        raise MediaCaptureError(
            "连接设备音频通道超时（server 可能未启动）",
            detail=last_error[:200],
        )

    # ---- 对外采集 --------------------------------------------------

    async def capture(
        self,
        seconds: float = DEFAULT_SECONDS,
        *,
        source: str = DEFAULT_AUDIO_SOURCE,
        maximum: float = MAX_SECONDS,
    ) -> AudioClip:
        """采集设备当前播放声音（默认 20 秒），返回 WAV 文件与元信息。

        Args:
            seconds: 采集时长；内部按 :func:`clamp_seconds` 收敛。
            source: ``output``（设备播放输出，推荐）或 ``mic``（麦克风）。
            maximum: 本次采集时长上限（默认 :data:`MAX_SECONDS`，由插件配置传入）。

        Raises:
            MediaCaptureError: server 启动失败、连接失败或几乎没有采到数据。
        """
        source = str(source or DEFAULT_AUDIO_SOURCE).strip().lower()
        if source not in AUDIO_SOURCES:
            source = DEFAULT_AUDIO_SOURCE
        duration = clamp_seconds(seconds, maximum=maximum)
        async with self._capture_lock():
            return await self._capture_locked(duration, source)

    async def _capture_locked(self, seconds: float, source: str) -> AudioClip:
        jar = await self._resolve_jar()
        version = _parse_server_version(jar.name)
        remote_jar = REMOTE_JAR_TEMPLATE.format(version=version)
        scid = random.getrandbits(31)
        socket_name = self._socket_name(scid)
        remote_log = REMOTE_LOG_TEMPLATE.format(scid=f"{scid:08x}")
        port = free_local_port()
        pid: int | None = None
        sock: socket.socket | None = None
        started = time.monotonic()
        await self._ensure_ready()
        self._device_lease = DeviceLease(self._adb, "audio")
        await self._device_lease.prepare()
        await self._ensure_remote_jar(jar, remote_jar)
        try:
            try:
                await self._run_host(
                    ["forward", f"tcp:{port}", f"localabstract:{socket_name}"],
                    timeout=15.0,
                )
            except PhoneControlError as exc:
                raise MediaCaptureError(f"建立音频端口转发失败：{exc}") from exc

            try:
                output = await self._shell(
                    self._launch_command(
                        version=version,
                        remote_jar=remote_jar,
                        remote_log=remote_log,
                        scid=scid,
                        source=source,
                    ),
                    timeout=30.0,
                )
            except PhoneControlError as exc:
                raise MediaCaptureError(f"启动音频采集 server 失败：{exc}") from exc
            match = re.search(r"PID=(\d+)", str(output or ""))
            if not match:
                raise MediaCaptureError(
                    "启动音频采集 server 后未拿到进程号",
                    detail=" | ".join(str(output or "").splitlines())[:300],
                )
            pid = int(match.group(1))
            self._device_lease.observe(output)
            self._log(
                f"音频采集 server 已启动（pid={pid}，scid={scid:x}，"
                f"source={source}，{seconds:.1f}s）"
            )

            try:
                try:
                    sock = await self._connect_audio(port)
                except MediaCaptureError as exc:
                    log_tail = await self._read_log_tail(remote_log)
                    if log_tail:
                        raise MediaCaptureError(
                            exc.message,
                            detail=f"{exc.detail}；server 日志：{log_tail}"[:600],
                        ) from exc
                    raise
                target_bytes = int(round(seconds * BYTES_PER_SECOND))
                pcm = await asyncio.to_thread(_read_pcm_stream, sock, target_bytes)
            except MediaCaptureError:
                raise
            except OSError as exc:
                raise MediaCaptureError(f"读取设备音频失败：{exc}") from exc
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            await self._stop_server(pid, port)

        actual_seconds = pcm_duration(len(pcm))
        if actual_seconds < _MIN_VALID_SECONDS:
            log_tail = await self._read_log_tail(remote_log)
            await self._remove_remote_log(remote_log)
            raise MediaCaptureError(
                f"未采集到设备播放音频（仅 {actual_seconds:.1f}s）",
                detail=log_tail or "设备可能未在播放声音/音频采集被系统拦截",
            )

        await self._remove_remote_log(remote_log)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / f"audio-{int(time.time() * 1000)}-{scid:08x}.wav"
        written_seconds = write_wav(path, pcm)
        peak = pcm_peak(pcm)
        truncated = actual_seconds + 0.5 < seconds
        warning = ""
        if truncated:
            warning = (
                f"音频采集提前结束（{actual_seconds:.1f}s/{seconds:.1f}s），"
                "可能被其他 scrcpy 会话清理或播放中断"
            )
        if peak <= _SILENCE_PEAK:
            warning = (
                (warning + "；" if warning else "")
                + "采集到的音频近乎静音（App 可能禁止播放捕获/设备静音）"
            )
        self._log(
            f"音频采集完成：{written_seconds:.1f}s，{len(pcm)} bytes，"
            f"peak={peak}，耗时={time.monotonic() - started:.1f}s"
        )
        return AudioClip(
            path=path,
            seconds=written_seconds,
            pcm_bytes=len(pcm),
            source=source,
            scid=scid,
            peak=peak,
            truncated=truncated,
            warning=warning,
        )
