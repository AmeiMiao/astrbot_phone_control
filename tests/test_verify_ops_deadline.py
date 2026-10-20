"""验证操作执行期限回归：单次尝试到期后不再执行后续设备动作（D7）。

覆盖：首步前、首步后/步间、拖动后、最终确认前到期时，后续设备动作数为 0；
期限未到时有效批次仍按顺序执行并执行最终确认。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.errors import PhoneControlError
from core.service import PhoneService


class FakeDeadlineController:
    """只提供单次尝试剩余秒数的验证控制器替身。"""

    def __init__(self, remaining: float = 10.0) -> None:
        self.remaining = float(remaining)

    def attempt_remaining_seconds(self) -> float:
        return self.remaining


class FakeActions:
    """记录 tap/swipe 顺序的 DeviceActions 替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.on_action = None

    async def screen_size(self) -> tuple[int, int]:
        return (1080, 1920)

    async def tap(self, x: int, y: int) -> None:
        self.calls.append(("tap", x, y))
        if self.on_action is not None:
            self.on_action()

    async def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration_ms: int = 500,
    ) -> None:
        self.calls.append(("swipe", x1, y1, x2, y2, duration_ms))
        if self.on_action is not None:
            self.on_action()


def _service(
    tmp_path: Path,
) -> tuple[PhoneService, FakeActions, FakeDeadlineController]:
    actions = FakeActions()
    service = PhoneService(
        raw_config={"adb": {"host": "127.0.0.1", "port": 5555}},
        adb=object(),
        actions=actions,
        diary=None,
        data_dir=tmp_path,
    )
    controller = FakeDeadlineController()
    service.verification = controller
    return service, actions, controller


def _run(coro):
    return asyncio.run(coro)


def test_first_step_skipped_when_attempt_already_expired(tmp_path):
    service, actions, controller = _service(tmp_path)
    controller.remaining = 0.0

    async def scenario() -> None:
        with pytest.raises(PhoneControlError):
            await service.exec_verify_ops(
                [{"action": "tap", "x": 10, "y": 20}],
                wait_seconds=0.2,
            )

    _run(scenario())
    assert actions.calls == []


def test_steps_stopped_after_attempt_expires_between_taps(tmp_path):
    service, actions, controller = _service(tmp_path)

    def expire_after_first() -> None:
        if len(actions.calls) == 1:
            controller.remaining = 0.0

    actions.on_action = expire_after_first

    async def scenario() -> None:
        with pytest.raises(PhoneControlError):
            await service.exec_verify_ops(
                [
                    {"action": "tap", "x": 10, "y": 20},
                    {"action": "tap", "x": 30, "y": 40},
                ],
                wait_seconds=0.2,
            )

    _run(scenario())
    assert actions.calls == [("tap", 10, 20)]


def test_confirm_skipped_after_attempt_expires_after_drag(tmp_path):
    service, actions, controller = _service(tmp_path)

    def expire_after_drag() -> None:
        controller.remaining = 0.0

    actions.on_action = expire_after_drag

    async def scenario() -> None:
        with pytest.raises(PhoneControlError):
            await service.exec_verify_ops(
                [
                    {
                        "action": "drag",
                        "x1": 10,
                        "y1": 20,
                        "x2": 100,
                        "y2": 200,
                        "duration_ms": 300,
                    }
                ],
                confirm={"x": 30, "y": 40},
                wait_seconds=0.2,
            )

    _run(scenario())
    assert actions.calls == [("swipe", 10, 20, 100, 200, 300)]


def test_confirm_skipped_after_attempt_expires_after_first_tap(tmp_path):
    service, actions, controller = _service(tmp_path)

    def expire_after_first() -> None:
        if len(actions.calls) == 1:
            controller.remaining = 0.0

    actions.on_action = expire_after_first

    async def scenario() -> None:
        with pytest.raises(PhoneControlError):
            await service.exec_verify_ops(
                [{"action": "tap", "x": 10, "y": 20}],
                confirm={"x": 30, "y": 40},
                wait_seconds=0.2,
            )

    _run(scenario())
    # 已执行的首次点击不可回滚，但最终确认不得再执行。
    assert actions.calls == [("tap", 10, 20)]


def test_confirm_only_batch_skipped_when_attempt_expired(tmp_path):
    service, actions, controller = _service(tmp_path)
    controller.remaining = 0.0

    async def scenario() -> None:
        with pytest.raises(PhoneControlError):
            await service.exec_verify_ops(
                [], confirm={"x": 30, "y": 40}, wait_seconds=0.2
            )

    _run(scenario())
    assert actions.calls == []


def test_confirm_executes_when_attempt_remaining(tmp_path):
    service, actions, _controller = _service(tmp_path)

    async def scenario() -> str:
        return await service.exec_verify_ops(
            [{"action": "tap", "x": 10, "y": 20}],
            confirm={"x": 30, "y": 40},
            wait_seconds=0.2,
        )

    summary = _run(scenario())
    assert actions.calls == [("tap", 10, 20), ("tap", 30, 40)]
    assert "确认 tap(30,40)" in summary
