"""验证码操作批次预校验单元测试：设备动作前整体校验 ops/confirm。

覆盖：任意后续步骤或 confirm 非法时设备动作数为 0；有效批次按计划顺序执行；
执行阶段取消后不再执行剩余步骤（物理动作不可回滚，可提前发现的非法必须提前拒绝）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.device_gate import DeviceExecutionGate, ExecutionCancelled
from core.errors import ActionFailedError
from core.service import PhoneService, _plan_verify_steps


class FakeActions:
    """记录 tap/swipe 调用顺序的 DeviceActions 替身。"""

    def __init__(self, size: tuple[int, int] = (1080, 1920)) -> None:
        self.size = size
        self.calls: list[tuple] = []
        self.size_calls = 0

    async def screen_size(self) -> tuple[int, int]:
        self.size_calls += 1
        return self.size

    async def tap(self, x: int, y: int) -> None:
        self.calls.append(("tap", x, y))

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


class FakeEvent:
    """门禁作用域需要的最小事件（extra + umo）。"""

    def __init__(self, umo: str = "webchat:FriendMessage:1") -> None:
        self.unified_msg_origin = umo
        self._extras: dict = {}

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


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


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 整批校验：任何非法项都不产生设备动作
# ----------------------------------------------------------------------


def test_invalid_later_step_does_not_execute_earlier_steps(tmp_path):
    service, actions = _service(tmp_path)

    async def scenario() -> None:
        with pytest.raises(ActionFailedError):
            await service.exec_verify_ops(
                [
                    {"action": "tap", "x": 10, "y": 20},
                    {"action": "tap", "x": 9999, "y": 20},
                ],
                wait_seconds=0.2,
            )

    _run(scenario())
    # 第一笔合法点击也不能先执行：整批校验在设备动作前完成。
    assert actions.calls == []


def test_invalid_action_type_rejected_before_device_action(tmp_path):
    service, actions = _service(tmp_path)

    async def scenario() -> None:
        with pytest.raises(ActionFailedError):
            await service.exec_verify_ops(
                [
                    {"action": "tap", "x": 10, "y": 20},
                    {"action": "long_press", "x": 10, "y": 20},
                ],
                wait_seconds=0.2,
            )

    _run(scenario())
    assert actions.calls == []


def test_ops_over_limit_rejected_before_device_query(tmp_path):
    service, actions = _service(tmp_path)

    async def scenario() -> None:
        with pytest.raises(ActionFailedError):
            await service.exec_verify_ops(
                [{"action": "tap", "x": 1, "y": 1} for _ in range(21)],
                wait_seconds=0.2,
            )

    _run(scenario())
    assert actions.calls == []
    # 结构非法时连屏幕尺寸都不查询。
    assert actions.size_calls == 0


def test_non_list_ops_rejected(tmp_path):
    service, actions = _service(tmp_path)

    async def scenario() -> None:
        with pytest.raises(ActionFailedError):
            await service.exec_verify_ops("tap(10,20)", wait_seconds=0.2)

    _run(scenario())
    assert actions.calls == []
    assert actions.size_calls == 0


def test_non_dict_item_rejected(tmp_path):
    service, actions = _service(tmp_path)

    async def scenario() -> None:
        with pytest.raises(ActionFailedError):
            await service.exec_verify_ops(
                [{"action": "tap", "x": 10, "y": 20}, "tap 30 40"],
                wait_seconds=0.2,
            )

    _run(scenario())
    assert actions.calls == []


@pytest.mark.parametrize(
    "confirm",
    [
        {"x": 9999, "y": 20},
        {"x": 10},
        {"x": "bad", "y": 20},
        "30,40",
    ],
)
def test_invalid_confirm_rejected_before_device_actions(tmp_path, confirm):
    service, actions = _service(tmp_path)

    async def scenario() -> None:
        with pytest.raises(ActionFailedError):
            await service.exec_verify_ops(
                [{"action": "tap", "x": 10, "y": 20}],
                confirm=confirm,
                wait_seconds=0.2,
            )

    _run(scenario())
    assert actions.calls == []


def test_invalid_drag_duration_rejected_before_device_actions(tmp_path):
    service, actions = _service(tmp_path)

    async def scenario() -> None:
        with pytest.raises(ActionFailedError):
            await service.exec_verify_ops(
                [
                    {
                        "action": "drag",
                        "x1": 10,
                        "y1": 20,
                        "x2": 100,
                        "y2": 200,
                        "duration_ms": "很快",
                    }
                ],
                wait_seconds=0.2,
            )

    _run(scenario())
    assert actions.calls == []


# ----------------------------------------------------------------------
# 有效批次：顺序执行、confirm 最后执行
# ----------------------------------------------------------------------


def test_valid_batch_executes_in_plan_order_with_confirm(tmp_path):
    service, actions = _service(tmp_path)

    async def scenario() -> str:
        return await service.exec_verify_ops(
            [
                {"action": "tap", "x": 10, "y": 20},
                {
                    "action": "drag",
                    "x1": 30,
                    "y1": 40,
                    "x2": 300,
                    "y2": 400,
                    "duration_ms": 800,
                },
            ],
            confirm={"x": 50, "y": 60},
            wait_seconds=0.2,
        )

    summary = _run(scenario())
    assert actions.calls == [
        ("tap", 10, 20),
        ("swipe", 30, 40, 300, 400, 800),
        ("tap", 50, 60),
    ]
    assert "已执行验证操作" in summary
    assert summary.index("tap(10,20)") < summary.index("drag(30,40)")
    assert "确认 tap(50,60)" in summary


def test_confirm_only_batch_is_executed(tmp_path):
    service, actions = _service(tmp_path)

    async def scenario() -> str:
        return await service.exec_verify_ops(
            [], confirm={"x": 50, "y": 60}, wait_seconds=0.2
        )

    summary = _run(scenario())
    assert actions.calls == [("tap", 50, 60)]
    assert "确认 tap(50,60)" in summary


def test_empty_batch_returns_no_action_message(tmp_path):
    service, actions = _service(tmp_path)

    async def scenario() -> str:
        return await service.exec_verify_ops([], wait_seconds=0.2)

    result = _run(scenario())
    assert "没有可执行的操作" in result
    assert actions.calls == []


def test_plan_normalizes_actions_and_clamps_duration():
    plan, confirm = _plan_verify_steps(
        [
            {
                "action": "swipe",
                "x1": 1,
                "y1": 2,
                "x2": 3,
                "y2": 4,
                "duration_ms": 50,
            },
            {"action": "click", "x": 5, "y": 6},
            {"action": "point", "x": 7, "y": 8},
        ],
        {"x": 9, "y": 10},
        100,
        200,
    )
    assert plan == [
        {
            "action": "drag",
            "x1": 1,
            "y1": 2,
            "x2": 3,
            "y2": 4,
            "duration_ms": 100,
        },
        {"action": "tap", "x": 5, "y": 6},
        {"action": "tap", "x": 7, "y": 8},
    ]
    assert confirm == {"x": 9, "y": 10}


# ----------------------------------------------------------------------
# 执行阶段取消：已计划的后续步骤不再执行
# ----------------------------------------------------------------------


def test_cancel_between_steps_stops_remaining_actions(tmp_path):
    service, actions = _service(tmp_path)
    gate = DeviceExecutionGate()
    service.gate = gate
    original_tap = actions.tap

    async def cancel_after_first_tap(x: int, y: int) -> None:
        await original_tap(x, y)
        if len(actions.calls) == 1:
            gate.force_release("测试取消")

    actions.tap = cancel_after_first_tap  # type: ignore[method-assign]
    event = FakeEvent()

    async def scenario() -> None:
        with gate.scope(
            event, purpose="phone_solve_verification", write=False
        ):
            with pytest.raises(ExecutionCancelled):
                await service.exec_verify_ops(
                    [
                        {"action": "tap", "x": 1, "y": 2},
                        {"action": "tap", "x": 3, "y": 4},
                    ],
                    confirm={"x": 5, "y": 6},
                    wait_seconds=0.2,
                )

    _run(scenario())
    # 取消后第二笔点击与最终确认都不执行。
    assert actions.calls == [("tap", 1, 2)]
