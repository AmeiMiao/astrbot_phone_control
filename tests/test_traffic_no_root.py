"""设备无 root 时抓包跳过重试的回归测试。"""

from __future__ import annotations

import asyncio
import types

from core.traffic_capture import TrafficCapture


class FakeAdb:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(self, args, **_kwargs):
        self.calls.append(list(args))
        return ""

    async def shell(self, _command, **_kwargs):
        return "1000"


def _capture(tmp_path) -> tuple[TrafficCapture, FakeAdb]:
    adb = FakeAdb()
    return (
        TrafficCapture(adb=adb, data_dir=tmp_path, logger=lambda _m: None),
        adb,
    )


def test_setup_device_marks_root_unavailable(tmp_path, monkeypatch) -> None:
    capture, _adb = _capture(tmp_path)
    monkeypatch.setattr("core.traffic_capture.ROOT_SETTLE_SECONDS", 0)

    async def _hash(_path):
        return "deadbeef"

    async def _cleanup():
        return None

    async def _wait_root():
        return False

    monkeypatch.setattr(capture, "_subject_hash_old", _hash)
    monkeypatch.setattr(capture, "cleanup_device_state", _cleanup)
    monkeypatch.setattr(capture, "_wait_root", _wait_root)

    try:
        asyncio.run(capture._setup_device())
    except RuntimeError as exc:
        assert "root" in str(exc)
    else:  # pragma: no cover - 必须有失败
        raise AssertionError("无 root 时应抛出 RuntimeError")
    assert capture.root_unavailable is True


def test_start_skips_when_root_unavailable(tmp_path) -> None:
    capture, adb = _capture(tmp_path)
    capture._root_unavailable = True
    result = asyncio.run(capture.start())
    assert result["ok"] is False
    assert result["root_unavailable"] is True
    assert adb.calls == []
    assert capture.status()["root_unavailable"] is True
