"""工具副作用分类回归：会导航/点击的工具按写操作门禁（D5）。

覆盖：``phone_check_inbox`` 不再登记为只读工具；验证 observing/attempting 时
不能切换 App 或点击消息入口；验证会话正常结束后恢复；纯读观察工具保持只读。
本机离线运行（本地无 AstrBot 时用 conftest 的最小 ``astrbot.api`` 桩并补平台
模块桩，验证状态机本身是纯标准库实现）。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

from core import verification as verification_mod
from core.errors import PhoneControlError
from core.models import Rect, ScreenSnapshot, UiNode
from core.service import PhoneService


def _load_verification_controller_class():
    """导入真实验证控制器类；本机缺 AstrBot 时补最小平台模块桩。"""
    import astrbot  # conftest 在本地无 AstrBot 时提供最小桩

    if getattr(astrbot, "_acat_local_stub", False):
        for name in ("astrbot.core", "astrbot.core.platform"):
            if name not in sys.modules:
                module = types.ModuleType(name)
                module.__path__ = []  # type: ignore[attr-defined]
                sys.modules[name] = module
        stub_name = "astrbot.core.platform.message_session"
        if stub_name not in sys.modules:
            module = types.ModuleType(stub_name)
            module.MessageSession = type("MessageSession", (), {})
            sys.modules[stub_name] = module
    module = importlib.import_module("core.verification_controller")
    return module.PhoneVerification


PhoneVerification = _load_verification_controller_class()


class FakeActions:
    """只记录 tap 的 DeviceActions 替身。"""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    async def screen_size(self) -> tuple[int, int]:
        return (1080, 1920)

    async def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    async def swipe(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        return None


def _controller(state: dict | None = None):
    """不跑完整构造的验证控制器（门禁方法只需状态与持久化标记）。"""
    controller = object.__new__(PhoneVerification)
    controller.state = dict(state) if state else {}
    controller._persist_error = ""
    controller._load_error = ""
    return controller


def _service(tmp_path: Path) -> tuple[PhoneService, FakeActions]:
    actions = FakeActions()
    service = PhoneService(
        raw_config={"adb": {"host": "127.0.0.1", "port": 5555}},
        adb=object(),
        actions=actions,
        diary=None,
        data_dir=tmp_path,
    )
    return service, actions


# ----------------------------------------------------------------------
# 工具分类：可导航工具按写操作，纯读观察工具保持只读
# ----------------------------------------------------------------------


def test_check_inbox_not_registered_as_read_only_tool():
    assert "phone_check_inbox" not in PhoneVerification._READ_ONLY_TOOLS
    for name in ("phone_screen", "phone_status", "phone_list_apps"):
        assert name in PhoneVerification._READ_ONLY_TOOLS


def test_guard_tool_denies_navigating_tool_while_observing():
    controller = _controller(verification_mod.new_session())
    denied = controller.guard_tool("phone_check_inbox", write=True)
    assert denied
    assert "error:" in denied
    # 纯读观察工具在验证进行中仍放行
    assert controller.guard_tool("phone_screen", write=False) == ""


def test_guard_tool_allows_navigating_tool_after_session_closed():
    controller = _controller(verification_mod.new_session())
    controller.state["status"] = verification_mod.STATUS_PASSED
    assert controller.guard_tool("phone_check_inbox", write=True) == ""


# ----------------------------------------------------------------------
# 底层副作用门禁：service.check_inbox 的 open_app / tap 按写语义复查
# ----------------------------------------------------------------------


def test_check_inbox_blocks_app_navigation_while_observing(tmp_path):
    service, actions = _service(tmp_path)
    service.verification = _controller(verification_mod.new_session())
    opened: list[str] = []

    async def fake_open_app(app: str) -> str:
        opened.append(app)
        return "已打开"

    service.open_app = fake_open_app  # type: ignore[method-assign]

    async def scenario() -> None:
        with pytest.raises(PhoneControlError):
            await service.check_inbox(app="review.app")

    asyncio.run(scenario())
    assert opened == []
    assert actions.taps == []


def test_check_inbox_blocks_inbox_tap_while_observing(tmp_path):
    service, actions = _service(tmp_path)
    service.verification = _controller(verification_mod.new_session())
    snapshot = ScreenSnapshot(
        package="review.app",
        width=1080,
        height=1920,
        nodes=[
            UiNode(
                ref=1,
                text="回复我的",
                clickable=True,
                bounds=Rect(0, 0, 200, 100),
            )
        ],
    )

    async def fake_screen(max_nodes: int = 0):
        return "屏幕内容", snapshot

    service.screen = fake_screen  # type: ignore[method-assign]

    async def scenario() -> None:
        with pytest.raises(PhoneControlError):
            await service.check_inbox(app="", include_image=False)

    asyncio.run(scenario())
    assert actions.taps == []


def test_check_inbox_runs_again_after_verification_session_closed(tmp_path):
    service, _actions = _service(tmp_path)
    controller = _controller(verification_mod.new_session())
    controller.state["status"] = verification_mod.STATUS_PASSED
    service.verification = controller
    screens: list[int] = []
    snapshot = ScreenSnapshot(package="review.app", width=1080, height=1920, nodes=[])

    async def fake_screen(max_nodes: int = 0):
        screens.append(max_nodes)
        return "当前页面", snapshot

    service.screen = fake_screen  # type: ignore[method-assign]

    async def scenario() -> str:
        text, _image, _mime = await service.check_inbox(
            app="", include_image=False
        )
        return text

    text = asyncio.run(scenario())
    assert "【收件箱检查】" in text
    # 会话结束后恢复页面观察能力
    assert screens
