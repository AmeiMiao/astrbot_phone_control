"""实时屏幕帧缓存：任务期间按配置间隔截图 + 环形缓冲 + 新鲜度查询。

插件在玩手机/后台任务会话第一次调用手机工具时懒启动 :class:`LiveScreenWatcher`，
按配置间隔用 ``service.capture()``（自适应截图：静态 PNG 与 raw 快速截图按需切换）
抓帧，保存最近 N 帧供 ``phone_live_screen`` / ``phone_read_screen`` 使用；任务结束
或会话空闲超时后停止并清空缓冲。本模块不依赖 astrbot，截图函数通过构造参数注入，
便于本地单测。
"""

from __future__ import annotations

from .identity import PLUGIN_NAME

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_INTERVAL_MS = 1200
DEFAULT_FRAMES = 3
MIN_INTERVAL_MS = 300
MAX_INTERVAL_MS = 5000
MIN_FRAMES = 1
MAX_FRAMES = 10
MAX_BACKOFF_SECONDS = 10.0


def _clamp_interval_ms(value: Any, default: int = DEFAULT_INTERVAL_MS) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, number))


def _clamp_frames(value: Any, default: int = DEFAULT_FRAMES) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(MIN_FRAMES, min(MAX_FRAMES, number))


@dataclass(frozen=True)
class LiveFrame:
    """一帧实时屏幕画面。"""

    data: bytes
    path: Path
    ts: float
    method: str = "png"
    foreground: str = ""
    activity: str = ""

    def age_ms(self, now: float | None = None) -> int:
        """帧年龄（毫秒，最小 0）。"""
        return max(0, int((float(now or time.time()) - self.ts) * 1000))


# 抓图函数返回 ``(图片字节, 落盘路径, 元信息)``；元信息可含 method/foreground/activity。
CaptureFn = Callable[[], Awaitable[tuple[bytes, Path, dict[str, Any]]]]
# 运行期配置提供者：返回 ``(enabled, interval_ms, frames)``。
SettingsProvider = Callable[[], tuple[bool, int, int]]


def _default_settings() -> tuple[bool, int, int]:
    return True, DEFAULT_INTERVAL_MS, DEFAULT_FRAMES


