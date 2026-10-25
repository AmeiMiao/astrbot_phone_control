"""控制/感知后端抽象：ADB 兜底，可选 scrcpy（控制+低延迟镜像）与 minitouch（touch）。

设计目标：

* **不引入额外强依赖**：scrcpy 模块（``core.scrcpy``）可选，模块不存在或
  启动失败时自动回退 ADB，并只告警一次、记录 ``last_error``；
* **连接维护**：常驻开关开启时预热并保持所选通道；关闭时按
  ``control.scrcpy.idle_stop_seconds`` 空闲停止，停止和卸载显式收尾；
* **视频按需**：启动时由 ``perception.screen_source`` 决定视频通道——
  ``adb`` 只开控制通道（server ``video=false``，无 H264 编码/解码），
  ``auto``/``scrcpy`` 才启用视频镜像；仅控制通道运行期间若配置改为需要视频，
  停止后带视频重启一次（有一次性 guard，失败回退 ADB，不会循环重启）；
* **纯标准库**：不导入 astrbot，适配器只依赖鸭子类型对象，便于本地单测。

协议约定（``core.scrcpy.ScrcpyClient``）：

* ``start()/stop()``（同步或异步均可）、``capabilities() -> dict``；
  可选 ``video`` 构造参数 / ``set_video_enabled(bool)`` / ``video`` 属性用于开关视频通道
* ``latest_frame(max_age_ms=0) -> bytes | (data, format, meta) | dict | 带 data/ts 的对象``
* ``tap(x, y)``、``long_press(x, y, duration_ms=800)``、``swipe(x1, y1, x2, y2, duration_ms=...)``
* ``inject_text(text)``、``press_key(key)``、``back()``、``get_device_size() -> (w, h)``

所有调用都做了签名兼容（TypeError 时退化）与失败回退，scrcpy 客户端接口小改不需要动本模块。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

SCRCPY_VERSION = "v5.0.1"
MINITOUCH_VERSION = "v1.3.2"

LogFn = Callable[[str], None]


class BackendError(Exception):
    """后端不可用/调用失败（管理器内部使用，向上回退 ADB）。"""


# ----------------------------------------------------------------------
# 结果归一化
# ----------------------------------------------------------------------


async def _await_maybe(value: Any) -> Any:
    """如果 value 是 awaitable 则 await，否则原样返回（用于可选异步钩子）。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _read_bytes_if_path(value: Any) -> bytes | None:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, (str, Path)):
        try:
            return Path(value).read_bytes()
        except OSError:
            return None
    return None


