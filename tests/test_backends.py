"""core.backends 单元测试：后端选择、回退、minitouch、空闲停止与状态输出。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from core.backends import (
    AdbControlBackend,
    AdbPerceptionBackend,
    BackendManager,
    MinitouchControlBackend,
    ScrcpyControlBackend,
    ScrcpyPerceptionBackend,
)
from core.config import PhoneConfig


def _config(raw: dict | None = None) -> PhoneConfig:
    return PhoneConfig.from_mapping(raw or {})


class FakeActions:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def tap(self, x, y):
        self.calls.append(("tap", x, y))

    async def long_press(self, x, y, *, duration_ms=800):
        self.calls.append(("long_press", x, y, duration_ms))

    async def swipe_direction(self, direction, *, duration_ms=800):
        self.calls.append(("swipe", direction, duration_ms))

    async def input_text(self, text, *, press_enter=False):
        self.calls.append(("input_text", text, press_enter))

    async def press_key(self, key):
        self.calls.append(("press_key", key))

    async def foreground(self):
        return ("com.example.app", ".Main")

    async def screenshot(self, path, **kwargs):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-png")
        return target


class FakeScrcpyClient:
    def __init__(self, *, frame_data: bytes = b"frame-jpeg", frame_ts=None, fail_frame=False):
        self.calls: list[tuple] = []
        self.stopped = False
        self.started = False
        self.video = True
        self.started_video: bool | None = None
        self._frame_data = frame_data
        self._frame_ts = time.time() if frame_ts is None else frame_ts
        self._fail_frame = fail_frame

    def set_video_enabled(self, enabled: bool) -> None:
        self.video = bool(enabled)

    async def start(self):
        self.started = True
        self.started_video = self.video

    async def stop(self):
        self.stopped = True

    def capabilities(self):
        return {"h264": True, "video": self.video, "control": True}

    async def tap(self, x, y):
        self.calls.append(("tap", x, y))

    async def long_press(self, x, y, *, duration_ms=800):
        self.calls.append(("long_press", x, y, duration_ms))

    async def swipe_direction(self, direction, *, duration_ms=800):
        self.calls.append(("swipe", direction, duration_ms))

    async def inject_text(self, text):
        self.calls.append(("inject_text", text))

    async def press_key(self, key):
        self.calls.append(("press_key", key))

    async def back(self):
        self.calls.append(("back",))

    async def get_device_size(self):
        return (1080, 1920)

    async def latest_frame(self, max_age_ms=0):
        if self._fail_frame:
            raise RuntimeError("视频流中断")
        return {
            "data": self._frame_data,
            "format": "jpeg",
            "ts": self._frame_ts,
        }


class FakeMinitouchClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def tap(self, x, y):
        self.calls.append(("tap", x, y))

    async def long_press(self, x, y, *, duration_ms=800):
        self.calls.append(("long_press", x, y, duration_ms))

    async def swipe(self, direction, *, duration_ms=800):
        self.calls.append(("swipe", direction, duration_ms))


def test_adb_control_backend_delegates():
    async def scenario():
        actions = FakeActions()
        backend = AdbControlBackend(actions)
        await backend.tap(10, 20)
        await backend.long_press(10, 20, duration_ms=500)
        await backend.swipe("up", duration_ms=400)
        await backend.input_text("你好", press_enter=True)
        await backend.press_key("home")
        await backend.back()
        return backend.capabilities(), actions.calls

    caps, calls = asyncio.run(scenario())
    assert caps["control"] == "adb"
    assert calls[0] == ("tap", 10, 20)
    assert ("input_text", "你好", True) in calls
    assert ("press_key", "back") in calls


def test_scrcpy_control_backend_and_text_fallback():
    async def scenario():
        actions = FakeActions()
        fallback = AdbControlBackend(actions)
        client = FakeScrcpyClient()
        backend = ScrcpyControlBackend(client, fallback)
        await backend.tap(1, 2)
        await backend.long_press(3, 4)
        await backend.swipe("up")
        await backend.input_text("hello", press_enter=True)
        await backend.input_text("中文输入")
        await backend.press_key("home")
        await backend.back()
        return client.calls, actions.calls

    client_calls, adb_calls = asyncio.run(scenario())
    assert ("tap", 1, 2) in client_calls
    assert ("inject_text", "hello") in client_calls
    assert ("press_key", "enter") in client_calls
    assert ("input_text", "中文输入", False) in adb_calls
    assert ("press_key", "home") in client_calls
    assert ("back",) in client_calls


def test_scrcpy_control_backend_failure_falls_back():
    class BrokenClient(FakeScrcpyClient):
        async def tap(self, x, y):
            raise RuntimeError("socket closed")

        async def inject_text(self, text):
            raise RuntimeError("invalid op")

    async def scenario():
        actions = FakeActions()
        logs: list[str] = []
        backend = ScrcpyControlBackend(
            BrokenClient(),
            AdbControlBackend(actions),
            log=logs.append,
        )
        await backend.tap(5, 6)
        await backend.input_text("abc")
        return actions.calls, logs

    adb_calls, logs = asyncio.run(scenario())
    assert ("tap", 5, 6) in adb_calls
    assert ("input_text", "abc", False) in adb_calls
    assert logs


def test_minitouch_backend_touch_only_and_text_fallback():
    async def scenario():
        actions = FakeActions()
        client = FakeMinitouchClient()
        backend = MinitouchControlBackend(client, AdbControlBackend(actions))
        await backend.tap(7, 8)
        await backend.long_press(7, 8)
        await backend.swipe("down")
        await backend.input_text("hi", press_enter=True)
        await backend.press_key("enter")
        return client.calls, actions.calls

    touch_calls, adb_calls = asyncio.run(scenario())
    assert ("tap", 7, 8) in touch_calls
    assert ("input_text", "hi", True) in adb_calls
    assert ("press_key", "enter") in adb_calls


def test_adb_perception_backend(tmp_path):
    async def scenario():
        backend = AdbPerceptionBackend(FakeActions(), tmp_path)
        return await backend.screenshot(fast=True, expected_package="")

    data, fmt, meta = asyncio.run(scenario())
    assert data == b"fake-png"
    assert fmt == "png"
    assert meta["source"] == "adb"
    assert meta["method"] == "png"


def test_scrcpy_perception_backend_fresh_and_stale(tmp_path):
    async def scenario():
        adb = AdbPerceptionBackend(FakeActions(), tmp_path)
        fresh_client = FakeScrcpyClient(frame_ts=time.time())
        fresh = ScrcpyPerceptionBackend(fresh_client, adb)
        fresh_result = await fresh.screenshot()
        stale_client = FakeScrcpyClient(frame_ts=time.time() - 60)
        stale = ScrcpyPerceptionBackend(stale_client, adb, fresh_ms=1200)
        stale_result = await stale.screenshot()
        broken = ScrcpyPerceptionBackend(
            FakeScrcpyClient(fail_frame=True), adb
        )
        broken_result = await broken.screenshot()
        return fresh_result, stale_result, broken_result

    fresh_data, fresh_fmt, fresh_meta = asyncio.run(scenario())[0]
    assert fresh_data == b"frame-jpeg" and fresh_fmt == "jpeg"
    assert fresh_meta["source"] == "scrcpy"
    # 过期/失败帧回退 ADB
    _, _, stale_meta = asyncio.run(scenario())[1]
    _, _, broken_meta = asyncio.run(scenario())[2]
    assert stale_meta["source"] == "adb"
    assert broken_meta["source"] == "adb"


def test_manager_lazy_scrcpy_and_idle_stop():
    clock = [100.0]
    raw = {"control": {"scrcpy": {"keep_alive": False}}, "perception": {"screen_source": "auto", "live_screen_enabled": True}}

    async def scenario():
        client = FakeScrcpyClient()
        started: list[bool] = []
        manager = BackendManager(
            config_provider=lambda: _config(raw),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=lambda: client,
            on_started=lambda: started.append(True),
            clock=lambda: clock[0],
        )
        assert manager.scrcpy_running is False
        control = await manager.control()
        assert control.name == "scrcpy"
        assert manager.scrcpy_running is True and client.started
        assert started == [True]
        perception = await manager.perception()
        assert perception.name == "scrcpy"
        manager.note_activity(now=100.0)
        clock[0] = 200.0
        assert await manager.maybe_stop_idle(now=200.0) is False
        clock[0] = 219.0
        assert await manager.maybe_stop_idle(now=219.0) is False
        clock[0] = 220.0
        assert await manager.maybe_stop_idle(now=220.0) is True
        assert client.stopped is True
        assert manager.scrcpy_running is False
        assert manager.control_backend.name == "adb"
        # 下次操作再懒启动
        assert (await manager.control()).name == "scrcpy"
        return manager

    asyncio.run(scenario())


def test_manager_fallback_warns_once():
    async def scenario():
        warns: list[str] = []

        def fail_factory():
            raise RuntimeError("scrcpy-server 缺失")

        manager = BackendManager(
            config_provider=lambda: _config(),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=fail_factory,
            warn=warns.append,
        )
        control = await manager.control()
        await manager.perception()
        status = manager.status()
        return control.name, status, warns

    name, status, warns = asyncio.run(scenario())
    assert name == "adb"
    assert status.last_error
    assert len(warns) == 1


def test_manager_minitouch_optional():
    async def scenario():
        actions = FakeActions()
        minitouch_factory_calls: list[str] = []

        def minitouch_factory(path):
            minitouch_factory_calls.append(path)
            return FakeMinitouchClient()

        raw = {
            "control": {
                "minitouch": {"binary_path": "/opt/minitouch"},
            }
        }
        manager = BackendManager(
            config_provider=lambda: _config(raw),
            actions=actions,
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=FakeScrcpyClient,
            minitouch_factory=minitouch_factory,
        )
        control = await manager.control()
        status = manager.status()
        return control.name, status.minitouch_enabled, minitouch_factory_calls

    name, enabled, calls = asyncio.run(scenario())
    assert name == "minitouch"
    assert enabled is True
    assert calls == ["/opt/minitouch"]


def test_manager_minitouch_not_configured():
    async def scenario():
        calls: list[str] = []

        def minitouch_factory(path):
            calls.append(path)
            return FakeMinitouchClient()

        manager = BackendManager(
            config_provider=lambda: _config(),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=FakeScrcpyClient,
            minitouch_factory=minitouch_factory,
        )
        control = await manager.control()
        return control.name, calls

    name, calls = asyncio.run(scenario())
    assert name == "scrcpy"
    assert calls == []


def test_manager_adb_modes_do_not_start_scrcpy():
    async def scenario():
        factory_calls: list[bool] = []

        def factory():
            factory_calls.append(True)
            return FakeScrcpyClient()

        raw = {"control": {"backend": "adb"}, "perception": {"screen_source": "adb"}}
        manager = BackendManager(
            config_provider=lambda: _config(raw),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=factory,
        )
        control = await manager.control()
        perception = await manager.perception()
        return control.name, perception.name, factory_calls

    control_name, perception_name, calls = asyncio.run(scenario())
    assert control_name == "adb"
    assert perception_name == "adb"
    assert calls == []


def test_clipboard_can_start_control_only_with_both_adb_modes(tmp_path):
    async def scenario():
        client = FakeScrcpyClient()
        async def clipboard():return "https://example.org/copied-current-post"
        client.get_clipboard = clipboard
        manager = BackendManager(
            config_provider=lambda: _config({"control":{"backend":"adb"},"perception":{"screen_source":"adb"}}),
            actions=FakeActions(), adb=None, data_dir=tmp_path, scrcpy_factory=lambda:client,
        )
        try:
            assert await manager.read_clipboard()=="https://example.org/copied-current-post"
            assert client.started_video is False
            assert (await manager.control()).name=="adb"
            assert (await manager.perception()).name=="adb"
        finally:await manager.close()
        assert client.stopped
    asyncio.run(scenario())


def test_manager_perception_only_starts_scrcpy():
    async def scenario():
        raw = {"control": {"backend": "adb"}, "perception": {"screen_source": "auto", "live_screen_enabled": True}}
        client = FakeScrcpyClient()
        manager = BackendManager(
            config_provider=lambda: _config(raw),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=lambda: client,
        )
        perception = await manager.perception()
        control = await manager.control()
        return perception.name, control.name, manager.status_lines()

    perception_name, control_name, lines = asyncio.run(scenario())
    assert perception_name == "scrcpy"
    assert control_name == "adb"
    assert any("感知后端：scrcpy" in line for line in lines)


def test_manager_close_prevents_restart():
    async def scenario():
        calls: list[bool] = []

        def factory():
            calls.append(True)
            return FakeScrcpyClient()

        manager = BackendManager(
            config_provider=lambda: _config(),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=factory,
        )
        await manager.control()
        await manager.close()
        control = await manager.control()
        return control.name, calls

    name, calls = asyncio.run(scenario())
    assert name == "adb"
    assert calls == [True]


def test_manager_status_contains_video_availability():
    raw = {"perception": {"screen_source": "auto", "live_screen_enabled": True}}

    async def scenario():
        manager = BackendManager(
            config_provider=lambda: _config(raw),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=FakeScrcpyClient,
        )
        await manager.control()
        status = manager.status()
        lines = manager.status_lines()
        return status, lines

    status, lines = asyncio.run(scenario())
    assert status.scrcpy_running is True
    assert status.scrcpy_video is True
    assert any("scrcpy video：可用" in line for line in lines), lines


def test_scrcpy_perception_video_capability_false_falls_back():
    class NoVideoClient(FakeScrcpyClient):
        @property
        def capabilities(self):
            return {"video": False, "control": True, "decoder": True}

    async def scenario():
        fallback_reasons: list[str] = []
        raw = {"perception": {"screen_source": "auto", "live_screen_enabled": True}}
        manager = BackendManager(
            config_provider=lambda: _config(raw),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=lambda: NoVideoClient(),
        )
        perception = await manager.perception()
        status = manager.status()
        return perception.name, status, manager.status_lines()

    name, status, lines = asyncio.run(scenario())
    assert name == "adb"
    assert status.scrcpy_video is False
    assert status.fallback_reason and "video" in status.fallback_reason
    assert any("scrcpy video：不可用" in line for line in lines), lines


def test_scrcpy_perception_none_frame_falls_back_silently(tmp_path):
    class NoneFrameClient(FakeScrcpyClient):
        async def latest_frame(self, max_age_ms=0):
            return None

    async def scenario():
        adb = AdbPerceptionBackend(FakeActions(), tmp_path)
        reasons: list[str] = []
        backend = ScrcpyPerceptionBackend(
            NoneFrameClient(), adb, on_fallback=reasons.append
        )
        result = await backend.screenshot()
        return result, reasons

    (data, fmt, meta), reasons = asyncio.run(scenario())
    assert data == b"fake-png" and fmt == "png"
    assert meta["source"] == "adb"
    assert reasons and "暂无可用视频帧" in reasons[0]


def test_scrcpy_frame_object_contract(tmp_path):
    import types

    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 16

    class FrameObjectClient(FakeScrcpyClient):
        async def latest_frame(self, max_age_ms=0):
            return types.SimpleNamespace(
                data=jpeg,
                ts=time.time(),
                width=576,
                height=1024,
                mime="image/jpeg",
            )

    async def scenario():
        adb = AdbPerceptionBackend(FakeActions(), tmp_path)
        backend = ScrcpyPerceptionBackend(
            FrameObjectClient(), adb, fresh_ms=5000
        )
        return await backend.screenshot()

    data, fmt, meta = asyncio.run(scenario())
    assert data == jpeg
    assert fmt == "jpeg"
    assert meta["source"] == "scrcpy"
    assert meta.get("width") == 576 and meta.get("height") == 1024


def test_property_capabilities_are_supported():
    class PropertyClient(FakeScrcpyClient):
        @property
        def capabilities(self):
            return {"video": True, "control": True, "decoder": True}

    raw = {"perception": {"screen_source": "auto", "live_screen_enabled": True}}

    async def scenario():
        manager = BackendManager(
            config_provider=lambda: _config(raw),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=lambda: PropertyClient(),
        )
        control = await manager.control()
        perception = await manager.perception()
        return control.name, perception.name, manager.status().scrcpy_video

    control_name, perception_name, video = asyncio.run(scenario())
    assert control_name == "scrcpy"
    assert perception_name == "scrcpy"
    assert video is True


class LateVideoScrcpyClient(FakeScrcpyClient):
    """首帧前 capabilities.video=False，解码出帧后变 True。"""

    def __init__(self) -> None:
        super().__init__()
        self.video_ready = False

    def capabilities(self):
        return {"video": self.video_ready, "control": True}

    def latest_frame(self, *, max_age_ms: int = 0):
        return None


def test_manager_refreshes_video_capability_after_first_frame():
    raw = {"perception": {"screen_source": "auto", "live_screen_enabled": True}}

    async def scenario():
        client = LateVideoScrcpyClient()
        manager = BackendManager(
            config_provider=lambda: _config(raw),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=lambda: client,
        )
        await manager.ensure(need_control=False, need_perception=True)
        first = manager.perception_backend.name
        client.video_ready = True
        await manager.ensure(need_control=False, need_perception=True)
        return first, manager.perception_backend.name, manager.status()

    first, second, status = asyncio.run(scenario())
    assert first == "adb"
    assert second == "scrcpy"
    assert status.scrcpy_video is True
    assert status.fallback_reason == ""


def test_scrcpy_perception_expected_package_uses_frame_when_foreground_matches(tmp_path):
    async def scenario(expected: str):
        client = FakeScrcpyClient()
        fallback = AdbPerceptionBackend(FakeActions(), tmp_path / "shots")
        backend = ScrcpyPerceptionBackend(
            client, fallback, fresh_ms=5000
        )
        return await backend.screenshot(expected_package=expected)

    data, fmt, meta = asyncio.run(scenario("com.example.app"))
    assert data == b"frame-jpeg"
    assert fmt == "jpeg"
    assert meta["source"] == "scrcpy"

    data2, _, meta2 = asyncio.run(scenario("com.other.app"))
    assert data2 == b"fake-png"
    assert meta2["source"] == "adb"


def test_manager_default_adb_screen_source_starts_control_only():
    """默认配置（screen_source=adb）：scrcpy 仅控制通道，感知走 ADB、video 未启用。"""

    async def scenario():
        client = FakeScrcpyClient()
        calls: list[bool] = []

        def factory(video=True):
            calls.append(video)
            return client

        manager = BackendManager(
            config_provider=lambda: _config(),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=factory,
        )
        control = await manager.control()
        status = manager.status()
        lines = manager.status_lines()
        perception = await manager.perception()
        return control.name, perception.name, status, lines, calls, client

    name, perception_name, status, lines, calls, client = asyncio.run(scenario())
    assert name == "scrcpy"
    assert perception_name == "adb"
    assert calls == [False]
    assert client.video is False
    assert status.scrcpy_running is True
    assert status.scrcpy_video is False
    assert status.scrcpy_video_disabled is True
    assert status.fallback_reason == ""
    assert any("仅控制通道" in line for line in lines), lines


def test_manager_auto_with_live_screen_disabled_starts_control_only():
    """screen_source=auto 且持续截图开关关闭时：scrcpy 仅控制通道，感知走 ADB。"""

    async def scenario():
        client = FakeScrcpyClient()
        seen_video: list[bool] = []

        def factory(video=True):
            seen_video.append(video)
            return client

        manager = BackendManager(
            config_provider=lambda: _config(
                {"perception": {"screen_source": "auto", "live_screen_enabled": False}}
            ),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=factory,
        )
        try:
            control = await manager.control()
            assert control.name == "scrcpy"
            assert seen_video == [False]
            assert client.started_video is False
            perception = await manager.perception()
            assert perception.name == "adb"
            status = manager.status()
            assert status.scrcpy_video_disabled is True
            assert status.scrcpy_video is False
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_manager_video_mode_when_screen_source_auto():
    async def scenario():
        client = FakeScrcpyClient()
        calls: list[bool] = []

        def factory(video=True):
            calls.append(video)
            return client

        manager = BackendManager(
            config_provider=lambda: _config({"perception": {"screen_source": "auto", "live_screen_enabled": True}}),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=factory,
        )
        control = await manager.control()
        perception = await manager.perception()
        status = manager.status()
        return control.name, perception.name, status, calls, client

    name, perception_name, status, calls, client = asyncio.run(scenario())
    assert name == "scrcpy"
    assert perception_name == "scrcpy"
    assert calls == [True]
    assert client.video is True
    assert status.scrcpy_video is True
    assert status.scrcpy_video_disabled is False


def test_manager_sets_video_flag_on_factory_without_video_kwarg():
    """工厂不接受 video 关键字时，管理器在 start 前通过属性/方法设置开关。"""

    async def scenario():
        client = FakeScrcpyClient()

        def factory():
            return client

        manager = BackendManager(
            config_provider=lambda: _config(),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=factory,
        )
        await manager.control()
        return client

    client = asyncio.run(scenario())
    assert client.video is False
    assert client.started is True
    assert client.started_video is False


def test_manager_upgrades_control_only_to_video_once():
    """仅控制通道运行后配置改为需要视频：停止并以视频模式重启一次。"""
    raw = {"perception": {"screen_source": "adb"}}
    clients: list[FakeScrcpyClient] = []

    def factory(video=True):
        client = FakeScrcpyClient()
        client.video = video
        clients.append(client)
        return client

    async def scenario():
        manager = BackendManager(
            config_provider=lambda: _config(raw),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=factory,
        )
        await manager.control()
        assert len(clients) == 1 and clients[0].video is False
        # 配置热加载：感知来源改为 auto，后续感知调用需要视频帧
        raw["perception"]["screen_source"] = "auto"
        raw["perception"]["live_screen_enabled"] = True
        perception = await manager.perception()
        assert perception.name == "scrcpy"
        assert len(clients) == 2
        assert clients[0].stopped is True
        assert clients[1].video is True
        # 再次调用不会继续重启
        await manager.perception()
        await manager.perception()
        assert len(clients) == 2
        status = manager.status()
        assert status.scrcpy_video is True
        assert status.scrcpy_video_disabled is False

    asyncio.run(scenario())


def test_manager_video_upgrade_failure_falls_back_adb_without_loop():
    raw = {"perception": {"screen_source": "adb"}}
    factory_calls: list[bool] = []

    class FailingSecondClient(FakeScrcpyClient):
        async def start(self):
            raise RuntimeError("视频模式启动失败")

    async def scenario():
        clients_created = {"count": 0}

        def factory(video=True):
            factory_calls.append(video)
            clients_created["count"] += 1
            if clients_created["count"] == 1:
                client = FakeScrcpyClient()
                client.video = video
                return client
            return FailingSecondClient()

        warns: list[str] = []
        manager = BackendManager(
            config_provider=lambda: _config(raw),
            actions=FakeActions(),
            adb=None,
            data_dir="/tmp/backends-test",
            scrcpy_factory=factory,
            warn=warns.append,
        )
        await manager.control()
        raw["perception"]["screen_source"] = "auto"
        raw["perception"]["live_screen_enabled"] = True
        perception = await manager.perception()
        assert perception.name == "adb"
        assert factory_calls == [False, True]
        # 失败后不无限重试
        await manager.perception()
        await manager.perception()
        assert factory_calls == [False, True]
        return warns

    warns = asyncio.run(scenario())
    assert any("重启失败" in message for message in warns), warns


def test_concurrent_warmup_and_control_start_one_scrcpy(tmp_path):
    async def run():
        starts = []
        class SlowClient(FakeScrcpyClient):
            async def start(self):
                starts.append(self)
                await asyncio.sleep(0.02)
                await super().start()
        manager = BackendManager(config_provider=lambda: _config({}),
            actions=FakeActions(), adb=None, data_dir=tmp_path,
            scrcpy_factory=lambda: SlowClient())
        await asyncio.gather(manager.ensure(), manager.control(), manager.perception())
        assert len(starts) == 1
        await manager.stop()
        assert starts[0].stopped
    asyncio.run(run())


def test_transient_scrcpy_failure_recovers_after_backoff(tmp_path):
    async def run():
        now = [100.0]
        attempts = []
        def factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("device offline")
            return FakeScrcpyClient()
        manager = BackendManager(config_provider=lambda: _config({}),
            actions=FakeActions(), adb=None, data_dir=tmp_path,
            scrcpy_factory=factory, clock=lambda: now[0])
        assert (await manager.control()).name == "adb"
        now[0] = 110.0
        assert (await manager.control()).name == "adb"
        assert len(attempts) == 1
        now[0] = 131.0
        assert (await manager.control()).name == "scrcpy"
        assert len(attempts) == 2
        await manager.stop()
    asyncio.run(run())


def test_keep_alive_preserves_one_client_and_explicit_stop_wins():
    async def run():
        client=FakeScrcpyClient()
        manager=BackendManager(config_provider=lambda:_config({'control':{'scrcpy':{'keep_alive':True}},'perception':{'screen_source':'adb'}}),actions=FakeActions(),adb=None,data_dir='/tmp/keep-alive',scrcpy_factory=lambda:client)
        await manager.ensure()
        assert await manager.maybe_stop_idle(now=time.monotonic()+10000) is False
        assert client.started and not client.stopped
        await manager.close()
        await manager.ensure()
        assert client.stopped and not manager.scrcpy_running
    asyncio.run(run())


def test_first_frame_waits_for_decoder_and_uses_scrcpy():
    async def run():
        client=FakeScrcpyClient();calls=[0]
        original=client.capabilities
        def capabilities():
            caps=original();calls[0]+=1;caps['video']=calls[0]>=3;return caps
        client.capabilities=capabilities
        manager=BackendManager(config_provider=lambda:_config({'perception':{'screen_source':'scrcpy'}}),actions=FakeActions(),adb=None,data_dir='/tmp/first-frame',scrcpy_factory=lambda:client)
        backend=await manager.perception()
        assert backend.name=='scrcpy' and calls[0]>=3
        await manager.close()
    asyncio.run(run())


def test_static_old_frame_requests_fresh_frame_without_adb():
    async def run():
        class Client:
            refreshed=False
            def capabilities(self):return {'video':True}
            def latest_frame(self,**kwargs):
                if not self.refreshed:return None
                return b'new-frame','jpeg',{'source':'scrcpy','ts':time.time()}
            async def request_frame(self):self.refreshed=True
        fallback=AdbPerceptionBackend(FakeActions(),'/tmp/static-refresh')
        backend=ScrcpyPerceptionBackend(Client(),fallback)
        data,fmt,meta=await backend.screenshot()
        assert data==b'new-frame' and meta['source']=='scrcpy'
    asyncio.run(run())


def test_app_backend_sources_do_not_report_host_scrcpy_fallback(tmp_path):
    from types import SimpleNamespace

    async def scenario():
        adb = SimpleNamespace(app_mode=True, screen_source='app_screencap')
        actions = FakeActions()
        actions._adb = adb
        warnings = []
        manager = BackendManager(
            config_provider=lambda: _config({'perception': {'screen_source': 'scrcpy'}}),
            actions=actions, adb=adb, data_dir=tmp_path, warn=warnings.append,
        )
        await manager.control()
        status = manager.status()
        assert status.connection_mode == 'app' and status.control == 'relay_app'
        assert not status.scrcpy_running
        _, _, meta = await (await manager.perception()).screenshot()
        assert meta['source'] == 'app_screencap'
        manager.record_capture(meta['source'])
        assert manager.fallback_count == 0 and not warnings
        assert all('懒启动' not in line and '兜底' not in line for line in manager.status_lines())
        adb.screen_source = 'app_scrcpy'
        assert manager.status().perception == 'app_scrcpy'
        await manager.close()

    asyncio.run(scenario())
