"""core.live_screen 单元测试：环形缓冲、新鲜度、启停与失败退避。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.live_screen import (
    DEFAULT_FRAMES,
    DEFAULT_INTERVAL_MS,
    MAX_INTERVAL_MS,
    MIN_INTERVAL_MS,
    LiveScreenWatcher,
    _clamp_frames,
    _clamp_interval_ms,
)


def _tmp_frame_bytes(index: int) -> bytes:
    return f"frame-{index}".encode()


class FakeCapture:
    """记录抓帧次数，按顺序返回递增帧。"""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    async def __call__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"抓帧失败 {self.calls}")
        index = self.calls
        path = Path(f"/tmp/live-frame-{index}.png")
        meta = {
            "method": "jpeg" if index % 2 else "png",
            "foreground": "com.example.app",
            "activity": ".MainActivity",
        }
        return _tmp_frame_bytes(index), path, meta


def test_clamp_settings_bounds():
    asyncio.run(_run_test_clamp_settings_bounds())


async def _run_test_clamp_settings_bounds():
        assert _clamp_interval_ms(10) == MIN_INTERVAL_MS
        assert _clamp_interval_ms(999999) == MAX_INTERVAL_MS
        assert _clamp_interval_ms("bad") == DEFAULT_INTERVAL_MS
        assert _clamp_frames(0) == 1
        assert _clamp_frames(99) == 10
        assert _clamp_frames("bad") == DEFAULT_FRAMES

def test_capture_once_and_ring_buffer(tmp_path):
    asyncio.run(_run_test_capture_once_and_ring_buffer())


async def _run_test_capture_once_and_ring_buffer():
        capture = FakeCapture()
        watcher = LiveScreenWatcher(capture, max_frames=2)
        first = await watcher.capture_once()
        assert first.data == b"frame-1"
        assert first.method == "jpeg"
        assert first.foreground == "com.example.app"
        assert first.activity == ".MainActivity"
        second = await watcher.capture_once()
        third = await watcher.capture_once()
        assert second.data == b"frame-2"
        assert third.data == b"frame-3"
        frames = watcher.frames()
        assert [frame.data for frame in frames] == [b"frame-2", b"frame-3"]
        assert watcher.capture_count == 3
        assert watcher.stats()["frames"] == 2

def test_latest_cached_freshness():
    asyncio.run(_run_test_latest_cached_freshness())


async def _run_test_latest_cached_freshness():
        watcher = LiveScreenWatcher(FakeCapture(), max_frames=3)
        assert watcher.latest_cached() is None
        frame = await watcher.capture_once()
        assert watcher.latest_cached(0) is frame
        assert watcher.latest_cached(60_000) is frame
        assert watcher.latest_cached(1) is None or watcher.latest_cached(1) is frame

def test_latest_triggers_capture_when_empty_or_stale():
    asyncio.run(_run_test_latest_triggers_capture_when_empty_or_stale())


async def _run_test_latest_triggers_capture_when_empty_or_stale():
        capture = FakeCapture()
        watcher = LiveScreenWatcher(capture, max_frames=2)
        frame = await watcher.latest(0)
        assert frame.data == b"frame-1"
        assert capture.calls == 1
        cached = await watcher.latest(60_000)
        assert cached is frame
        assert capture.calls == 1

def test_wait_fresh_returns_cached_or_timeout():
    asyncio.run(_run_test_wait_fresh_returns_cached_or_timeout())


async def _run_test_wait_fresh_returns_cached_or_timeout():
        capture = FakeCapture()
        watcher = LiveScreenWatcher(capture, max_frames=2)
        await watcher.capture_once()
        fresh = await watcher.wait_fresh(60_000, timeout=0.2)
        assert fresh is not None and fresh.data == b"frame-1"
        stale = await watcher.wait_fresh(0, timeout=0.1)
        assert stale is None or stale.data == b"frame-1"

def test_ensure_fresh_captures_when_stale():
    asyncio.run(_run_test_ensure_fresh_captures_when_stale())


async def _run_test_ensure_fresh_captures_when_stale():
        capture = FakeCapture()
        watcher = LiveScreenWatcher(capture, max_frames=2)
        frame = await watcher.ensure_fresh(1, timeout=0.1)
        assert frame.data == b"frame-1"
        assert capture.calls == 1

def test_configure_updates_frames_and_interval():
    asyncio.run(_run_test_configure_updates_frames_and_interval())


async def _run_test_configure_updates_frames_and_interval():
        watcher = LiveScreenWatcher(FakeCapture(), interval_ms=1200, max_frames=3)
        for _ in range(3):
            await watcher.capture_once()
        watcher.configure(interval_ms=500, max_frames=1)
        assert watcher.interval_ms == 500
        assert watcher.max_frames == 1
        assert len(watcher.frames()) == 1

def test_watcher_start_stop_captures_frames():
    asyncio.run(_run_test_watcher_start_stop_captures_frames())


async def _run_test_watcher_start_stop_captures_frames():
        capture = FakeCapture()
        watcher = LiveScreenWatcher(
            capture,
            interval_ms=MIN_INTERVAL_MS,
            max_frames=3,
            settings_provider=lambda: (True, MIN_INTERVAL_MS, 3),
        )
        watcher.start()
        assert watcher.running
        for _ in range(100):
            if capture.calls >= 2:
                break
            await asyncio.sleep(0.02)
        await watcher.stop()
        assert not watcher.running
        assert capture.calls >= 1
        # 停止后清空缓冲，不保留画面/常驻协程
        assert watcher.frames() == []

def test_watcher_disabled_does_not_capture():
    asyncio.run(_run_test_watcher_disabled_does_not_capture())


async def _run_test_watcher_disabled_does_not_capture():
        capture = FakeCapture()
        watcher = LiveScreenWatcher(
            capture,
            interval_ms=MIN_INTERVAL_MS,
            settings_provider=lambda: (False, MIN_INTERVAL_MS, 3),
        )
        watcher.start()
        await asyncio.sleep(0.2)
        await watcher.stop()
        assert capture.calls == 0
        assert not watcher.running

def test_watcher_records_error_and_keeps_running():
    asyncio.run(_run_test_watcher_records_error_and_keeps_running())


async def _run_test_watcher_records_error_and_keeps_running():
        capture = FakeCapture(fail_times=5)
        watcher = LiveScreenWatcher(
            capture,
            interval_ms=MIN_INTERVAL_MS,
            max_backoff_seconds=0.1,
            settings_provider=lambda: (True, MIN_INTERVAL_MS, 3),
        )
        watcher.start()
        for _ in range(100):
            if watcher.consecutive_errors:
                break
            await asyncio.sleep(0.02)
        await watcher.stop()
        assert watcher.last_error
        assert not watcher.running


def test_stop_clears_frames_and_running_state():
    async def scenario():
        watcher = LiveScreenWatcher(FakeCapture(), max_frames=3)
        await watcher.capture_once()
        await watcher.capture_once()
        assert len(watcher.frames()) == 2
        watcher.start()
        await asyncio.sleep(0)
        await watcher.stop()
        assert watcher.frames() == []
        assert watcher.running is False

    asyncio.run(scenario())


def test_failure_log_deduped():
    logs: list[str] = []

    async def scenario():
        capture = FakeCapture(fail_times=10)
        watcher = LiveScreenWatcher(
            capture,
            interval_ms=MIN_INTERVAL_MS,
            max_backoff_seconds=0.05,
            settings_provider=lambda: (True, MIN_INTERVAL_MS, 3),
            logger=logs.append,
        )
        watcher.start()
        for _ in range(100):
            if capture.calls >= 2:
                break
            await asyncio.sleep(0.02)
        await watcher.stop()
        return capture.calls

    calls = asyncio.run(scenario())
    assert calls >= 2
    assert len(logs) <= 2, logs