@dataclass
class LiveScreenWatcher:
    """常驻后台截图循环，维护最近若干帧的环形缓冲。"""

    capture: CaptureFn
    settings_provider: SettingsProvider | None = None
    logger: Callable[[str], None] | None = None
    interval_ms: int = DEFAULT_INTERVAL_MS
    max_frames: int = DEFAULT_FRAMES
    max_backoff_seconds: float = MAX_BACKOFF_SECONDS
    _frames: deque[LiveFrame] = field(init=False, repr=False)
    _lock: asyncio.Lock = field(init=False, repr=False)
    _stop: asyncio.Event = field(init=False, repr=False)
    _task: asyncio.Task | None = field(init=False, default=None, repr=False)
    last_error: str = field(init=False, default="")
    last_logged_error: str = field(init=False, default="")
    consecutive_errors: int = field(init=False, default=0)
    capture_count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.interval_ms = _clamp_interval_ms(self.interval_ms)
        self.max_frames = _clamp_frames(self.max_frames)
        self._frames = deque(maxlen=self.max_frames)
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self.consecutive_errors = 0
        self.last_error = ""
        self.last_logged_error = ""
        self.capture_count = 0

    # ---- 配置/状态 ----------------------------------------------------

    def _log(self, message: str) -> None:
        if self.logger:
            try:
                self.logger(message)
            except Exception:  # noqa: BLE001 - 日志回调不允许影响抓帧
                pass

    def configure(
        self, *, interval_ms: int | None = None, max_frames: int | None = None
    ) -> None:
        """更新抓帧间隔与缓冲帧数（配置热更新用）。"""
        if interval_ms is not None:
            self.interval_ms = _clamp_interval_ms(interval_ms)
        if max_frames is not None:
            frames = _clamp_frames(max_frames)
            if frames != self.max_frames:
                self._frames = deque(self._frames, maxlen=frames)
                self.max_frames = frames

    def _read_settings(self) -> tuple[bool, int, int]:
        provider = self.settings_provider or _default_settings
        try:
            enabled, interval_ms, frames = provider()
        except Exception as exc:  # noqa: BLE001 - 配置读取失败按默认/继续跑
            self._log(f"实时屏幕读取配置失败，按默认继续：{exc}")
            enabled, interval_ms, frames = True, self.interval_ms, self.max_frames
        return bool(enabled), int(interval_ms), int(frames)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def frames(self) -> list[LiveFrame]:
        """当前缓冲的全部帧（旧→新）。"""
        return list(self._frames)

    def clear(self) -> None:
        """清空环形缓冲（watcher 停止后调用，不保留画面）。"""
        self._frames.clear()

    def stats(self) -> dict[str, Any]:
        """运行状态摘要（/phone 状态与测试用）。"""
        return {
            "running": self.running,
            "capture_count": self.capture_count,
            "frames": len(self._frames),
            "interval_ms": self.interval_ms,
            "max_frames": self.max_frames,
            "last_error": self.last_error,
            "consecutive_errors": self.consecutive_errors,
        }

    # ---- 生命周期 -----------------------------------------------------

    def start(self) -> None:
        """启动常驻循环（幂等）。"""
        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._loop(), name=f"{PLUGIN_NAME}_live_screen"
        )

    async def stop(self) -> None:
        """停止常驻循环并等待任务退出（幂等）。"""
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        # 停止后不保留任何帧/常驻协程；下次任务再懒启动并重新抓帧。
        self._frames.clear()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.01, seconds))
        except asyncio.TimeoutError:
            pass

    async def _loop(self) -> None:
        """按配置间隔抓帧；失败指数退避（最多 10s），不中断循环。"""
        while not self._stop.is_set():
            enabled, interval_ms, frames = self._read_settings()
            self.configure(interval_ms=interval_ms, max_frames=frames)
            if not enabled:
                await self._sleep(1.0)
                continue
            started = time.monotonic()
            try:
                await self.capture_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 抓图失败只退避不中断
                error_text = str(exc) or exc.__class__.__name__
                self.last_error = error_text
                self.consecutive_errors += 1
                delay = min(
                    (self.interval_ms / 1000.0)
                    * (2 ** min(self.consecutive_errors, 5)),
                    self.max_backoff_seconds,
                )
                # 同一错误只记一次，避免连续失败刷屏。
                if error_text != self.last_logged_error:
                    self.last_logged_error = error_text
                    self._log(f"实时屏幕抓帧失败：{error_text}（将退避重试）")
                await self._sleep(delay)
                continue
            self.consecutive_errors = 0
            self.last_error = ""
            self.last_logged_error = ""
            await self._sleep(
                max(0.02, self.interval_ms / 1000.0 - (time.monotonic() - started))
            )

    # ---- 抓帧/查询 ----------------------------------------------------

    async def capture_once(self) -> LiveFrame:
        """串行抓取一帧并放入缓冲（与后台循环共用一把锁）。"""
        async with self._lock:
            data, path, meta = await self.capture()
            if not data:
                raise ValueError("实时屏幕抓帧数据为空")
            meta = meta or {}
            frame = LiveFrame(
                data=bytes(data),
                path=Path(path),
                ts=time.time(),
                method=str(meta.get("method") or "png"),
                foreground=str(meta.get("foreground") or ""),
                activity=str(meta.get("activity") or ""),
            )
            self._frames.append(frame)
            self.capture_count += 1
            self.last_error = ""
            self.last_logged_error = ""
            return frame

    def latest_cached(self, max_age_ms: int = 0) -> LiveFrame | None:
        """取缓存里最新一帧；超过 ``max_age_ms``（0=不限）视为没有。"""
        if not self._frames:
            return None
        frame = self._frames[-1]
        if max_age_ms and frame.age_ms() > max_age_ms:
            return None
        return frame

    async def latest(self, max_age_ms: int = 0) -> LiveFrame:
        """返回最新帧；缓存缺失/过期时立即抓一张。"""
        cached = self.latest_cached(max_age_ms)
        if cached is not None:
            return cached
        return await self.capture_once()

    async def wait_fresh(
        self, max_age_ms: int, timeout: float = 5.0
    ) -> LiveFrame | None:
        """等待年龄不超过 ``max_age_ms`` 的帧出现；超时返回 None。"""
        target = max(0, int(max_age_ms))
        deadline = time.monotonic() + max(0.05, float(timeout))
        while time.monotonic() < deadline:
            cached = self.latest_cached(target)
            if cached is not None:
                return cached
            await asyncio.sleep(0.05)
        return self.latest_cached(target)

    async def ensure_fresh(
        self, max_age_ms: int, *, timeout: float = 5.0
    ) -> LiveFrame:
        """确保返回不超过 ``max_age_ms`` 的帧；过期/缺失时立即抓一张。"""
        if max_age_ms <= 0:
            return await self.latest(0)
        frame = await self.wait_fresh(max_age_ms, timeout=timeout)
        if frame is not None:
            return frame
        return await self.capture_once()