def _as_capabilities(value: Any) -> dict[str, Any]:
    """把 dict/对象/dataclass 能力快照统一成 dict。"""
    if isinstance(value, dict):
        return dict(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return {}
    caps: dict[str, Any] = {}
    for key in ("video", "control", "decoder", "audio", "server_jar", "minitouch"):
        if hasattr(value, key):
            item = getattr(value, key)
            caps[key] = bool(item) if key in {"video", "control", "decoder", "audio", "minitouch"} else item
    if not caps:
        try:
            caps.update(dict(vars(value)))
        except TypeError:
            return {}
    return caps


def _client_capabilities_sync(client: Any) -> dict[str, Any]:
    """读取客户端的 capabilities（属性或方法/同步）。"""
    value = getattr(client, "capabilities", None)
    if value is None:
        return {}
    try:
        if callable(value):
            value = value()
        if inspect.isawaitable(value):
            value = None
    except Exception:  # noqa: BLE001 - 能力读取失败按未知处理
        return {}
    return _as_capabilities(value)


async def _client_capabilities(client: Any) -> dict[str, Any]:
    """读取客户端的 capabilities（属性或同步/异步方法）。"""
    value = getattr(client, "capabilities", None)
    if value is None:
        return {}
    try:
        if callable(value):
            value = value()
        if inspect.isawaitable(value):
            value = await value
    except Exception:  # noqa: BLE001 - 能力读取失败按未知处理
        return {}
    return _as_capabilities(value)


def _normalize_screenshot_result(
    result: Any, fallback_path: Path
) -> tuple[Path, dict[str, Any]]:
    """归一化截图返回：Path/str/tuple/dict/带属性对象。"""
    meta: dict[str, Any] = {}
    path: Any = None
    if result is None:
        path = fallback_path
    elif isinstance(result, Path):
        path = result
    elif isinstance(result, str):
        path = result
    elif isinstance(result, tuple):
        path = result[0] if result else fallback_path
        if len(result) > 1:
            if isinstance(result[1], dict):
                meta.update(result[1])
            elif isinstance(result[1], (int, float)):
                meta["elapsed_ms"] = float(result[1])
        if len(result) > 2:
            extra = result[2]
            if isinstance(extra, dict):
                meta.update(extra)
    elif isinstance(result, dict):
        path = result.get("path") or fallback_path
        meta.update({k: v for k, v in result.items() if k != "path"})
    else:
        path = getattr(result, "path", None) or fallback_path
        for key in (
            "method",
            "duration_ms",
            "elapsed_ms",
            "fast",
            "mismatch",
            "retries",
            "format",
        ):
            value = getattr(result, key, None)
            if value is not None:
                meta.setdefault(key, value)
    resolved = Path(path) if path else fallback_path
    if resolved.exists():
        meta.setdefault("method", "jpeg" if resolved.suffix.lower() in {".jpg", ".jpeg"} else "png")
    return resolved, meta


def _normalize_frame(frame: Any) -> tuple[bytes, str, dict[str, Any]] | None:
    """把 ``latest_frame`` 返回统一成 ``(bytes, format, meta)``。"""
    if frame is None:
        return None
    fmt = ""
    meta: dict[str, Any] = {}
    data: bytes | None = None
    if isinstance(frame, (bytes, bytearray)):
        data = bytes(frame)
    elif isinstance(frame, (str, Path)):
        data = _read_bytes_if_path(frame)
        fmt = "jpeg" if str(frame).lower().endswith((".jpg", ".jpeg")) else "png"
    elif isinstance(frame, tuple):
        if frame:
            data = _read_bytes_if_path(frame[0])
        if len(frame) > 1 and isinstance(frame[1], str):
            fmt = frame[1]
        if len(frame) > 2 and isinstance(frame[2], dict):
            meta.update(frame[2])
    elif isinstance(frame, dict):
        meta.update(frame)
        data = _read_bytes_if_path(
            frame.get("data") or frame.get("bytes") or frame.get("jpeg") or frame.get("png")
        )
        path = frame.get("path")
        if data is None and path:
            data = _read_bytes_if_path(path)
        fmt = str(frame.get("format") or frame.get("method") or "")
    else:
        for key in (
            "data",
            "bytes",
            "jpeg",
            "jpeg_bytes",
            "jpeg_data",
            "png",
            "image",
            "image_bytes",
            "frame",
        ):
            data = _read_bytes_if_path(getattr(frame, key, None))
            if data is not None:
                break
        if data is None:
            data = _read_bytes_if_path(getattr(frame, "path", None))
        for key in ("format", "method", "image_format"):
            value = getattr(frame, key, None)
            if value:
                fmt = str(value)
                break
        for key in ("width", "w"):
            value = getattr(frame, key, None)
            if isinstance(value, int):
                meta.setdefault("width", value)
                break
        for key in ("height", "h"):
            value = getattr(frame, key, None)
            if isinstance(value, int):
                meta.setdefault("height", value)
                break
        for key in ("ts", "timestamp", "created_at", "time"):
            value = getattr(frame, key, None)
            if isinstance(value, (int, float)):
                meta.setdefault("ts", float(value))
                break
        age_ms = getattr(frame, "age_ms", None)
        if callable(age_ms):
            try:
                meta.setdefault("age_ms", float(age_ms()))
            except Exception:  # noqa: BLE001
                pass
    if not data:
        return None
    if not fmt:
        fmt = "jpeg" if data[:3] == b"\xff\xd8\xff" else "png"
    fmt = "jpeg" if fmt.lower() in {"jpg", "jpeg"} else "png"
    meta.setdefault("source", "scrcpy")
    return data, fmt, meta


# ----------------------------------------------------------------------
# 控制后端
# ----------------------------------------------------------------------


class ControlBackend(Protocol):
    """控制后端协议。"""

    name: str

    async def tap(self, x: int, y: int) -> None: ...

    async def long_press(self, x: int, y: int, *, duration_ms: int = 800) -> None: ...

    async def swipe(
        self, direction: str, *, duration_ms: int = 800
    ) -> None: ...

    async def input_text(self, text: str, *, press_enter: bool = False) -> None: ...

    async def press_key(self, key: str) -> None: ...

    async def back(self) -> None: ...

    def capabilities(self) -> dict[str, Any]: ...


class AdbControlBackend:
    """基于现有 DeviceActions 的 ADB 控制（兜底实现）。"""

    name = "adb"

    def __init__(self, actions: Any) -> None:
        self.actions = actions

    async def tap(self, x: int, y: int) -> None:
        await self.actions.tap(x, y)

    async def long_press(
        self, x: int, y: int, *, duration_ms: int = 800
    ) -> None:
        await self.actions.long_press(x, y, duration_ms=duration_ms)

    async def swipe(self, direction: str, *, duration_ms: int = 800) -> None:
        await self.actions.swipe_direction(direction, duration_ms=duration_ms)

    async def input_text(self, text: str, *, press_enter: bool = False) -> None:
        await self.actions.input_text(text, press_enter=press_enter)

    async def press_key(self, key: str) -> None:
        await self.actions.press_key(key)

    async def back(self) -> None:
        await self.actions.press_key("back")

    def capabilities(self) -> dict[str, Any]:
        return {
            "control": "adb",
            "tap": True,
            "long_press": True,
            "swipe": True,
            "text": True,
            "key": True,
            "minitouch": False,
        }


def _is_ascii(text: str) -> bool:
    try:
        return all(ord(char) < 128 for char in str(text or ""))
    except TypeError:
        return False


class ScrcpyControlBackend:
    """基于 ``core.scrcpy.ScrcpyClient`` 的低延迟控制；失败回退 ADB。"""

    name = "scrcpy"

    def __init__(
        self,
        client: Any,
        fallback: AdbControlBackend,
        *,
        log: LogFn | None = None,
        on_result: Callable[[str], None] | None = None,
    ) -> None:
        self._on_result = on_result or (lambda source: None)
        self.client = client
        self.fallback = fallback
        self._log = log or (lambda message: None)
        self._warned: set[str] = set()

    def _warn_once(self, key: str, message: str) -> None:
        self._on_result("ADBKeyboard" if key == "input_text" else "adb")
        if key in self._warned:
            return
        self._warned.add(key)
        self._log(message)

    async def _invoke(self, method: str, *args: Any, **kwargs: Any) -> Any:
        func = getattr(self.client, method, None)
        if func is None:
            raise BackendError(f"scrcpy 客户端不支持 {method}")
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        self._on_result("scrcpy")
        return result

    async def tap(self, x: int, y: int) -> None:
        try:
            await self._invoke("tap", x, y)
        except Exception as exc:  # noqa: BLE001 - 失败回退 ADB
            self._warn_once("tap", f"scrcpy tap 失败，回退 ADB：{exc}")
            await self.fallback.tap(x, y)

    async def long_press(
        self, x: int, y: int, *, duration_ms: int = 800
    ) -> None:
        try:
            try:
                await self._invoke(
                    "long_press", x, y, duration_ms=duration_ms
                )
            except TypeError:
                await self._invoke("long_press", x, y)
        except Exception as exc:  # noqa: BLE001
            self._warn_once("long_press", f"scrcpy long_press 失败，回退 ADB：{exc}")
            await self.fallback.long_press(x, y, duration_ms=duration_ms)

    async def _device_size(self) -> tuple[int, int]:
        try:
            size = await self._invoke("get_device_size")
        except Exception:  # noqa: BLE001 - 取不到尺寸时用兜底
            size = None
        if isinstance(size, dict):
            width = int(size.get("width") or size.get("w") or 0)
            height = int(size.get("height") or size.get("h") or 0)
            return width, height
        if isinstance(size, (tuple, list)) and len(size) >= 2:
            return int(size[0]), int(size[1])
        return 0, 0

    async def swipe(self, direction: str, *, duration_ms: int = 800) -> None:
        name = str(direction or "up").lower()
        try:
            # scrcpy 客户端若直接提供方向滑动则优先使用
            direct = getattr(self.client, "swipe_direction", None)
            if direct is not None:
                result = direct(name, duration_ms=duration_ms)
                if inspect.isawaitable(result):
                    await result
                return
            width, height = await self._device_size()
            if width <= 0 or height <= 0:
                raise BackendError("scrcpy 未提供设备尺寸，无法坐标滑动")
            cx, cy = width // 2, height // 2
            points = {
                "up": (cx, int(height * 0.75), cx, int(height * 0.3)),
                "down": (cx, int(height * 0.3), cx, int(height * 0.75)),
                "left": (int(width * 0.8), cy, int(width * 0.2), cy),
                "right": (int(width * 0.2), cy, int(width * 0.8), cy),
            }
            x1, y1, x2, y2 = points.get(name, points["up"])
            try:
                await self._invoke(
                    "swipe", x1, y1, x2, y2, duration_ms=duration_ms
                )
            except TypeError:
                await self._invoke("swipe", x1, y1, x2, y2)
        except Exception as exc:  # noqa: BLE001
            self._warn_once("swipe", f"scrcpy swipe 失败，回退 ADB：{exc}")
            await self.fallback.swipe(name, duration_ms=duration_ms)

    async def input_text(self, text: str, *, press_enter: bool = False) -> None:
        # 中文/非 ASCII 或 scrcpy 输入失败 → ADBKeyboard；字母数字优先低延迟注入
        if not _is_ascii(text):
            self._on_result("ADBKeyboard")
            await self.fallback.input_text(text, press_enter=press_enter)
            return
        try:
            accepted = await self._invoke("inject_text", text)
            if accepted is False:
                raise BackendError("scrcpy inject_text 返回 False")
            if press_enter:
                await self.press_key("enter")
        except Exception as exc:  # noqa: BLE001
            self._warn_once(
                "input_text", f"scrcpy inject_text 失败，回退 ADBKeyboard：{exc}"
            )
            await self.fallback.input_text(text, press_enter=press_enter)

    async def press_key(self, key: str) -> None:
        try:
            await self._invoke("press_key", key)
        except Exception as exc:  # noqa: BLE001
            self._warn_once("press_key", f"scrcpy press_key 失败，回退 ADB：{exc}")
            await self.fallback.press_key(key)

    async def back(self) -> None:
        try:
            func = getattr(self.client, "back", None)
            if func is None:
                raise BackendError("scrcpy 客户端不支持 back")
            result = func()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001
            self._warn_once("back", f"scrcpy back 失败，回退 ADB：{exc}")
            await self.fallback.press_key("back")

    def capabilities(self) -> dict[str, Any]:
        caps = _client_capabilities_sync(self.client)
        base = {
            "control": "scrcpy",
            "tap": True,
            "long_press": True,
            "swipe": True,
            "text": True,
            "key": True,
        }
        base.update(caps)
        base["control"] = "scrcpy"
        return base


class MinitouchControlBackend:
    """可选 minitouch touch 后端；文字/按键回退 ADB。"""

    name = "minitouch"

    def __init__(
        self,
        client: Any,
        fallback: AdbControlBackend,
        *,
        log: LogFn | None = None,
    ) -> None:
        self.client = client
        self.fallback = fallback
        self._log = log or (lambda message: None)
        self._warned: set[str] = set()

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self._log(message)

    async def _touch(self, method: str, *args: Any, **kwargs: Any) -> bool:
        func = getattr(self.client, method, None)
        if func is None:
            return False
        try:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:  # noqa: BLE001
            self._warn_once(method, f"minitouch {method} 失败，回退 ADB：{exc}")
            return False

    async def tap(self, x: int, y: int) -> None:
        if not await self._touch("tap", x, y):
            await self.fallback.tap(x, y)

    async def long_press(
        self, x: int, y: int, *, duration_ms: int = 800
    ) -> None:
        if not await self._touch("long_press", x, y, duration_ms=duration_ms):
            await self.fallback.long_press(x, y, duration_ms=duration_ms)

    async def swipe(self, direction: str, *, duration_ms: int = 800) -> None:
        if not await self._touch(
            "swipe", direction, duration_ms=duration_ms
        ):
            await self.fallback.swipe(direction, duration_ms=duration_ms)

    async def input_text(self, text: str, *, press_enter: bool = False) -> None:
        await self.fallback.input_text(text, press_enter=press_enter)

    async def press_key(self, key: str) -> None:
        await self.fallback.press_key(key)

    async def back(self) -> None:
        await self.fallback.press_key("back")

    def capabilities(self) -> dict[str, Any]:
        return {
            "control": "minitouch",
            "tap": True,
            "long_press": True,
            "swipe": True,
            "text": False,
            "key": False,
            "minitouch": True,
        }


# ----------------------------------------------------------------------
# 感知后端
# ----------------------------------------------------------------------


class PerceptionBackend(Protocol):
    """感知后端协议：截图返回 ``(bytes, format, meta)``。"""

    name: str

    async def screenshot(
        self, *, fast: bool = False, expected_package: str = ""
    ) -> tuple[bytes, str, dict[str, Any]]: ...


class AdbPerceptionBackend:
    """ADB screencap（现有 Q 截图能力）作为兜底感知后端。"""

    name = "adb"

    def __init__(self, actions: Any, output_dir: Path) -> None:
        self.actions = actions
        self.output_dir = Path(output_dir)

    async def screenshot(
        self, *, fast: bool = False, expected_package: str = ""
    ) -> tuple[bytes, str, dict[str, Any]]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"frame-{int(time.time() * 1000)}.png"
        started = time.monotonic()
        try:
            result = await self.actions.screenshot(
                path,
                expected_package=expected_package or None,
                fast=bool(fast),
            )
        except TypeError:
            result = await self.actions.screenshot(path)
        resolved, meta = _normalize_screenshot_result(result, path)
        try:
            data = Path(resolved).read_bytes()
        except OSError as exc:
            raise BackendError(f"读取 ADB 截图失败：{resolved}（{exc}）") from exc
        fmt = (
            "jpeg"
            if str(meta.get("method") or meta.get("format") or "").lower()
            in {"jpeg", "jpg"}
            or Path(resolved).suffix.lower() in {".jpg", ".jpeg"}
            else "png"
        )
        meta.setdefault("source", "adb")
        meta.setdefault("method", fmt)
        meta.setdefault("format", fmt)
        meta.setdefault(
            "elapsed_ms", round((time.monotonic() - started) * 1000, 1)
        )
        return data, fmt, meta


class ScrcpyPerceptionBackend:
    """scrcpy 视频最新帧；过期/不可用时回退 ADB screencap。"""

    name = "scrcpy"

    def __init__(
        self,
        client: Any,
        fallback: AdbPerceptionBackend,
        *,
        fresh_ms: int = 1200,
        clock: Callable[[], float] = time.time,
        log: LogFn | None = None,
        on_fallback: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.fallback = fallback
        self.fresh_ms = max(100, int(fresh_ms))
        self._clock = clock
        self._log = log or (lambda message: None)
        self._on_fallback = on_fallback
        self._warned: set[str] = set()

    def _fallback(self, reason: str, *, log: bool = False) -> None:
        """记录回退原因（静默，不向用户报错）；同一原因只记一次日志。"""
        if log:
            self._warn_once(f"fallback:{reason}", f"实时帧回退 ADB：{reason}")
        if self._on_fallback is not None:
            with suppress(Exception):
                self._on_fallback(reason)

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self._log(message)

    def _video_available(self) -> bool | None:
        """按对外契约检查 capabilities.video；None 表示未知（按可用处理）。"""
        caps = _client_capabilities_sync(self.client)
        if "video" not in caps:
            return None
        return bool(caps.get("video"))

    async def _latest_frame(self, max_age_ms: int) -> tuple[bytes, str, dict] | None:
        video = self._video_available()
        if video is False:
            self._fallback("scrcpy video 能力不可用")
            return None
        func = getattr(self.client, "latest_frame", None)
        if func is None:
            self._fallback("scrcpy 客户端不支持 latest_frame")
            return None
        try:
            try:
                frame = func(max_age_ms=max_age_ms)
            except TypeError:
                frame = func()
            if inspect.isawaitable(frame):
                frame = await frame
        except Exception as exc:  # noqa: BLE001 - 视频帧失败静默回退 ADB
            reason = f"scrcpy 取帧失败：{exc}"
            client_error = getattr(self.client, "last_error", "") or ""
            if client_error:
                reason = f"{reason}（{client_error}）"
            self._fallback(reason, log=True)
            return None
        if frame is None:
            self._fallback("scrcpy 暂无可用视频帧")
            return None
        normalized = _normalize_frame(frame)
        if normalized is None:
            self._fallback("scrcpy 帧数据不可用")
            return None
        data, fmt, meta = normalized
        ts = meta.get("ts")
        if isinstance(ts, (int, float)):
            age = max(0.0, self._clock() - float(ts))
            meta["age_ms"] = round(age * 1000, 1)
            if age * 1000 > max_age_ms:
                self._fallback("scrcpy 最新帧已过期")
                return None
        return data, fmt, meta

    async def screenshot(
        self, *, fast: bool = False, expected_package: str = ""
    ) -> tuple[bytes, str, dict[str, Any]]:
        frame = await self._latest_frame(self.fresh_ms)
        if frame is None:
            refresh = getattr(self.client, "request_frame", None)
            if callable(refresh):
                try:
                    await _await_maybe(refresh())
                except Exception as exc:
                    self._fallback(f"scrcpy 请求新帧失败：{exc}", log=True)
            deadline = time.monotonic() + 2.0
            while frame is None and time.monotonic() < deadline:
                await asyncio.sleep(.05)
                frame = await self._latest_frame(self.fresh_ms)
        if frame is not None:
            data, fmt, meta = frame
            expected = str(expected_package or "").strip()
            if expected:
                # 前台包一致性：scrcpy 帧本身不带包名，用一次轻量 ADB 前台查询校验；
                # 不一致或查询失败时回退 ADB 截图，保证调用方拿到的画面与期望 App 一致。
                actual = ""
                try:
                    actual, _activity = await self.fallback.actions.foreground()
                except Exception:  # noqa: BLE001 - 查询失败按不一致处理
                    actual = ""
                if not actual or str(actual) != expected:
                    self._fallback("scrcpy 帧前台包不一致")
                else:
                    meta.setdefault("elapsed_ms", 0.0)
                    meta["expected_package"] = expected
                    return data, fmt, meta
            else:
                meta.setdefault("elapsed_ms", 0.0)
                return data, fmt, meta
        return await self.fallback.screenshot(
            fast=fast, expected_package=expected_package
        )


# ----------------------------------------------------------------------
# 后端管理器
# ----------------------------------------------------------------------


@dataclass
class BackendStatus:
    """后端状态快照。"""

    control: str = "adb"
    perception: str = "adb"
    scrcpy_running: bool = False
    scrcpy_video: bool | None = None
    # 仅控制通道（screen_source=adb，视频通道未启用；非能力失败）
    scrcpy_video_disabled: bool = False
    minitouch_enabled: bool = False
    last_error: str = ""
    fallback_reason: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)

    def lines(self) -> list[str]:
        control = self.control
        if self.scrcpy_running:
            control = f"{control}（scrcpy 已连接）"
        elif self.last_error:
            control = f"{control}（ADB 兜底：{self.last_error}）"
        lines = [
            f"控制后端：{control}",
            f"感知后端：{self.perception}",
        ]
        if self.scrcpy_running:
            if self.scrcpy_video_disabled:
                lines.append("scrcpy video：未启用（仅控制通道，无 H264 编码/解码）")
            elif self.scrcpy_video is False:
                reason = self.fallback_reason or self.last_error or "video 能力不可用"
                lines.append(f"scrcpy video：不可用（{reason}）")
            elif self.scrcpy_video:
                lines.append("scrcpy video：可用")
            else:
                lines.append("scrcpy video：未知")
        else:
            lines.append("scrcpy video：未启动（按需懒启动）")
        lines.append(f"minitouch：{'已启用' if self.minitouch_enabled else '未启用'}")
        if self.capabilities:
            keys = [
                key
                for key, value in self.capabilities.items()
                if value is True
            ]
            if keys:
                lines.append(f"能力：{'、'.join(keys)}")
        return lines


class BackendManager:
    """懒启动 scrcpy、失败回退 ADB、空闲自动停止的控制/感知后端管理器。"""

    def __init__(
        self,
        *,
        config_provider: Callable[[], Any],
        actions: Any,
        adb: Any,
        data_dir: str | Path,
        adb_path: str | Path | None = None,
        scrcpy_factory: Callable[[], Awaitable[Any] | Any] | None = None,
        minitouch_factory: Callable[[str], Awaitable[Any] | Any] | None = None,
        log: LogFn | None = None,
        warn: LogFn | None = None,
        on_started: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config_provider = config_provider
        self._actions = actions
        self._adb = adb
        self._adb_path = adb_path
        self.data_dir = Path(data_dir)
        self._scrcpy_factory = scrcpy_factory
        self._minitouch_factory = minitouch_factory
        self._log = log or (lambda message: None)
        self._warn = warn or (lambda message: None)
        self._on_started = on_started
        self._clock = clock
        self._adb_control = AdbControlBackend(actions)
        self._adb_perception = AdbPerceptionBackend(
            actions, self.data_dir / "screenshots"
        )
        self.control_backend: ControlBackend = self._adb_control
        self.perception_backend: PerceptionBackend = self._adb_perception
        self._scrcpy_client: Any = None
        self._minitouch_client: Any = None
        self._lifecycle_lock = asyncio.Lock()
        self._retry_at = 0.0
        self._scrcpy_unavailable = False
        self._scrcpy_error_logged = False
        self._scrcpy_caps: dict[str, Any] = {}
        self._scrcpy_last_error = ""
        # 本次 scrcpy 会话请求的视频模式：True=视频+控制，False=仅控制，None=未运行
        self._scrcpy_want_video: bool | None = None
        # 仅控制通道运行后因感知需要视频而重启的一次性 guard
        self._video_restart_attempted = False
        # scrcpy 工厂是否接受 video 关键字参数（首次调用时探测）
        self._factory_video_kwarg: bool | None = None
        self.fallback_reason = ""
        self._closed = False
        self.recoveries = 0
        self.fallback_count = 0
        self.last_success_at = 0.0
        self.last_control_source = "未执行"
        self.last_capture_source = "未执行"
        self.last_error = ""
        self._started_at = 0.0
        self._last_activity = 0.0
        self._warned: set[str] = set()

    # ---- 配置 ---------------------------------------------------------

    def _config(self) -> Any:
        try:
            return self._config_provider()
        except Exception:  # noqa: BLE001 - 配置异常按 ADB 兜底
            return None

    def _config_sections(self) -> tuple[Any, Any, Any]:
        cfg = self._config()
        control = getattr(cfg, "control", None)
        scrcpy = getattr(control, "scrcpy", None)
        perception = getattr(cfg, "perception", None)
        return control, scrcpy, perception

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self._warn(message)

    # ---- 状态 ---------------------------------------------------------

    @property
    def scrcpy_running(self) -> bool:
        return self._scrcpy_client is not None

    def status(self) -> BackendStatus:
        caps = dict(self._scrcpy_caps)
        if self._scrcpy_client is not None:
            live = _client_capabilities_sync(self._scrcpy_client)
            if live:
                caps.update(live)
            if (
                caps.get("video") is True
                and self.fallback_reason == "scrcpy video 能力不可用"
            ):
                self.fallback_reason = ""
            # 能力变化后（如首帧解码完成）让状态反映实际会使用的后端
            if caps:
                self._scrcpy_caps.update(caps)
                self._apply_backends()
        running = self._scrcpy_client is not None
        control_only = running and self._scrcpy_want_video is False
        if running:
            scrcpy_video: bool | None = bool(caps.get("video", True))
        else:
            scrcpy_video = False if self.last_error else None
        return BackendStatus(
            control=getattr(self.control_backend, "name", "adb"),
            perception=getattr(self.perception_backend, "name", "adb"),
            scrcpy_running=running,
            scrcpy_video=scrcpy_video,
            scrcpy_video_disabled=control_only,
            minitouch_enabled=self._minitouch_client is not None,
            last_error=self.last_error,
            fallback_reason=self.fallback_reason,
            capabilities=(
                caps
                if running
                else (
                    self.control_backend.capabilities()
                    if hasattr(self.control_backend, "capabilities")
                    else {}
                )
            ),
        )

    def record_capture(self, source: str) -> None:
        self.last_capture_source = source
        if source == "scrcpy":
            self.last_success_at = time.time()
            self.fallback_reason = ""
        else:
            _control, _scrcpy, perception = self._config_sections()
            if self._wants_scrcpy_video(perception):
                self.fallback_count += 1
                self._warn(f"截图实际回退 ADB：{self.fallback_reason or self.last_error or '视频尚未就绪'}")

    def _control_result(self, source: str) -> None:
        self.last_control_source = source
        if source == "scrcpy":
            self.last_success_at = time.time()

    def status_lines(self) -> list[str]:
        control, scrcpy, perception = self._config_sections()
        return self.status().lines() + [
            f"配置后端：控制={getattr(control, 'backend', 'auto')}；截图={self._screen_source(perception)}",
            f"常驻={bool(getattr(scrcpy, 'keep_alive', True))}；恢复={self.recoveries}；感知回退={self.fallback_count}",
            f"最近实际来源：控制={self.last_control_source}；截图={self.last_capture_source}；scrcpy最近成功={self.last_success_at:.0f}",
        ]

    # ---- 生命周期 -----------------------------------------------------

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _factory_accepts_video(factory: Any) -> bool:
        """scrcpy 工厂是否接受 video 关键字参数（不接受时按无参调用）。"""
        try:
            parameters = inspect.signature(factory).parameters
        except (TypeError, ValueError):
            return False
        return "video" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    async def _call_scrcpy_factory(self, video: bool) -> Any:
        if self._scrcpy_factory is None:
            raise BackendError("scrcpy 客户端未配置（等待依赖模块/插件内置 server）")
        if self._factory_video_kwarg is None:
            self._factory_video_kwarg = self._factory_accepts_video(
                self._scrcpy_factory
            )
        if self._factory_video_kwarg:
            return await self._maybe_await(self._scrcpy_factory(video=video))
        return await self._maybe_await(self._scrcpy_factory())

    @staticmethod
    async def _set_client_video(client: Any, video: bool) -> bool:
        """在 start() 前把 video 开关应用到客户端（兼容 set_video_enabled/属性）。"""
        setter = getattr(client, "set_video_enabled", None)
        if callable(setter):
            await _await_maybe(setter(bool(video)))
            return True
        if hasattr(client, "video"):
            try:
                setattr(client, "video", bool(video))
                return True
            except Exception:  # noqa: BLE001 - 只读属性等无法设置
                return False
        return False

    async def _start_scrcpy(self, *, video: bool) -> Any:
        if self._scrcpy_factory is None:
            raise BackendError("scrcpy 客户端未配置（等待依赖模块/插件内置 server）")
        client = await self._call_scrcpy_factory(video)
        if client is None:
            raise BackendError("scrcpy 客户端构造失败")
        applied = await self._set_client_video(client, video)
        if not applied:
            self._warn_once(
                "scrcpy_video_switch",
                "scrcpy 客户端不支持视频开关，按客户端默认（视频开启）启动",
            )
        start = getattr(client, "start", None)
        if callable(start):
            await self._maybe_await(start())
        return client

    @staticmethod
    def _screen_source(perception: Any) -> str:
        return str(getattr(perception, "screen_source", "adb") or "adb").lower()

    @classmethod
    def _wants_scrcpy_video(cls, perception: Any) -> bool:
        """是否启用视频镜像。

        - screen_source=scrcpy：始终启用视频；
        - screen_source=auto：仅当「任务期间持续截图」（perception.live_screen_enabled）
          开启时才启用视频；关闭时感知走 ADB 截图，scrcpy 只开控制通道（省掉常驻
          H264 编码/解码）；
        - screen_source=adb：不启用视频。
        """
        source = cls._screen_source(perception)
        if source == "scrcpy":
            return True
        if source == "auto":
            return bool(getattr(perception, "live_screen_enabled", True))
        return False

    async def _ensure_scrcpy(
        self, *, need_control: bool, need_perception: bool, for_clipboard: bool = False
    ) -> None:
        if getattr(self._adb, "app_mode", False):
            return
        if self._closed or self._scrcpy_client is not None:
            return
        if not for_clipboard and self._scrcpy_unavailable and self._clock() < self._retry_at:
            return
        self._scrcpy_unavailable = False
        control, _scrcpy, perception = self._config_sections()
        backend_choice = str(getattr(control, "backend", "auto") or "auto")
        screen_source = self._screen_source(perception)
        wants = (
            need_control and backend_choice in {"auto", "scrcpy"}
        ) or (
            need_perception and screen_source in {"auto", "scrcpy"}
        )
        if not wants and not for_clipboard:
            return
        video = False if for_clipboard else self._wants_scrcpy_video(perception)
        try:
            client = await self._start_scrcpy(video=video)
        except Exception as exc:  # noqa: BLE001 - 失败回退 ADB，只告警一次
            self._scrcpy_unavailable = True
            self._retry_at = self._clock() + 30
            self.last_error = str(exc) or exc.__class__.__name__
            if not self._scrcpy_error_logged:
                self._scrcpy_error_logged = True
                self._warn(f"scrcpy 不可用，已回退 ADB（{self.last_error}）")
            return
        self.last_error = ""
        self._retry_at = 0.0
        self._scrcpy_error_logged = False
        self._scrcpy_client = client
        self._scrcpy_want_video = video
        self._video_restart_attempted = False
        self._scrcpy_caps = await _client_capabilities(client)
        self._scrcpy_last_error = str(getattr(client, "last_error", "") or "")
        self._started_at = self._clock()
        self._last_activity = self._started_at
        await self._try_minitouch(control)
        self._apply_backends()
        self._log(
            "控制后端：scrcpy {version} 已启动（{mode}，max_size={max_size}）".format(
                version=SCRCPY_VERSION,
                mode="视频+控制" if video else "仅控制通道",
                max_size=getattr(_scrcpy, "max_size", 0) if video else 0,
            )
        )
        if self._on_started is not None:
            with suppress(Exception):
                self._on_started()

    def _on_perception_fallback(self, reason: str) -> None:
        """scrcpy 帧不可用时记录原因（不报错，状态里展示）。"""
        self.fallback_reason = str(reason or "")[:200]

    async def _refresh_scrcpy_caps(self) -> dict[str, Any]:
        """重新读取 scrcpy 客户端实时能力（video 需有解码帧后才为 True）。"""
        if self._scrcpy_client is None:
            return {}
        caps = await _client_capabilities(self._scrcpy_client)
        if caps:
            self._scrcpy_caps.update(caps)
            if self._scrcpy_caps.get("video") is True:
                self.fallback_reason = ""
        return caps

    def _apply_backends(self) -> None:
        control, _scrcpy, perception = self._config_sections()
        backend_choice = str(getattr(control, "backend", "auto") or "auto")
        expects_video = self._wants_scrcpy_video(perception)
        control_ok = self._scrcpy_caps.get("control", True) is not False
        video_ok = self._scrcpy_caps.get("video", True) is not False
        # 控制后端
        if (
            self._scrcpy_client is not None
            and backend_choice in {"auto", "scrcpy"}
            and control_ok
        ):
            if self._minitouch_client is not None:
                self.control_backend = MinitouchControlBackend(
                    self._minitouch_client,
                    self._adb_control,
                    log=self._warn,
                )
            else:
                self.control_backend = ScrcpyControlBackend(
                    self._scrcpy_client,
                    self._adb_control,
                    log=self._warn,
                    on_result=self._control_result,
                )
        else:
            self.control_backend = self._adb_control
            if (
                self._scrcpy_client is not None
                and backend_choice in {"auto", "scrcpy"}
            ):
                self.fallback_reason = "scrcpy control 能力不可用"
        # 感知后端：只有 screen_source 需要视频帧时才使用 scrcpy 帧；
        # screen_source=adb 时感知固定走 ADB 截图（scrcpy 仅控制通道），不算回退。
        if (
            self._scrcpy_client is not None
            and expects_video
            and video_ok
        ):
            self.perception_backend = ScrcpyPerceptionBackend(
                self._scrcpy_client,
                self._adb_perception,
                log=self._warn,
                on_fallback=self._on_perception_fallback,
            )
        elif self._scrcpy_client is not None and expects_video:
            self.perception_backend = self._adb_perception
            self.fallback_reason = "scrcpy video 能力不可用"
        else:
            self.perception_backend = self._adb_perception

    async def _try_minitouch(self, control: Any) -> None:
        minitouch = getattr(control, "minitouch", None)
        binary_path = str(getattr(minitouch, "binary_path", "") or "").strip()
        if not binary_path:
            return
        if self._minitouch_factory is None:
            self._warn_once(
                "minitouch_factory",
                f"minitouch 已配置但插件缺少加载器（{binary_path}），继续使用 scrcpy/ADB",
            )
            return
        try:
            client = await self._maybe_await(self._minitouch_factory(binary_path))
            self._minitouch_client = client
            if client is not None:
                self._log(
                    f"touch 后端：minitouch {MINITOUCH_VERSION} 已启用"
                    f"（{binary_path}）"
                )
        except Exception as exc:  # noqa: BLE001 - 可选后端失败继续 scrcpy/ADB
            self._warn_once(
                "minitouch", f"minitouch 启用失败，继续使用 scrcpy/ADB：{exc}"
            )

    async def ensure(self, *, need_control: bool = True, need_perception: bool = True) -> None:
        """串行启动、刷新能力与升级视频；临时失败按 30 秒退避恢复。"""
        async with self._lifecycle_lock:
            if self._scrcpy_client is not None:
                caps = await self._refresh_scrcpy_caps()
                if caps.get("control") is False:
                    await self._stop_current_client()
                    self.recoveries += 1
            await self._ensure_scrcpy(need_control=need_control, need_perception=need_perception)
            if self._needs_video_upgrade(need_perception=need_perception):
                await self._upgrade_scrcpy_video()
            if self._scrcpy_client is not None:
                if need_perception and self._scrcpy_want_video:
                    deadline = time.monotonic() + 3.0
                    while not self._scrcpy_caps.get("video") and time.monotonic() < deadline:
                        await asyncio.sleep(.05)
                        await self._refresh_scrcpy_caps()
                        if getattr(self._scrcpy_client, "last_error", ""):
                            break
                await self._refresh_scrcpy_caps()
                self._apply_backends()

    def _needs_video_upgrade(self, *, need_perception: bool) -> bool:
        """仅控制通道运行中，后续感知需要视频帧时是否需要重启一次。"""
        if not need_perception:
            return False
        if self._scrcpy_client is None or self._video_restart_attempted:
            return False
        if self._scrcpy_want_video is not False:
            return False
        _control, _scrcpy, perception = self._config_sections()
        return self._wants_scrcpy_video(perception)

    async def _upgrade_scrcpy_video(self) -> None:
        """停止仅控制通道实例并以视频模式重启一次（失败回退 ADB，不重试）。"""
        self._video_restart_attempted = True
        self._log("感知需要视频帧：scrcpy 当前仅控制通道，停止后以视频模式重启一次")
        await self._stop_current_client()
        await self._ensure_scrcpy(need_control=True, need_perception=True)
        if self._scrcpy_client is None:
            self._warn_once(
                "scrcpy_video_upgrade_failed",
                "scrcpy 视频模式重启失败，感知继续回退 ADB",
            )

    async def control(self) -> ControlBackend:
        await self.ensure(need_control=True, need_perception=False)
        return self.control_backend

    async def read_clipboard(self) -> str:
        """按需使用控制通道读剪贴板，兼容设备控制/截图配置均为 ADB。"""
        if getattr(self._adb, "app_mode", False):
            return await self._adb.read_clipboard()
        async with self._lifecycle_lock:
            if self._closed:
                raise BackendError("手机控制后端已关闭")
            if self._scrcpy_client is not None:
                caps = await self._refresh_scrcpy_caps()
                if caps.get("control") is False:
                    await self._stop_current_client()
            await self._ensure_scrcpy(need_control=False, need_perception=False, for_clipboard=True)
            if self._scrcpy_client is None:
                raise BackendError(self.last_error or "剪贴板控制通道不可用")
            self._last_activity = self._clock()
            return await self._maybe_await(self._scrcpy_client.get_clipboard())

    async def perception(self) -> PerceptionBackend:
        await self.ensure(need_control=False, need_perception=True)
        return self.perception_backend

    def note_activity(self, now: float | None = None) -> None:
        """记录一次手机工具活动（供空闲停止判断）。"""
        self._last_activity = float(now) if now is not None else self._clock()

    async def maybe_stop_idle(self, now: float | None = None) -> bool:
        """空闲超过 ``control.scrcpy.idle_stop_seconds`` 时停止 scrcpy。"""
        if self._scrcpy_client is None:
            return False
        _control, scrcpy, _perception = self._config_sections()
        if bool(getattr(scrcpy, "keep_alive", False)):
            return False
        try:
            idle = max(10, int(getattr(scrcpy, "idle_stop_seconds", 120)))
        except (TypeError, ValueError):
            idle = 120
        current = float(now) if now is not None else self._clock()
        last = self._last_activity or self._started_at or current
        if current - last < idle:
            return False
        await self.stop()
        self._log("控制后端：scrcpy 空闲已停止（避免常驻开销）")
        return True

    async def _stop_current_client(self) -> None:
        """停止控制客户端并恢复 ADB；各资源分别收尾并汇总错误。"""
        clients = [self._minitouch_client, self._scrcpy_client]
        self._scrcpy_client = None
        self._minitouch_client = None
        self._scrcpy_caps = {}
        self._scrcpy_last_error = ""
        self._scrcpy_want_video = None
        self.fallback_reason = ""
        self.control_backend = self._adb_control
        self.perception_backend = self._adb_perception
        errors = []
        for client in clients:
            if client is None:
                continue
            func = getattr(client, "stop", None) or getattr(client, "close", None)
            if callable(func):
                try:
                    await self._maybe_await(func())
                except Exception as exc:
                    errors.append(str(exc))
        if errors:
            raise BackendError("停止控制后端失败：" + "；".join(errors))

    async def stop(self) -> None:
        """停止 scrcpy 连接/解码并恢复 ADB 兜底（可被下次操作重新懒启动）。"""
        async with self._lifecycle_lock:
            self._video_restart_attempted = False
            self._scrcpy_unavailable = False
            self._scrcpy_error_logged = False
            self._retry_at = 0.0
            await self._stop_current_client()

    async def close(self) -> None:
        """插件停用：停止后端并禁止再次懒启动。"""
        self._closed = True
        await self.stop()
