"""minitouch v1.3.2 可选 touch 后端（用户自备 device 端二进制）。

DeviceFarmer/minitouch 没有官方预编译 release，因此本模块不随插件内置二进制：
只有用户显式配置 ``control.minitouch.binary_path`` 时才启用，失败时由上层继续
使用 scrcpy / ADB 控制（不影响主流程）。

启动流程（Android 10 以下的常见用法；Android 10+ 可能需要 STFService agent）：

1. ``adb push <binary_path> /data/local/tmp/minitouch`` + ``chmod 755``；
2. 后台运行 ``/data/local/tmp/minitouch``，它在设备上监听 abstract socket
   ``minitouch``；
3. ``adb forward tcp:<port> localabstract:minitouch`` 后连接该端口；
4. 从 socket 读取 banner（``v 1`` / ``^ max-contacts max-x max-y max-pressure`` /
   ``$ pid``），之后按 LF 分隔的文本协议发送 ``d/m/u/c/w/r`` 命令。

坐标约定：对外 API 使用**设备真实像素**（``wm size``），内部按 banner 给出的
``max_x``/``max_y`` 线性缩放后写入协议。

协议参考：https://github.com/DeviceFarmer/minitouch（v1.3.2，MIT/Apache 混合，
以项目 LICENSE 为准）。
"""

from __future__ import annotations

import asyncio
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .adb import parse_wm_size
from .scrcpy import free_local_port, parse_forward_list

LogFn = Callable[[str], None]

DEFAULT_SOCKET_NAME = "minitouch"
_REMOTE_BIN = "/data/local/tmp/minitouch"
_REMOTE_LOG = "/data/local/tmp/minitouch.log"
_MINITOUCH_NEEDLE = _REMOTE_BIN
_BANNER_VERSION_RE = re.compile(r"^v\s+(\d+)\s*$")
_BANNER_LIMITS_RE = re.compile(r"^\^\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$")
_BANNER_PID_RE = re.compile(r"^\$\s+(\d+)\s*$")

_START_TIMEOUT_S = 10.0
_CONNECT_TIMEOUT_S = 2.0
_BANNER_TIMEOUT_S = 5.0

# 触摸压力使用 banner 的 max_pressure（多数设备 255）；0 表示设备不报告压力
DEFAULT_PRESSURE_RATIO = 1.0


class MinitouchError(RuntimeError):
    """minitouch 后端不可用/协议错误。"""


def parse_banner(text: str) -> dict[str, int]:
    """解析 minitouch banner，返回 version/max_contacts/max_x/max_y/max_pressure/pid。"""
    banner: dict[str, int] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        match = _BANNER_VERSION_RE.match(line)
        if match:
            banner["version"] = int(match.group(1))
            continue
        match = _BANNER_LIMITS_RE.match(line)
        if match:
            banner["max_contacts"] = int(match.group(1))
            banner["max_x"] = int(match.group(2))
            banner["max_y"] = int(match.group(3))
            banner["max_pressure"] = int(match.group(4))
            continue
        match = _BANNER_PID_RE.match(line)
        if match:
            banner["pid"] = int(match.group(1))
    missing = [
        key
        for key in ("max_contacts", "max_x", "max_y", "max_pressure")
        if key not in banner
    ]
    if missing:
        raise MinitouchError(
            "minitouch banner 缺少字段：" + "、".join(missing) + f"（原文：{text[:300]!r}）"
        )
    if banner["max_x"] <= 0 or banner["max_y"] <= 0:
        raise MinitouchError(f"minitouch banner 坐标范围非法：{banner}")
    return banner


def scale_axis(value: int, device_max: int, touch_max: int) -> int:
    """把设备像素坐标缩放到 minitouch 的坐标范围并 clamp。"""
    if device_max <= 0 or touch_max <= 0:
        raise MinitouchError(f"坐标缩放参数非法：{device_max}/{touch_max}")
    scaled = int(round(int(value) * touch_max / device_max))
    return max(0, min(touch_max, scaled))


