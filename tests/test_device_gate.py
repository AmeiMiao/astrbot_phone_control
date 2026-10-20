"""core.device_gate 单元测试：授权、设备占用、抢占、代次、期限与租约释放。

全部离线运行：用假事件（``get_extra``/``set_extra``/``unified_msg_origin``）
与注入的单调时钟、回调验证门禁语义，不连接 AstrBot 与真实设备。
"""

from __future__ import annotations

import math

import pytest

from core.device_gate import (
    KIND_AUTONOMOUS,
    KIND_COMMAND,
    KIND_USER,
    RUN_KEY_EXTRA,
    DeviceExecutionGate,
    ExecutionCancelled,
    GateError,
    current_scope,
)

# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------


class FakeClock:
    """可拨动的单调时钟。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeEvent:
    """最小事件：覆盖 extra 读写与 unified_msg_origin。"""

    def __init__(
        self,
        *,
        umo: str = "webchat:FriendMessage:1",
        autonomous: bool = False,
        extras: dict | None = None,
    ) -> None:
        self.unified_msg_origin = umo
        self.autonomous = autonomous
        self._extras: dict = dict(extras or {})

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


class NoExtraEvent:
    """不支持 extra 的事件（按 UMO 兜底释放）。"""

    def __init__(self, umo: str = "webchat:FriendMessage:1") -> None:
        self.unified_msg_origin = umo


class LossyEvent:
    """能读写的鸭子事件，但 extra 读永远拿不到值（模拟框架重建事件）。"""

    def __init__(self, umo: str = "webchat:FriendMessage:1") -> None:
        self.unified_msg_origin = umo

    def get_extra(self, key, default=None):
        return default

    def set_extra(self, key, value):
        pass


def _gate(
    clock: FakeClock | None = None,
    *,
    stale_seconds: float = 1800.0,
    **kwargs,
) -> DeviceExecutionGate:
    return DeviceExecutionGate(
        clock=clock if clock is not None else FakeClock(),
        stale_seconds=stale_seconds,
        **kwargs,
    )


def _autonomy(event) -> bool:
    return bool(getattr(event, "autonomous", False))


# ----------------------------------------------------------------------
# 1. 异常与初始状态
# ----------------------------------------------------------------------


def test_gate_error_fields():
    err = GateError("只有管理员可以操作手机", code="denied")
    assert err.message == "只有管理员可以操作手机"
    assert err.code == "denied"
    assert str(err) == "只有管理员可以操作手机"
    # 空文案回退为统一提示
    assert GateError("").message == "当前不能操作手机"
    assert GateError("x").code == "denied"


def test_execution_cancelled_is_gate_error_with_code():
    err = ExecutionCancelled("手机操作已被暂停或取消")
    assert isinstance(err, GateError)
    assert err.code == "cancelled"
    assert err.message == "手机操作已被暂停或取消"


def test_gate_initial_state():
    gate = _gate()
    assert gate.epoch == 0
    assert gate.busy is False
    assert gate.owner_umo == ""
    assert gate.owner_kind == ""
    assert gate.status_text() == "设备占用：空闲"
    assert current_scope() is None


def test_run_key_extra_name_is_stable():
    # 事件 extra 的键名是 main.py / tools.py 共用契约。
    assert RUN_KEY_EXTRA == "phone_device_run_key"


# ----------------------------------------------------------------------
# 2. 授权与阻断（进入设备前）
# ----------------------------------------------------------------------


def test_enter_authorize_denied():
    calls: list = []

    def authorize(event):
        calls.append(event)
        return "error: 只有管理员可以操作手机"

    event = FakeEvent()
    gate = _gate(authorize=authorize)
    with pytest.raises(GateError) as exc:
        gate.enter(event, purpose="打开B站")
    assert exc.value.message == "error: 只有管理员可以操作手机"
    assert exc.value.code == "denied"
    assert calls == [event]
    # 拒绝不产生任何租约
    assert gate.busy is False
    assert gate.epoch == 0


def test_authorize_runs_before_blocking_check():
    order: list[str] = []

    def authorize(event):
        order.append("authorize")
        return "error: 无权限"

    def blocking(event, purpose, write):
        order.append("blocking")
        return "手机自动操作已暂停"

    gate = _gate(authorize=authorize, blocking_reason=blocking)
    with pytest.raises(GateError) as exc:
        gate.enter(FakeEvent(), purpose="tap")
    assert exc.value.message == "error: 无权限"
    assert order == ["authorize"]


def test_enter_blocking_reason_denied():
    recorded: list[tuple] = []

    def blocking(event, purpose, write):
        recorded.append((event, purpose, write))
        return "手机自动操作已暂停：当前在等待人工完成验证"

    event = FakeEvent()
    gate = _gate(blocking_reason=blocking)
    with pytest.raises(GateError) as exc:
        gate.enter(event, purpose="phone_tap", write=True)
    assert "手机自动操作已暂停" in exc.value.message
    assert recorded and recorded[0][1] == "phone_tap"
    assert recorded[0][2] is True
    assert gate.busy is False


def test_blocking_reason_none_means_allowed():
    gate = _gate(blocking_reason=lambda event, purpose, write: None)
    scope = gate.enter(FakeEvent(), purpose="tap")
    assert scope.kind == KIND_USER
    assert gate.busy is True


def test_blocking_callback_error_is_treated_as_allowed():
    def blocking(event, purpose, write):
        raise RuntimeError("状态读取失败")

    gate = _gate(blocking_reason=blocking)
    scope = gate.enter(FakeEvent(), purpose="tap")
    assert scope.gate is gate
    assert gate.busy is True


def test_blocking_reason_checked_before_busy():
    clock = FakeClock()
    state = {"reason": ""}
    gate = _gate(clock, blocking_reason=lambda e, p, w: state["reason"])
    holder = FakeEvent(umo="webchat:FriendMessage:1")
    gate.enter(holder, purpose="打开B站")
    state["reason"] = "手机自动操作已暂停：等待人工验证"
    with pytest.raises(GateError) as exc:
        gate.enter(FakeEvent(umo="webchat:FriendMessage:2"), purpose="tap")
    # 暂停文案优先于占用文案
    assert "等待人工验证" in exc.value.message
    assert gate.owner_umo == holder.unified_msg_origin


# ----------------------------------------------------------------------
# 3. 占用互斥与同一 run key 可重入
# ----------------------------------------------------------------------


def test_busy_other_run_key_rejected_with_hint():
    clock = FakeClock()
    gate = _gate(clock)
    first = FakeEvent(umo="webchat:FriendMessage:1")
    second = FakeEvent(umo="webchat:FriendMessage:2")
    gate.enter(first, purpose="打开B站")
    clock.advance(5)

    with pytest.raises(GateError) as exc:
        gate.enter(second, purpose="tap")
    message = exc.value.message
    assert "手机正被另一个用户任务使用" in message
    assert "已持续 5s" in message
    assert "/phone stop" in message
    # 原租约不受影响
    assert gate.busy is True
    assert gate.owner_umo == first.unified_msg_origin


def test_busy_message_labels_owner_kind():
    gate = _gate(autonomy_of=_autonomy)
    holder = FakeEvent(autonomous=True)
    gate.enter(holder, purpose="持续刷")
    with pytest.raises(GateError) as exc:
        gate.enter(FakeEvent(umo="webchat:FriendMessage:2"), purpose="tap")
    assert "手机正被另一个自主任务使用" in exc.value.message


def test_same_event_reentrant_same_run_key():
    clock = FakeClock()
    gate = _gate(clock)
    event = FakeEvent()
    first = gate.enter(event, purpose="打开B站")
    clock.advance(3)
    second = gate.enter(event, purpose="tap")
    assert first.key == second.key
    assert first.epoch == second.epoch
    assert gate.busy is True
    # 同一租约：不会按第二次 purpose 重建
    assert gate.owner_umo == event.unified_msg_origin
    assert "用户任务（打开B站）使用中" in gate.status_text()
    assert "已持续 3s" in gate.status_text()


def test_same_run_key_across_event_objects_reentrant():
    gate = _gate()
    key = "run-fixed-key"
    first_event = FakeEvent(extras={RUN_KEY_EXTRA: key})
    second_event = FakeEvent(
        umo="webchat:FriendMessage:2", extras={RUN_KEY_EXTRA: key}
    )
    first = gate.enter(first_event, purpose="打开B站")
    second = gate.enter(second_event, purpose="tap")
    assert first.key == second.key == key
    assert gate.busy is True


def test_enter_generates_run_key_and_stores_in_event_extra():
    gate = _gate()
    event = FakeEvent()
    scope = gate.enter(event, purpose="tap")
    stored = event.get_extra(RUN_KEY_EXTRA)
    assert isinstance(stored, str) and stored
    assert stored.startswith("run-")
    assert scope.key == stored
    # 再次进入复用同一个 key
    assert gate.enter(event, purpose="swipe").key == stored


def test_enter_does_not_activate_contextvar():
    gate = _gate()
    gate.enter(FakeEvent(), purpose="tap")
    assert current_scope() is None
    assert gate.busy is True


# ----------------------------------------------------------------------
# 3.1 可让路租约（任务收尾清理）
# ----------------------------------------------------------------------


def test_preemptible_lease_taken_over_by_new_task():
    gate = _gate()
    cleanup_event = FakeEvent(umo="webchat:FriendMessage:1")
    newcomer = FakeEvent(umo="webchat:FriendMessage:2")
    with gate.scope(
        cleanup_event, purpose="task_cleanup", preemptible=True
    ) as cleanup_scope:
        epoch_before = gate.epoch
        # 新任务需要设备：直接接管可让路租约，旧收尾下一次检查必须停止。
        gate.enter(newcomer, purpose="phone_tap")
        assert gate.epoch == epoch_before + 1
        assert gate.owner_umo == newcomer.unified_msg_origin
        with pytest.raises(ExecutionCancelled) as exc:
            cleanup_scope.checkpoint("am kill-all")
        assert "接管或任务已被停止" in exc.value.message
    # 作用域退出只清上下文；新任务的租约保留到 run 结束。
    assert gate.busy is True
    assert gate.release_event(newcomer) is True
    assert gate.busy is False


def test_preemptible_lease_not_taken_over_by_another_preemptible():
    gate = _gate()
    first = FakeEvent(umo="webchat:FriendMessage:1")
    second = FakeEvent(umo="webchat:FriendMessage:2")
    with gate.scope(first, purpose="task_cleanup", preemptible=True):
        epoch_before = gate.epoch
        with pytest.raises(GateError) as exc:
            gate.enter(second, purpose="task_cleanup", preemptible=True)
        assert "手机正被另一个" in exc.value.message
        # 收尾之间仍互斥：不接管、不改代次、租约仍归第一个收尾。
        assert gate.epoch == epoch_before
        assert gate.owner_umo == first.unified_msg_origin


def test_preemptible_request_does_not_take_normal_lease():
    gate = _gate()
    holder = FakeEvent(umo="webchat:FriendMessage:1")
    holder_scope = gate.enter(holder, purpose="phone_tap")
    epoch_before = gate.epoch
    with pytest.raises(GateError) as exc:
        gate.enter(
            FakeEvent(umo="webchat:FriendMessage:2"),
            purpose="task_cleanup",
            preemptible=True,
        )
    assert "手机正被另一个" in exc.value.message
    assert gate.epoch == epoch_before
    assert gate.owner_umo == holder.unified_msg_origin
    assert gate.release_event(holder_scope.event) is True


def test_takeover_validated_before_stealing_preemptible_lease():
    # 新请求自身已到期：不能先接管旧收尾再报错，否则旧收尾会被无谓中止。
    clock = FakeClock()
    state = {"remaining": 30.0}
    gate = _gate(clock, deadline_seconds=lambda event, purpose: state["remaining"])
    cleanup_event = FakeEvent(umo="webchat:FriendMessage:1")
    gate.enter(cleanup_event, purpose="task_cleanup", preemptible=True)
    epoch_before = gate.epoch
    state["remaining"] = 0.0
    with pytest.raises(ExecutionCancelled):
        gate.enter(FakeEvent(umo="webchat:FriendMessage:2"), purpose="phone_tap")
    assert gate.epoch == epoch_before
    assert gate.busy is True
    assert gate.owner_umo == cleanup_event.unified_msg_origin


# ----------------------------------------------------------------------
# 4. 租约类型判定
# ----------------------------------------------------------------------


def test_kind_defaults_to_user():
    gate = _gate()
    scope = gate.enter(FakeEvent(), purpose="打开B站")
    assert scope.kind == KIND_USER
    assert gate.owner_kind == KIND_USER


@pytest.mark.parametrize(
    "purpose", ["command", "command:status", "command:open"]
)
def test_kind_command_for_command_purpose(purpose):
    # 命令层传 command:<子命令>，kind 判定按前辍匹配；命令优先级高于自主任务判定。
    gate = _gate(autonomy_of=_autonomy)
    scope = gate.enter(FakeEvent(autonomous=True), purpose=purpose)
    assert scope.kind == KIND_COMMAND
    assert gate.owner_kind == KIND_COMMAND


def test_kind_non_command_purpose_with_colon_is_not_command():
    gate = _gate()
    scope = gate.enter(FakeEvent(), purpose="phone:status")
    assert scope.kind == KIND_USER


def test_kind_autonomous_for_autonomous_event():
    gate = _gate(autonomy_of=_autonomy)
    scope = gate.enter(FakeEvent(autonomous=True), purpose="持续刷")
    assert scope.kind == KIND_AUTONOMOUS
    assert gate.owner_kind == KIND_AUTONOMOUS


def test_autonomy_callback_error_falls_back_to_user():
    def autonomy(event):
        raise RuntimeError("鸭子类型事件")

    gate = _gate(autonomy_of=autonomy)
    scope = gate.enter(FakeEvent(), purpose="tap")
    assert scope.kind == KIND_USER


# ----------------------------------------------------------------------
# 5. 作用域与 status_text
# ----------------------------------------------------------------------


def test_scope_activates_and_clears_contextvar():
    gate = _gate()
    event = FakeEvent()
    with gate.scope(event, purpose="打开B站") as scope:
        assert current_scope() is scope
        assert scope.gate is gate
        assert scope.event is event
        assert scope.purpose == "打开B站"
        assert scope.write is True
        assert scope.owner_umo == event.unified_msg_origin
        assert scope.epoch == gate.epoch
    # 退出只清作用域，租约保留到 release_event
    assert current_scope() is None
    assert gate.busy is True
    assert gate.release_event(event) is True


def test_scope_clears_contextvar_on_exception():
    gate = _gate()
    event = FakeEvent()
    with pytest.raises(RuntimeError):
        with gate.scope(event, purpose="tap"):
            raise RuntimeError("handler 失败")
    assert current_scope() is None
    assert gate.busy is True
    gate.release_event(event)


def test_scope_fields_and_deadline():
    clock = FakeClock(now=100.0)
    gate = _gate(clock, deadline_seconds=lambda event, purpose: 30.0)
    scope = gate.enter(FakeEvent(), purpose="phone_solve", write=False)
    assert scope.deadline == 130.0
    assert scope.write is False
    assert gate.release_event(scope.event) is True


def test_status_text_busy_details():
    clock = FakeClock()
    gate = _gate(clock, autonomy_of=_autonomy)
    event = FakeEvent(autonomous=False)
    gate.enter(event, purpose="打开B站")
    clock.advance(65)
    text = gate.status_text()
    assert text == (
        "设备占用：用户任务（打开B站）使用中，已持续 65s；"
        "其他手机任务需等待其结束或发送 /phone stop"
    )


@pytest.mark.parametrize(
    ("purpose", "autonomous", "expected"),
    [
        ("持续刷", True, "设备占用：自主任务（持续刷）使用中"),
        ("command", False, "设备占用：命令（command）使用中"),
        ("", False, "设备占用：用户任务使用中"),
    ],
)
def test_status_text_labels(purpose, autonomous, expected):
    gate = _gate(autonomy_of=_autonomy)
    scope = gate.enter(
        FakeEvent(autonomous=autonomous), purpose=purpose
    )
    assert gate.status_text().startswith(expected)
    gate.release_event(scope.event)


# ----------------------------------------------------------------------
# 6. checkpoint 取消语义
# ----------------------------------------------------------------------


def test_checkpoint_ok_and_logs_operation():
    logs: list[str] = []
    gate = _gate(log=logs.append)
    event = FakeEvent()
    with gate.scope(event, purpose="tap"):
        assert gate.checkpoint("tap") is None
        assert gate.checkpoint() is None
    assert any("设备操作校验通过：tap" in line for line in logs)
    gate.release_event(event)


def test_checkpoint_outside_scope_passes_even_when_blocked():
    # 后台基础设施（实时屏幕/抓包等）不持有租约，也不经 contextvar 作用域。
    clock = FakeClock()
    state = {"reason": ""}
    gate = _gate(clock, blocking_reason=lambda e, p, w: state["reason"])
    gate.enter(FakeEvent(), purpose="tap")
    state["reason"] = "手机自动操作已暂停"
    assert gate.checkpoint("tap") is None

    idle_gate = _gate()
    assert idle_gate.checkpoint("tap") is None


def test_checkpoint_scope_of_other_gate_passes():
    gate_a = _gate()
    gate_b = _gate()
    event = FakeEvent()
    with gate_a.scope(event, purpose="tap"):
        # 另一个门禁的作用域不归本门禁校验
        assert gate_b.checkpoint("tap") is None
    gate_a.release_event(event)


def test_checkpoint_paused_raises():
    clock = FakeClock()
    state = {"reason": ""}
    gate = _gate(clock, blocking_reason=lambda e, p, w: state["reason"])
    event = FakeEvent()
    with gate.scope(event, purpose="tap"):
        assert gate.checkpoint("tap") is None
        state["reason"] = "手机自动操作已暂停：当前在等待人工完成验证"
        with pytest.raises(ExecutionCancelled) as exc:
            gate.checkpoint("tap")
        assert "等待人工完成验证" in exc.value.message
    gate.release_event(event)


def test_checkpoint_authorize_denied_raises():
    clock = FakeClock()
    state = {"denied": ""}
    gate = _gate(clock, authorize=lambda event: state["denied"])
    event = FakeEvent()
    with gate.scope(event, purpose="tap"):
        state["denied"] = "error: 权限被收回"
        with pytest.raises(ExecutionCancelled) as exc:
            gate.checkpoint("tap")
        assert "权限被收回" in exc.value.message
    gate.release_event(event)


def test_checkpoint_after_force_release_raises():
    gate = _gate()
    event = FakeEvent()
    with gate.scope(event, purpose="tap") as scope:
        gate.force_release("用户 /phone stop")
        with pytest.raises(ExecutionCancelled) as exc:
            scope.checkpoint("tap")
        assert "接管或任务已被停止" in exc.value.message


def test_checkpoint_after_preempt_raises():
    gate = _gate(autonomy_of=_autonomy)
    event = FakeEvent(autonomous=True)
    with gate.scope(event, purpose="持续刷") as scope:
        assert gate.preempt("user_priority") == [scope.key]
        with pytest.raises(ExecutionCancelled) as exc:
            scope.checkpoint("tap")
        assert "接管或任务已被停止" in exc.value.message


def test_checkpoint_lease_taken_by_stale_reclaim_raises():
    clock = FakeClock()
    gate = _gate(clock, stale_seconds=60.0)
    owner = FakeEvent(umo="webchat:FriendMessage:1")
    newcomer = FakeEvent(umo="webchat:FriendMessage:2")
    with gate.scope(owner, purpose="打开B站") as old_scope:
        clock.advance(61)
        gate.enter(newcomer, purpose="tap")
        assert gate.owner_umo == newcomer.unified_msg_origin
        with pytest.raises(ExecutionCancelled) as exc:
            old_scope.checkpoint("tap")
        assert "接管或任务已被停止" in exc.value.message


def test_checkpoint_epoch_changed_raises():
    # 同一 run key 被强制释放后重新进入：租约键相同但代次已变。
    clock = FakeClock()
    gate = _gate(clock)
    event = FakeEvent()
    with gate.scope(event, purpose="打开B站") as outer:
        gate.force_release("用户 /phone stop")
        with gate.scope(event, purpose="重新进入") as inner:
            assert inner.epoch == outer.epoch + 1
        with pytest.raises(ExecutionCancelled) as exc:
            outer.checkpoint("tap")
        assert "暂停或取消" in exc.value.message
    gate.release_event(event)


@pytest.mark.parametrize("remaining", [0.0, -1.0])
def test_enter_rejects_when_deadline_already_reached(remaining):
    gate = _gate(deadline_seconds=lambda event, purpose: remaining)
    with pytest.raises(ExecutionCancelled) as exc:
        gate.enter(FakeEvent(), purpose="phone_solve")
    assert "已到期" in exc.value.message
    # 期限校验先于绑定租约：到期操作不残留占用
    assert gate.busy is False
    assert gate.epoch == 0


def test_enter_expired_deadline_twice_leaves_no_busy_lease():
    # 同一事件连续两次因到期被拒，不应留下无人释放的租约。
    gate = _gate(deadline_seconds=lambda event, purpose: 0.0)
    event = FakeEvent()
    for _ in range(2):
        with pytest.raises(ExecutionCancelled) as exc:
            gate.enter(event, purpose="phone_solve")
        assert "已到期" in exc.value.message
        assert gate.busy is False
        assert gate.owner_umo == ""
        assert gate.epoch == 0
    # 其他任务收到的是「已到期」，而不是「设备被占用」
    with pytest.raises(ExecutionCancelled) as exc:
        gate.enter(FakeEvent(umo="webchat:FriendMessage:2"), purpose="tap")
    assert "已到期" in exc.value.message
    assert "手机正被另一个" not in exc.value.message
    assert gate.busy is False


def test_checkpoint_deadline_boundary():
    clock = FakeClock(now=1000.0)
    gate = _gate(clock, deadline_seconds=lambda event, purpose: 10.0)
    event = FakeEvent()
    with gate.scope(event, purpose="phone_solve") as scope:
        clock.advance(9.5)
        assert gate.checkpoint("tap") is None
        clock.advance(0.5)  # now == deadline，视为到期
        with pytest.raises(ExecutionCancelled) as exc:
            scope.checkpoint("tap")
        assert "已到期" in exc.value.message
    gate.release_event(event)


def test_checkpoint_infinite_deadline_passes():
    clock = FakeClock()
    gate = _gate(
        clock, deadline_seconds=lambda event, purpose: math.inf
    )
    event = FakeEvent()
    with gate.scope(event, purpose="tap") as scope:
        assert scope.deadline == math.inf
        clock.advance(10**6)
        assert gate.checkpoint("tap") is None
    gate.release_event(event)


def test_deadline_nan_means_unlimited():
    gate = _gate(deadline_seconds=lambda event, purpose: float("nan"))
    scope = gate.enter(FakeEvent(), purpose="tap")
    assert scope.deadline == math.inf
    gate.release_event(scope.event)


def test_deadline_callback_error_means_unlimited():
    def deadline(event, purpose):
        raise RuntimeError("期限读取失败")

    gate = _gate(deadline_seconds=deadline)
    scope = gate.enter(FakeEvent(), purpose="tap")
    assert scope.deadline == math.inf
    gate.release_event(scope.event)


# ----------------------------------------------------------------------
# 7. release_event
# ----------------------------------------------------------------------


def test_release_event_by_owner():
    gate = _gate()
    event = FakeEvent()
    gate.enter(event, purpose="tap")
    epoch = gate.epoch
    assert gate.release_event(event) is True
    assert gate.busy is False
    assert gate.epoch == epoch + 1
    assert gate.status_text() == "设备占用：空闲"
    # 重复释放返回 False
    assert gate.release_event(event) is False


def test_release_event_other_run_key_ignored():
    gate = _gate()
    owner = FakeEvent()
    gate.enter(owner, purpose="tap")
    other = FakeEvent(
        umo=owner.unified_msg_origin,
        extras={RUN_KEY_EXTRA: "run-another-task"},
    )
    assert gate.release_event(other) is False
    assert gate.busy is True
    assert gate.owner_umo == owner.unified_msg_origin


def test_release_event_when_idle_returns_false():
    gate = _gate()
    assert gate.release_event(FakeEvent()) is False
    assert gate.epoch == 0


def test_release_event_falls_back_to_umo_without_extra():
    gate = _gate()
    owner = NoExtraEvent(umo="webchat:FriendMessage:1")
    gate.enter(owner, purpose="打开B站")
    assert gate.busy is True
    # 事件被框架重建：不带 run key，按 UMO 兜底释放
    rebuilt = NoExtraEvent(umo="webchat:FriendMessage:1")
    assert gate.release_event(rebuilt) is True
    assert gate.busy is False


def test_release_event_umo_fallback_rejects_other_session():
    gate = _gate()
    gate.enter(NoExtraEvent(umo="webchat:FriendMessage:1"), purpose="打开B站")
    other = NoExtraEvent(umo="webchat:FriendMessage:2")
    assert gate.release_event(other) is False
    assert gate.busy is True


def test_release_event_umo_fallback_when_key_read_empty():
    gate = _gate()
    gate.enter(LossyEvent(umo="webchat:FriendMessage:1"), purpose="tap")
    assert gate.busy is True
    # 读不到 run key（值为空）时同样按 UMO 兜底
    assert gate.release_event(LossyEvent(umo="webchat:FriendMessage:1")) is True
    assert gate.busy is False


# ----------------------------------------------------------------------
# 8. preempt / force_release
# ----------------------------------------------------------------------


def test_preempt_revokes_autonomous_lease_and_bumps_epoch():
    logs: list[str] = []
    gate = _gate(autonomy_of=_autonomy, log=logs.append)
    event = FakeEvent(autonomous=True)
    scope = gate.enter(event, purpose="持续刷")
    assert gate.busy is True

    revoked = gate.preempt("user_priority")
    assert revoked == [scope.key]
    assert gate.busy is False
    assert gate.epoch == 1
    assert gate.owner_kind == ""
    assert gate.status_text() == "设备占用：空闲"
    assert any("撤销自主任务" in line for line in logs)
    # 撤销后的租约不可被原事件释放
    assert gate.release_event(event) is False


def test_preempt_keeps_user_lease():
    gate = _gate(autonomy_of=_autonomy)
    user_event = FakeEvent(autonomous=False)
    gate.enter(user_event, purpose="打开B站")
    assert gate.preempt("user_priority") == []
    assert gate.busy is True
    assert gate.epoch == 0
    assert gate.owner_umo == user_event.unified_msg_origin


def test_preempt_keeps_command_lease():
    gate = _gate(autonomy_of=_autonomy)
    gate.enter(FakeEvent(autonomous=True), purpose="command")
    assert gate.owner_kind == KIND_COMMAND
    assert gate.preempt("user_priority") == []
    assert gate.busy is True
    assert gate.epoch == 0


def test_preempt_when_idle_is_noop():
    gate = _gate()
    assert gate.preempt("user_priority") == []
    assert gate.epoch == 0
    assert gate.busy is False


def test_force_release_clears_lease_and_bumps_epoch():
    logs: list[str] = []
    gate = _gate(log=logs.append)
    event = FakeEvent()
    gate.enter(event, purpose="打开B站")
    gate.force_release("用户 stop")
    assert gate.busy is False
    assert gate.epoch == 1
    assert gate.release_event(event) is False
    assert any("强制释放" in line and "用户 stop" in line for line in logs)


def test_force_release_when_idle_still_bumps_epoch():
    logs: list[str] = []
    gate = _gate(log=logs.append)
    gate.force_release("插件停用")
    assert gate.busy is False
    assert gate.epoch == 1
    # 空闲强制释放不写租约日志（没有在途操作可中止）
    assert logs == []


# ----------------------------------------------------------------------
# 9. 超时租约回收
# ----------------------------------------------------------------------


def test_stale_lease_reclaimed_by_new_task():
    clock = FakeClock()
    logs: list[str] = []
    gate = _gate(clock, stale_seconds=60.0, log=logs.append)
    owner = FakeEvent(umo="webchat:FriendMessage:1")
    old_scope = gate.enter(owner, purpose="打开B站")
    clock.advance(61)

    newcomer = FakeEvent(umo="webchat:FriendMessage:2")
    new_scope = gate.enter(newcomer, purpose="tap")
    # 旧租约被回收后新任务接管，代次递增
    assert gate.busy is True
    assert gate.owner_umo == newcomer.unified_msg_origin
    assert new_scope.epoch == old_scope.epoch + 1
    assert gate.epoch == new_scope.epoch
    assert any("回收超时租约" in line for line in logs)


def test_stale_lease_not_reclaimed_before_timeout():
    clock = FakeClock()
    gate = _gate(clock, stale_seconds=60.0)
    owner = FakeEvent(umo="webchat:FriendMessage:1")
    gate.enter(owner, purpose="打开B站")
    clock.advance(30)
    with pytest.raises(GateError) as exc:
        gate.enter(FakeEvent(umo="webchat:FriendMessage:2"), purpose="tap")
    assert "手机正被另一个用户任务使用" in exc.value.message
    assert gate.owner_umo == owner.unified_msg_origin


def test_stale_lease_reclaim_resets_lease_timer():
    clock = FakeClock()
    gate = _gate(clock, stale_seconds=60.0)
    gate.enter(FakeEvent(umo="a:1"), purpose="一")
    clock.advance(61)
    newcomer = FakeEvent(umo="b:2")
    gate.enter(newcomer, purpose="二")
    clock.advance(10)
    # 新租约从接管时刻重新计时，未再超时
    with pytest.raises(GateError):
        gate.enter(FakeEvent(umo="c:3"), purpose="三")
    assert gate.owner_umo == newcomer.unified_msg_origin


def test_stale_seconds_has_minimum_floor():
    # stale_seconds 小于 60 时按 60s 生效，避免配置过小导致误回收
    clock = FakeClock()
    gate = _gate(clock, stale_seconds=5.0)
    gate.enter(FakeEvent(umo="a:1"), purpose="一")
    clock.advance(30)
    with pytest.raises(GateError):
        gate.enter(FakeEvent(umo="b:2"), purpose="二")
    clock.advance(31)  # 累计 61s
    gate.enter(FakeEvent(umo="b:2"), purpose="二")
    assert gate.busy is True


# ----------------------------------------------------------------------
# 10. 日志回调健壮性
# ----------------------------------------------------------------------


def test_log_callback_failure_does_not_break_gate():
    def broken_log(message: str) -> None:
        raise RuntimeError("日志后端故障")

    gate = _gate(log=broken_log, autonomy_of=_autonomy)
    event = FakeEvent()
    scope = gate.enter(event, purpose="tap")
    assert gate.busy is True
    # 用户租约不会被 preempt 撤销
    assert gate.preempt() == []
    gate.release_event(event)
    assert gate.busy is False
    gate.force_release("force")
    assert gate.epoch >= 2
    assert scope.key