class MinitouchClient:
    """minitouch 文本协议客户端（可选后端）。"""

    def __init__(
        self,
        adb: Any,
        *,
        binary_path: str | Path,
        logger: LogFn | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        self._adb = adb
        self.binary_path = Path(binary_path).expanduser()
        self._logger = logger
        self.data_dir = Path(data_dir).expanduser() if data_dir else None

        self._started = False
        self._stopping = False
        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._banner: dict[str, int] = {}
        self._device_size: tuple[int, int] | None = None
        self._server_pid: int | None = None
        self._forward_port: int | None = None
        self._asyncio_lock_obj: asyncio.Lock | None = None
        self._asyncio_lock_loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
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
    def banner(self) -> dict[str, int]:
        return dict(self._banner)

    @property
    def max_x(self) -> int:
        return int(self._banner.get("max_x", 0))

    @property
    def max_y(self) -> int:
        return int(self._banner.get("max_y", 0))

    @property
    def max_pressure(self) -> int:
        return int(self._banner.get("max_pressure", 0))

    @property
    def max_contacts(self) -> int:
        return int(self._banner.get("max_contacts", 1))

    @property
    def server_pid(self) -> int | None:
        return self._server_pid

    @property
    def device_size(self) -> tuple[int, int] | None:
        return self._device_size

    # ------------------------------------------------------------------
    # ADB 适配
    # ------------------------------------------------------------------
    async def _adb_ensure_ready(self) -> None:
        ensure = getattr(self._adb, "ensure_ready", None)
        if callable(ensure):
            value = ensure()
            if hasattr(value, "__await__"):
                await value

    async def _adb_shell(self, command: str, *, timeout: float | None = None) -> str:
        shell = getattr(self._adb, "shell", None)
        if not callable(shell):
            raise MinitouchError("ADB 客户端不支持 shell 命令")
        try:
            value = shell(command, timeout=timeout)
        except TypeError:
            value = shell(command)
        if hasattr(value, "__await__"):
            value = await value
        return str(value or "")

    async def _adb_host(self, *args: str, timeout: float | None = None) -> tuple[int, bytes, bytes]:
        run_host = getattr(self._adb, "run_host", None)
        if callable(run_host):
            result = run_host(*args, timeout=timeout)
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, tuple) and len(result) == 3:
                return result
            raise MinitouchError("ADB run_host 返回值格式不正确")
        runner = getattr(self._adb, "_run_adb", None)
        adb_path = getattr(self._adb, "_adb_path", None)
        serial = getattr(self._adb, "serial", "")
        if callable(runner) and adb_path:
            argv = [str(adb_path), "-s", str(serial), *[str(arg) for arg in args]]
            try:
                result = await runner(argv, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - 统一转 MinitouchError
                raise MinitouchError(f"adb 主机命令失败：{exc}") from exc
            if isinstance(result, tuple) and len(result) == 3:
                return result
        raise MinitouchError("ADB 客户端不支持主机侧命令（push/forward）")

    async def _adb_host_ok(self, *args: str, timeout: float | None = None) -> str:
        rc, stdout, stderr = await self._adb_host(*args, timeout=timeout)
        if rc != 0:
            detail = (stderr or stdout).decode("utf-8", "replace").strip()
            raise MinitouchError(
                f"adb {' '.join(args)} 失败（退出码 {rc}）：{detail[:300]}"
            )
        return stdout.decode("utf-8", "replace").strip()

    # ------------------------------------------------------------------
    # 启动/停止
    # ------------------------------------------------------------------
    def _asyncio_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._asyncio_lock_obj is None or self._asyncio_lock_loop is not loop:
            self._asyncio_lock_obj = asyncio.Lock()
            self._asyncio_lock_loop = loop
        return self._asyncio_lock_obj

    async def start(self) -> None:
        """推送并启动 minitouch、建立 socket、读取 banner（幂等）。"""
        async with self._asyncio_lock():
            if self._started:
                return
            self._stopping = False
            try:
                await self._start_internal()
            except Exception as exc:  # noqa: BLE001 - 失败清理并统一异常
                await self._cleanup_resources()
                if isinstance(exc, MinitouchError):
                    raise
                raise MinitouchError(f"minitouch 启动失败：{exc}") from exc

    async def _start_internal(self) -> None:
        binary = self.binary_path
        if not binary.is_file():
            raise MinitouchError(f"minitouch binary 不存在：{binary}")
        await self._adb_ensure_ready()
        await self._query_device_size()
        await self._kill_stale_processes()
        await self._remove_stale_forwards()
        await self._adb_host_ok("push", str(binary), _REMOTE_BIN, timeout=120.0)
        await self._adb_shell(
            f"chmod 755 {_REMOTE_BIN}; rm -f {_REMOTE_LOG}", timeout=10.0
        )
        start_output = await self._adb_shell(
            f"{_REMOTE_BIN} < /dev/null > {_REMOTE_LOG} 2>&1 & echo PID=$!",
            timeout=30.0,
        )
        pid_match = re.search(r"PID=(\d+)", start_output)
        if pid_match:
            self._server_pid = int(pid_match.group(1))

        port = free_local_port()
        await self._adb_host_ok(
            "forward", f"tcp:{port}", f"localabstract:{DEFAULT_SOCKET_NAME}"
        )
        self._forward_port = port
        sock = await self._connect_socket(port)
        self._socket = sock
        banner = await self._read_banner(sock)
        self._banner = banner
        if banner.get("pid"):
            self._server_pid = int(banner["pid"])
        self._started = True
        self._log(
            "minitouch 已启动："
            f"max_contacts={banner.get('max_contacts')} "
            f"max_x={banner.get('max_x')} max_y={banner.get('max_y')} "
            f"max_pressure={banner.get('max_pressure')} pid={self._server_pid}"
        )

    async def _query_device_size(self) -> None:
        try:
            output = await self._adb_shell("wm size", timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - 取不到分辨率时启动后按需报错
            self._log(f"查询设备分辨率失败：{exc}")
            return
        width, height = parse_wm_size(output)
        if width > 0 and height > 0:
            self._device_size = (width, height)
            self._log(f"设备分辨率 {width}x{height}")

    async def _kill_stale_processes(self) -> None:
        try:
            output = await self._adb_shell("ps -A -o PID,ARGS", timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - 清理失败不阻塞启动
            self._log(f"查询残留 minitouch 进程失败：{exc}")
            return
        pids: list[int] = []
        for line in output.splitlines():
            stripped = line.strip()
            if _MINITOUCH_NEEDLE not in stripped:
                continue
            head = stripped.split(None, 1)[0] if stripped else ""
            if head.isdigit():
                pids.append(int(head))
        if not pids:
            return
        self._log(f"清理残留 minitouch 进程：{pids}")
        await self._adb_shell(
            "kill " + " ".join(str(pid) for pid in pids) + " 2>/dev/null; true",
            timeout=10.0,
        )
        await asyncio.sleep(0.2)

    async def _remove_stale_forwards(self) -> None:
        try:
            output = await self._adb_host_ok("forward", "--list", timeout=10.0)
        except MinitouchError as exc:
            self._log(f"查询 adb forward 失败：{exc}")
            return
        for _serial, local, remote in parse_forward_list(output):
            if remote != f"localabstract:{DEFAULT_SOCKET_NAME}":
                continue
            try:
                await self._adb_host_ok("forward", "--remove", local, timeout=10.0)
                self._log(f"已移除残留 minitouch forward：{local}")
            except MinitouchError as exc:
                self._log(f"移除残留 minitouch forward 失败：{exc}")

    async def _connect_socket(self, port: int) -> socket.socket:
        deadline = time.monotonic() + _START_TIMEOUT_S
        last_error = ""
        while time.monotonic() < deadline:
            try:
                return await asyncio.to_thread(
                    MinitouchClient._connect_tcp, port, _CONNECT_TIMEOUT_S
                )
            except OSError as exc:
                last_error = str(exc)
                await asyncio.sleep(0.2)
        raise MinitouchError(
            f"连接 minitouch socket 失败：{last_error or '超时'}"
        )

    @staticmethod
    def _connect_tcp(port: int, timeout: float) -> socket.socket:
        return socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)

    async def _read_banner(self, sock: socket.socket) -> dict[str, int]:
        try:
            sock.settimeout(_BANNER_TIMEOUT_S)
            buffer = b""
            deadline = time.monotonic() + _BANNER_TIMEOUT_S
            while time.monotonic() < deadline:
                chunk = await asyncio.to_thread(sock.recv, 4096)
                if not chunk:
                    break
                buffer += chunk
                text = buffer.decode("utf-8", "replace")
                if _BANNER_PID_RE.search(text) or text.count("\n") >= 3:
                    return parse_banner(text)
            if not buffer:
                raise MinitouchError("minitouch socket 未返回 banner")
            return parse_banner(buffer.decode("utf-8", "replace"))
        finally:
            try:
                sock.settimeout(None)
            except OSError:
                pass

    async def stop(self) -> None:
        """停止 minitouch（幂等）：关闭 socket、kill 进程、移除 forward、清理远端文件。"""
        async with self._asyncio_lock():
            await self._stop_internal()

    async def close(self) -> None:
        """``stop`` 的别名。"""
        await self.stop()

    async def _stop_internal(self) -> None:
        self._stopping = True
        self._close_socket()
        await self._remove_forward()
        await self._kill_server()
        await self._cleanup_remote_files()
        self._stopping = False
        self._started = False
        self._banner = {}
        self._log("minitouch 已停止")

    async def _cleanup_resources(self) -> None:
        self._stopping = True
        try:
            self._close_socket()
            await self._remove_forward()
            await self._kill_server()
            await self._cleanup_remote_files()
        finally:
            self._stopping = False
            self._started = False

    def _close_socket(self) -> None:
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    async def _remove_forward(self) -> None:
        port = self._forward_port
        self._forward_port = None
        if port is None:
            return
        try:
            await self._adb_host_ok("forward", "--remove", f"tcp:{port}", timeout=10.0)
            self._log(f"已移除 minitouch forward tcp:{port}")
        except MinitouchError as exc:
            self._log(f"移除 minitouch forward 失败：{exc}")

    async def _kill_server(self) -> None:
        pid = self._server_pid
        self._server_pid = None
        if pid is None:
            return
        try:
            await self._adb_shell(f"kill {int(pid)} 2>/dev/null; true", timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - 清理失败只记录
            self._log(f"kill minitouch（pid={pid}）失败：{exc}")
            return
        self._log(f"已 kill minitouch（pid={pid}）")

    async def _cleanup_remote_files(self) -> None:
        try:
            await self._adb_shell(f"rm -f {_REMOTE_BIN} {_REMOTE_LOG}", timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - 清理失败只记录
            self._log(f"清理远端 minitouch 文件失败：{exc}")

    # ------------------------------------------------------------------
    # 坐标换算
    # ------------------------------------------------------------------
    def _require_started(self) -> None:
        if not self._started or self._socket is None:
            raise MinitouchError("minitouch 未启动")

    def _device_size_for_control(self) -> tuple[int, int]:
        if self._device_size is None:
            raise MinitouchError("设备分辨率未知，无法换算 minitouch 坐标")
        return self._device_size

    def _touch_point(self, x: int, y: int) -> tuple[int, int]:
        """把设备像素坐标换算为 minitouch 坐标并校验范围。"""
        device_w, device_h = self._device_size_for_control()
        try:
            x_int, y_int = int(x), int(y)
        except (TypeError, ValueError) as exc:
            raise MinitouchError(f"坐标必须是整数：({x!r}, {y!r})") from exc
        if not (0 <= x_int < device_w and 0 <= y_int < device_h):
            raise MinitouchError(
                f"坐标越界：({x_int}, {y_int})，设备尺寸 {device_w}x{device_h}"
            )
        return (
            scale_axis(x_int, device_w, self.max_x),
            scale_axis(y_int, device_h, self.max_y),
        )

    def _pressure(self, ratio: float = DEFAULT_PRESSURE_RATIO) -> int:
        """按 banner 的 max_pressure 计算压力值。"""
        max_pressure = self.max_pressure
        if max_pressure <= 0:
            return 0
        return max(0, min(max_pressure, int(round(max_pressure * float(ratio)))))

    # ------------------------------------------------------------------
    # 协议命令
    # ------------------------------------------------------------------
    async def _write(self, payload: str) -> None:
        sock = self._socket
        if sock is None:
            raise MinitouchError("minitouch 未启动")

        def _send() -> None:
            with self._send_lock:
                sock.sendall(payload.encode("utf-8"))

        try:
            await asyncio.to_thread(_send)
        except OSError as exc:
            raise MinitouchError(f"minitouch socket 写入失败：{exc}") from exc

    async def _commit(self) -> None:
        await self._write("c\n")

    async def tap(self, x: int, y: int) -> None:
        """在设备像素坐标点击。"""
        self._require_started()
        px, py = self._touch_point(x, y)
        pressure = self._pressure()
        await self._write(f"d 0 {px} {py} {pressure}\n")
        await self._commit()
        await asyncio.sleep(0.02)
        await self._write("u 0\n")
        await self._commit()

    async def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        """长按设备像素坐标。"""
        self._require_started()
        duration = max(1, int(duration_ms))
        if duration > 60_000:
            raise MinitouchError(f"长按时长过长：{duration}ms")
        px, py = self._touch_point(x, y)
        pressure = self._pressure()
        await self._write(f"d 0 {px} {py} {pressure}\n")
        await self._commit()
        await asyncio.sleep(duration / 1000.0)
        await self._write("u 0\n")
        await self._commit()

    async def swipe(self, *args: Any, duration_ms: int = 400, **kwargs: Any) -> None:
        """滑动：支持 ``swipe(x1, y1, x2, y2, duration_ms=...)`` 或 ``swipe("up")``。"""
        if args and isinstance(args[0], str):
            await self.swipe_direction(args[0], duration_ms=duration_ms)
            return
        if len(args) == 4:
            x1, y1, x2, y2 = (int(value) for value in args)
        elif len(args) == 1 and isinstance(args[0], (tuple, list)) and len(args[0]) == 4:
            x1, y1, x2, y2 = (int(value) for value in args[0])
        else:
            x1 = int(kwargs["x1"])
            y1 = int(kwargs["y1"])
            x2 = int(kwargs["x2"])
            y2 = int(kwargs["y2"])
        duration = max(1, int(duration_ms))
        if duration > 60_000:
            raise MinitouchError(f"滑动时长过长：{duration}ms")
        await self._swipe_points(x1, y1, x2, y2, duration)

    async def swipe_direction(
        self,
        direction: str,
        *,
        duration_ms: int = 400,
        ratio: float = 0.6,
    ) -> None:
        """按方向（up/down/left/right）在屏幕中部滑动。"""
        name = str(direction or "").strip().lower()
        if name not in ("up", "down", "left", "right"):
            raise MinitouchError(
                f"滑动方向非法：{direction!r}（可选 up/down/left/right）"
            )
        ratio_value = float(ratio)
        if not 0 < ratio_value <= 1:
            raise MinitouchError(f"ratio 必须在 (0, 1] 区间内：{ratio}")
        self._require_started()
        width, height = self._device_size_for_control()
        center_x, center_y = width // 2, height // 2
        delta_x = int(width * ratio_value / 2)
        delta_y = int(height * ratio_value / 2)
        if name == "up":
            await self._swipe_points(
                center_x, min(height - 1, center_y + delta_y),
                center_x, max(0, center_y - delta_y), duration_ms,
            )
        elif name == "down":
            await self._swipe_points(
                center_x, max(0, center_y - delta_y),
                center_x, min(height - 1, center_y + delta_y), duration_ms,
            )
        elif name == "left":
            await self._swipe_points(
                min(width - 1, center_x + delta_x), center_y,
                max(0, center_x - delta_x), center_y, duration_ms,
            )
        else:
            await self._swipe_points(
                max(0, center_x - delta_x), center_y,
                min(width - 1, center_x + delta_x), center_y, duration_ms,
            )

    async def _swipe_points(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int
    ) -> None:
        duration = max(1, int(duration_ms))
        start_x, start_y = self._touch_point(x1, y1)
        end_x, end_y = self._touch_point(x2, y2)
        pressure = self._pressure()
        await self._write(f"d 0 {start_x} {start_y} {pressure}\n")
        await self._commit()
        steps = max(2, min(60, int(round(duration / 16.0))))
        delay = duration / steps / 1000.0
        for index in range(1, steps + 1):
            ratio = index / steps
            px = int(round(start_x + (end_x - start_x) * ratio))
            py = int(round(start_y + (end_y - start_y) * ratio))
            await asyncio.sleep(delay)
            await self._write(f"m 0 {px} {py} {pressure}\n")
            await self._commit()
        await asyncio.sleep(delay)
        await self._write("u 0\n")
        await self._commit()
