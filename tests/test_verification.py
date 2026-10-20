"""core.verification 单元测试：尝试计数、失败上限、总时限、人工接管、恢复/取消与持久化。

另含 ``PhoneVerification`` 控制器级回归：取消/提交身份隔离（D1）、状态读取
故障保持阻断与显式处置（D3）。控制器依赖少量 AstrBot 符号，本地用最小替身导入。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from core.verification import (
    ACTIVE_STATUSES,
    CHALLENGE_KEYWORDS,
    CLOSED_STATUSES,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MANUAL_WAIT_TIMEOUT_SECONDS,
    DEFAULT_TOTAL_TIMEOUT_SECONDS,
    NOTIFY_FAILED,
    NOTIFY_NONE,
    NOTIFY_SENT,
    OUTCOME_CLEARED,
    PAUSED_STATUSES,
    SOLVE_KIND_CAPTCHA,
    STORE_LOAD_EMPTY,
    STORE_LOAD_FAULTS,
    STORE_LOAD_MISSING,
    STORE_LOAD_OK,
    STORE_LOAD_PARSE_ERROR,
    STORE_LOAD_READ_ERROR,
    STORE_LOAD_STRUCTURE_ERROR,
    STATUS_ATTEMPTING,
    STATUS_AWAITING_USER,
    STATUS_CANCELLED,
    STATUS_CHECKING,
    STATUS_EXPIRED,
    STATUS_OBSERVING,
    STATUS_PASSED,
    TERMINAL_STATUSES,
    SolveOutcome,
    VerificationStore,
    apply_awaiting,
    build_awaiting_message,
    challenge_present,
    current_identity,
    effective_limits,
    elapsed_ms,
    enter_checking,
    finish_attempt,
    finish_checking,
    is_active,
    is_closed,
    is_paused,
    is_terminal,
    manual_wait_expired,
    mark_cancelled,
    mark_expired,
    mark_notify,
    mark_passed,
    new_session,
    normalize_int,
    remaining_ms,
    reserve_attempt,
    session_timeout_reached,
    should_notify,
    status_text,
    submission_mismatch_reason,
)


def _session(**kwargs):
    defaults = dict(device="dev", app="B站", origin="auto", now=1000.0)
    defaults.update(kwargs)
    return new_session(**defaults)


def test_defaults_and_normalize():
    assert DEFAULT_MAX_ATTEMPTS == 3
    assert DEFAULT_TOTAL_TIMEOUT_SECONDS == 120
    assert DEFAULT_MANUAL_WAIT_TIMEOUT_SECONDS == 600
    assert normalize_int("5", 3) == 5
    assert normalize_int("bad", 3) == 3
    assert normalize_int(-1, 3, minimum=0) == 0
    assert normalize_int(0, 3, minimum=10) == 10


def test_status_groups():
    """过期属于暂停而非终态：只有 passed/cancelled 关闭会话。"""
    assert PAUSED_STATUSES == frozenset(
        {STATUS_AWAITING_USER, STATUS_CHECKING, STATUS_EXPIRED}
    )
    assert CLOSED_STATUSES == frozenset({STATUS_PASSED, STATUS_CANCELLED})
    assert TERMINAL_STATUSES == CLOSED_STATUSES
    assert STATUS_EXPIRED in PAUSED_STATUSES
    assert STATUS_EXPIRED not in CLOSED_STATUSES
    assert STATUS_AWAITING_USER in ACTIVE_STATUSES


def test_new_session_fields_and_active():
    state = _session()
    assert state["id"].startswith("ver-")
    assert state["status"] == STATUS_OBSERVING
    assert state["attempts"] == 0
    assert state["attempt_started_at"] == 0.0
    assert state["failure_count"] == 0
    assert state["quiz_answered"] == 0
    assert state["quiz_total"] == 0
    assert state["cancel_generation"] == 0
    assert is_active(state)
    assert not is_paused(state)
    assert not is_closed(state)
    assert not is_terminal(state)
    assert state["status"] in ACTIVE_STATUSES


def test_reserve_attempt_failure_limit_and_awaiting():
    state = _session(max_attempts=2)
    limits = effective_limits(state, {"max_attempts": 2, "total_timeout_seconds": 120})
    ok, reason = reserve_attempt(state, limits=limits, now=1000.0)
    assert ok and reason == ""
    assert state["attempts"] == 1
    assert state["status"] == "attempting"
    assert state["attempt_started_at"] == 1000.0
    finish_attempt(state, passed=False, limits=limits, now=1001.0)
    assert state["status"] == STATUS_OBSERVING
    assert state["failure_count"] == 1
    ok, _ = reserve_attempt(state, limits=limits, now=1002.0)
    assert ok
    finish_attempt(state, passed=False, limits=limits, now=1003.0)
    assert state["status"] == STATUS_AWAITING_USER
    assert state["failure_count"] == 2
    assert is_paused(state)
    # 已暂停后不预占
    ok, reason = reserve_attempt(state, limits=limits, now=1004.0)
    assert not ok
    assert "2/2" in reason
    assert "resume" in reason


def test_max_attempts_zero_direct_manual():
    state = _session(max_attempts=0)
    limits = effective_limits(state, {})
    ok, reason = reserve_attempt(state, limits=limits, now=1001.0)
    assert not ok
    assert state["status"] == STATUS_AWAITING_USER
    assert "0/0" in reason
    assert state["attempts"] == 0


def test_total_timeout_blocks_and_enters_awaiting():
    state = _session(total_timeout_seconds=120)
    limits = effective_limits(state, {})
    ok, _ = reserve_attempt(state, limits=limits, now=1000.0)
    assert ok
    assert not session_timeout_reached(state, limits=limits, now=1119.0)
    assert session_timeout_reached(state, limits=limits, now=1120.0)
    ok, reason = reserve_attempt(state, limits=limits, now=1121.0)
    assert not ok
    assert state["status"] == STATUS_AWAITING_USER
    assert state["last_reason"] == "timeout"
    assert "验证已暂停" in reason


def test_effective_limits_lowering_only():
    state = _session(max_attempts=5, total_timeout_seconds=300)
    limits = effective_limits(state, {"max_attempts": 2, "total_timeout_seconds": 120})
    assert limits["max_attempts"] == 2
    assert limits["total_timeout_seconds"] == 120
    limits = effective_limits(state, {"max_attempts": 9, "total_timeout_seconds": 999})
    assert limits["max_attempts"] == 5
    assert limits["total_timeout_seconds"] == 300


def test_finish_attempt_passed_closes_session():
    state = _session()
    limits = effective_limits(state, {})
    reserve_attempt(state, limits=limits, now=1000.0)
    finish_attempt(state, passed=True, limits=limits, now=1001.0)
    assert state["status"] == STATUS_PASSED
    assert state["attempt_started_at"] == 0.0
    assert is_closed(state)
    assert is_terminal(state)
    assert not is_active(state)
    assert not is_paused(state)


def test_enter_checking_and_finish():
    state = _session()
    apply_awaiting(state, reason="attempts", now=1002.0)
    assert state["status"] == STATUS_AWAITING_USER
    enter_checking(state, now=1003.0)
    assert state["status"] == STATUS_CHECKING
    assert is_paused(state)
    finish_checking(state, passed=False, now=1004.0)
    assert state["status"] == STATUS_AWAITING_USER
    finish_checking(state, passed=True, now=1005.0)
    assert state["status"] == STATUS_PASSED
    assert is_closed(state)


def test_manual_wait_expiry_keeps_pause_until_cancel():
    state = _session(manual_wait_timeout_seconds=60)
    apply_awaiting(state, reason="attempts", now=1000.0)
    limits = effective_limits(state, {})
    assert not manual_wait_expired(state, limits=limits, now=1030.0)
    assert manual_wait_expired(state, limits=limits, now=1061.0)
    mark_expired(state, now=1061.0)
    assert state["status"] == STATUS_EXPIRED
    # 到期仍属于暂停：不是终态、仍需显式取消
    assert is_paused(state)
    assert not is_closed(state)
    assert not is_terminal(state)
    mark_cancelled(state, reason="user_cancel", now=1062.0)
    assert state["status"] == STATUS_CANCELLED
    assert is_closed(state)
    assert not is_paused(state)


def test_cancel_and_notify_states():
    state = _session()
    apply_awaiting(state, reason="attempts", now=1000.0)
    assert should_notify(state)
    mark_notify(state, state_name=NOTIFY_SENT, now=1001.0)
    assert state["notify"]["state"] == NOTIFY_SENT
    assert not should_notify(state)
    mark_cancelled(state, reason="user_cancel")
    assert state["status"] == STATUS_CANCELLED
    assert not should_notify(state)


def test_notify_retry_limit():
    state = _session()
    apply_awaiting(state, reason="attempts", now=1000.0)
    for _ in range(3):
        assert should_notify(state)
        mark_notify(state, state_name=NOTIFY_FAILED, error="no target")
    assert not should_notify(state)


def test_build_message_and_status_text():
    state = _session(max_attempts=3)
    limits = effective_limits(state, {})
    for index in range(3):
        reserve_attempt(state, limits=limits, now=1000.0 + index)
        finish_attempt(state, passed=False, limits=limits, now=1000.5 + index)
    assert state["status"] == STATUS_AWAITING_USER
    assert state["failure_count"] == 3
    text = build_awaiting_message(state, limits=limits)
    assert "验证页面卡住了" in text
    assert "/phone verify resume ver-" in text
    assert "/phone verify cancel" in text
    rendered = status_text(state)
    assert "awaiting_user" in rendered
    assert "失败：3/3" in rendered
    assert status_text({}) == "验证与人工协助：当前没有进行中的验证会话"


def test_expired_status_text_shows_cancel_hint():
    state = _session(manual_wait_timeout_seconds=60)
    apply_awaiting(state, reason="attempts", now=1000.0)
    mark_expired(state, now=1100.0)
    rendered = status_text(state)
    assert "expired" in rendered
    assert "/phone verify cancel" in rendered
    assert "仍暂停" in rendered


def test_elapsed_and_remaining():
    state = _session(now=1000.0)
    limits = effective_limits(state, {})
    assert elapsed_ms(state, now=1001.5) == 1500
    assert remaining_ms(state, limits, now=1001.5) == 118500
    assert remaining_ms(state, limits, now=9999.0) == 0


def test_challenge_present_keywords():
    assert challenge_present("请完成验证后继续")
    assert challenge_present("拖动滑块完成拼图")
    for keyword in CHALLENGE_KEYWORDS[:3]:
        assert challenge_present(keyword)
    assert not challenge_present("首页推荐视频列表")


def test_store_roundtrip_and_corruption(tmp_path):
    store = VerificationStore(tmp_path)
    assert store.load() == {}
    state = _session()
    assert store.save(state)
    loaded = store.load()
    assert loaded["id"] == state["id"]
    assert loaded["status"] == state["status"]
    assert store.clear()
    assert store.load() == {}
    store.path.write_text("not-json", encoding="utf-8")
    # 兼容包装仍返回空状态，但 load_result 必须报告解析故障而不是「没有状态」
    assert store.load() == {}
    result = store.load_result()
    assert result.kind == STORE_LOAD_PARSE_ERROR
    assert result.fault is True
    assert result.state == {}
    assert store.path.read_text(encoding="utf-8") == "not-json"


# ----------------------------------------------------------------------
# VerificationStore.load_result：区分首次缺文件/合法空状态/读取故障
# ----------------------------------------------------------------------


def test_store_load_result_missing_file_is_normal_empty(tmp_path):
    store = VerificationStore(tmp_path / "new-data-dir")
    result = store.load_result()
    assert result.kind == STORE_LOAD_MISSING
    assert result.state == {}
    assert result.reason == ""
    assert result.fault is False
    assert store.load() == {}


def test_store_load_result_legal_empty_state(tmp_path):
    store = VerificationStore(tmp_path)
    assert store.save({})
    result = store.load_result()
    assert result.kind == STORE_LOAD_EMPTY
    assert result.state == {}
    assert result.reason == ""
    assert result.fault is False


def test_store_load_result_valid_state(tmp_path):
    store = VerificationStore(tmp_path)
    state = _session()
    state["status"] = STATUS_AWAITING_USER
    assert store.save(state)
    result = store.load_result()
    assert result.kind == STORE_LOAD_OK
    assert result.fault is False
    assert result.state["id"] == state["id"]


def test_store_load_result_corrupt_json_keeps_file_and_reports_fault(tmp_path):
    store = VerificationStore(tmp_path)
    store.path.write_text('{"id":', encoding="utf-8")
    result = store.load_result()
    assert result.kind == STORE_LOAD_PARSE_ERROR
    assert result.state == {}
    assert "JSON 无法解析" in result.reason
    assert "阻断" in result.reason
    assert "/phone verify cancel" in result.reason
    assert result.fault is True
    assert store.path.read_text(encoding="utf-8") == '{"id":'


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("[]", STORE_LOAD_STRUCTURE_ERROR),
        ('{"foo": 1}', STORE_LOAD_STRUCTURE_ERROR),
        ('{"id": 123, "status": "observing"}', STORE_LOAD_STRUCTURE_ERROR),
        ('{"id": "", "status": "observing"}', STORE_LOAD_STRUCTURE_ERROR),
        ('{"id": "ver-1", "status": "bogus"}', STORE_LOAD_STRUCTURE_ERROR),
        ('{"id": "ver-1", "status": "observing", "attempts": "oops"}', STORE_LOAD_STRUCTURE_ERROR),
        ('{"id": "ver-1", "status": "observing", "notify": []}', STORE_LOAD_STRUCTURE_ERROR),
    ],
)
def test_store_load_result_structure_failures(tmp_path, payload, expected):
    store = VerificationStore(tmp_path)
    store.path.write_text(payload, encoding="utf-8")
    result = store.load_result()
    assert result.kind == expected
    assert result.fault is True
    assert "结构校验失败" in result.reason
    assert result.state == {}
    # 故障文件保持原样，不被读取路径改动
    assert store.path.read_text(encoding="utf-8") == payload


def test_store_load_result_read_permission_error(tmp_path, monkeypatch):
    store = VerificationStore(tmp_path)
    state = _session()
    assert store.save(state)

    def _deny_read(*_args, **_kwargs):
        raise PermissionError("permission denied (test double)")

    monkeypatch.setattr(type(store.path), "read_text", _deny_read)
    result = store.load_result()
    assert result.kind == STORE_LOAD_READ_ERROR
    assert result.state == {}
    assert "读取失败" in result.reason
    assert "阻断" in result.reason
    assert result.fault is True
    # 文件仍保留（未被删除/覆盖）
    assert store.path.exists()


def test_store_load_result_read_error_on_directory(tmp_path):
    store = VerificationStore(tmp_path)
    store.path.mkdir()
    result = store.load_result()
    assert result.kind == STORE_LOAD_READ_ERROR
    assert result.fault is True


def test_store_quarantine_preserves_original_file(tmp_path):
    store = VerificationStore(tmp_path)
    store.path.write_text('{"id":', encoding="utf-8")
    backup = store.quarantine()
    assert backup is not None
    assert backup.parent == tmp_path
    assert backup.name.startswith("verification.json.corrupt-")
    assert backup.read_text(encoding="utf-8") == '{"id":'
    assert not store.path.exists()
    # 处置后重新读取等价于首次没有文件
    assert store.load_result().kind == STORE_LOAD_MISSING
    # 没有文件时 quarantine 不产生备份
    assert store.quarantine() is None


def test_store_load_faults_are_exactly_the_actionable_kinds():
    assert STORE_LOAD_FAULTS == {
        STORE_LOAD_READ_ERROR,
        STORE_LOAD_PARSE_ERROR,
        STORE_LOAD_STRUCTURE_ERROR,
    }
    assert STORE_LOAD_MISSING not in STORE_LOAD_FAULTS
    assert STORE_LOAD_EMPTY not in STORE_LOAD_FAULTS
    assert STORE_LOAD_OK not in STORE_LOAD_FAULTS


def test_mark_helpers():
    state = _session()
    mark_passed(state, reason="ok")
    assert state["status"] == STATUS_PASSED
    assert is_closed(state)
    # 已关闭的会话不会被再次改写
    mark_cancelled(state, reason="late")
    assert state["status"] == STATUS_PASSED
    state = _session()
    mark_cancelled(state)
    assert state["status"] == STATUS_CANCELLED
    assert is_closed(state)
    state = _session()
    mark_expired(state)
    assert state["status"] == STATUS_EXPIRED
    assert is_paused(state)
    assert state["notify"]["state"] == NOTIFY_NONE


# ======================================================================
# PhoneVerification 控制器级回归
# ======================================================================


def _install_controller_import_stubs() -> bool:
    """为 ``PhoneVerification`` 的导入补齐最小 AstrBot 符号（本地无 AstrBot 时）。

    容器内真实 AstrBot 存在时直接使用真实模块；这里只补 ``astrbot.api.event``
    与 ``astrbot.core.platform.message_session`` 两个导入依赖，不模拟业务行为。
    """
    try:
        import astrbot.api  # noqa: F401
    except ImportError:
        return False
    try:
        from astrbot.api.event import MessageChain  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺失则注册最小替身
        module = types.ModuleType("astrbot.api.event")
        module._acat_local_stub = True

        class _MessageChain:
            def message(self, *_args, **_kwargs):
                return self

        module.MessageChain = _MessageChain
        sys.modules["astrbot.api.event"] = module
        try:
            setattr(sys.modules["astrbot.api"], "event", module)
        except Exception:  # noqa: BLE001 - 桩包不支持属性时忽略
            pass
    try:
        from astrbot.core.platform.message_session import MessageSession  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺失则注册最小替身
        core = sys.modules.get("astrbot.core")
        if core is None:
            core = types.ModuleType("astrbot.core")
            core.__path__ = []
            sys.modules["astrbot.core"] = core
        platform = sys.modules.get("astrbot.core.platform")
        if platform is None:
            platform = types.ModuleType("astrbot.core.platform")
            platform.__path__ = []
            sys.modules["astrbot.core.platform"] = platform
        module = types.ModuleType("astrbot.core.platform.message_session")
        module._acat_local_stub = True

        class _MessageSession:
            def __init__(self, target: str = "") -> None:
                self.target = str(target or "")

            @classmethod
            def from_str(cls, value: str) -> "_MessageSession":
                return cls(value)

            def __str__(self) -> str:
                return self.target

        module.MessageSession = _MessageSession
        sys.modules["astrbot.core.platform.message_session"] = module
        try:
            setattr(platform, "message_session", module)
        except Exception:  # noqa: BLE001 - 桩包不支持属性时忽略
            pass
    return True


try:
    _CONTROLLER_IMPORT_OK = _install_controller_import_stubs()
    if _CONTROLLER_IMPORT_OK:
        from core.verification_controller import PhoneVerification  # noqa: E402
except Exception:  # noqa: BLE001 - 无法导入时跳过控制器级用例
    _CONTROLLER_IMPORT_OK = False
    PhoneVerification = None  # type: ignore[assignment]

requires_controller = pytest.mark.skipif(
    not _CONTROLLER_IMPORT_OK, reason="缺少 AstrBot 依赖，跳过控制器级用例"
)


class _FakeVerificationConfig:
    max_attempts = 3
    total_timeout_seconds = 120
    attempt_timeout_seconds = 30
    manual_wait_timeout_seconds = 600
    notify_target = ""


class _FakeActions:
    async def foreground(self) -> tuple[str, str]:
        return ("com.example.app", "com.example.app.MainActivity")


class _FakeAdb:
    serial = "fake-serial"


class _FakeService:
    def __init__(self) -> None:
        self.config = types.SimpleNamespace(verification=_FakeVerificationConfig())
        self.actions = _FakeActions()
        self.adb = _FakeAdb()

    def is_autonomous_play(self, _event) -> bool:
        return False

    def is_task_session(self, _event) -> bool:
        return False

    def last_session(self) -> str:
        return ""


class _FakePlugin:
    """控制器所需的最小插件替身（无设备、无 AstrBot 运行时）。"""

    def __init__(self, data_dir) -> None:
        from pathlib import Path

        self.data_dir = Path(data_dir)
        self.service = _FakeService()
        self.context = None
        self.device_gate = None
        self._continuous_enabled = False
        self.logs: list[str] = []

    def _device_log(self, message: str) -> None:
        self.logs.append(str(message))

    async def _stop_live_screen(self, _reason: str = "") -> None:
        return None

    def _start_random_play_loop(self) -> None:
        return None

    def _start_screenshot_cleanup_loop(self) -> None:
        return None

    def _start_continuous_loop(self) -> None:
        return None


def _make_controller(tmp_path):
    plugin = _FakePlugin(tmp_path)
    return plugin, PhoneVerification(plugin)


def _fake_event(umo: str = "webchat:FriendMessage:one"):
    return types.SimpleNamespace(
        unified_msg_origin=umo,
        get_extra=lambda _name, default=None: default,
    )


def _run(coro):
    return asyncio.run(coro)


@requires_controller
def test_controller_cancel_signals_in_flight_before_rotating_event(tmp_path):
    async def scenario() -> None:
        _plugin, controller = _make_controller(tmp_path)
        event = _fake_event()
        ok, _reason = await controller.reserve_attempt(event, kind="solver")
        assert ok
        old_event = controller.cancelled()
        session_id = controller.session_id
        await controller.cancel(session_id, event, restart_schedulers=False)
        # 旧执行必须先收到取消信号，之后才换新 Event
        assert old_event.is_set()
        assert controller.cancelled() is not old_event
        assert controller.state["status"] == STATUS_CANCELLED

    _run(scenario())


@requires_controller
def test_controller_old_session_outcome_rejected_after_new_attempt(tmp_path):
    """A13 重放：取消旧验证 → 新会话预占 → 旧结果返回不得写入新会话。"""

    async def scenario() -> None:
        _plugin, controller = _make_controller(tmp_path)
        first_event = _fake_event()
        ok, _reason = await controller.reserve_attempt(first_event, kind="solver")
        assert ok
        old_identity = controller.reservation_identity()
        old_session_id = controller.session_id
        await controller.cancel(old_session_id, first_event, restart_schedulers=False)

        new_event = _fake_event("webchat:FriendMessage:new")
        allowed, _reason = await controller.reserve_attempt(new_event, kind="solver")
        assert allowed
        new_identity = controller.reservation_identity()
        assert new_identity.session_id != old_identity.session_id
        # 新旧会话的取消代次都从 0 开始：仅凭代次无法区分，必须靠会话身份
        assert new_identity.generation == old_identity.generation == 0

        stale = SolveOutcome(
            OUTCOME_CLEARED,
            kind=SOLVE_KIND_CAPTCHA,
            evidence="旧求解返回",
            generation=old_identity.generation,
            session_id=old_identity.session_id,
            attempt_id=old_identity.attempt_id,
        )
        notice = await controller.submit_outcome(new_event, stale)
        assert notice == ""
        assert controller.session_id == new_identity.session_id
        assert controller.state["status"] != STATUS_PASSED
        assert controller.state["status"] in {STATUS_ATTEMPTING, STATUS_OBSERVING}

    _run(scenario())


@requires_controller
def test_controller_cancel_and_completion_same_event_loop_cycle(tmp_path):
    """取消与完成同一事件循环周期：旧结果在 cancel 后立即提交必须被拒。"""

    async def scenario() -> None:
        _plugin, controller = _make_controller(tmp_path)
        event = _fake_event()
        ok, _reason = await controller.reserve_attempt(event, kind="solver")
        assert ok
        identity = controller.reservation_identity()
        await controller.cancel(controller.session_id, event, restart_schedulers=False)
        # 无中间 tick：Cancel 后同一周期立即提交旧执行结果
        stale = SolveOutcome(
            OUTCOME_CLEARED,
            kind=SOLVE_KIND_CAPTCHA,
            evidence="同周期完成",
            generation=identity.generation,
            session_id=identity.session_id,
            attempt_id=identity.attempt_id,
        )
        notice = await controller.submit_outcome(event, stale)
        assert notice == ""
        assert controller.state["status"] == STATUS_CANCELLED
        assert controller.paused is False

    _run(scenario())


@requires_controller
def test_controller_submit_generation_zero_is_compared(tmp_path):
    """代次 0 也参与校验：会话/尝试匹配但代次变化时结果被拒。"""

    async def scenario() -> None:
        _plugin, controller = _make_controller(tmp_path)
        event = _fake_event()
        ok, _reason = await controller.reserve_attempt(event, kind="solver")
        assert ok
        identity = controller.reservation_identity()
        assert identity.generation == 0
        # 模拟取消代次已变化（如暂停后再恢复），但会话/尝试身份仍相同
        controller.state["cancel_generation"] = 1
        outcome = SolveOutcome(
            OUTCOME_CLEARED,
            kind=SOLVE_KIND_CAPTCHA,
            evidence="代次不一致",
            generation=0,
            session_id=identity.session_id,
            attempt_id=identity.attempt_id,
        )
        notice = await controller.submit_outcome(event, outcome)
        assert notice == ""
        assert controller.state["status"] != STATUS_PASSED

    _run(scenario())


@requires_controller
def test_controller_submit_attempt_identity_is_compared(tmp_path):
    """尝试序号不匹配（旧尝试结果）被拒。"""

    async def scenario() -> None:
        _plugin, controller = _make_controller(tmp_path)
        event = _fake_event()
        ok, _reason = await controller.reserve_attempt(event, kind="solver")
        assert ok
        identity = controller.reservation_identity()
        # 模拟同一会话已进入下一次尝试
        controller.state["attempts"] = identity.attempt_id + 1
        outcome = SolveOutcome(
            OUTCOME_CLEARED,
            kind=SOLVE_KIND_CAPTCHA,
            evidence="旧尝试结果",
            generation=identity.generation,
            session_id=identity.session_id,
            attempt_id=identity.attempt_id,
        )
        assert await controller.submit_outcome(event, outcome) == ""
        assert controller.state["status"] != STATUS_PASSED

    _run(scenario())


@requires_controller
def test_controller_submit_matching_identity_passes(tmp_path):
    """身份完全匹配的结果正常关闭会话（正向对照）。"""

    async def scenario() -> None:
        _plugin, controller = _make_controller(tmp_path)
        event = _fake_event()
        ok, _reason = await controller.reserve_attempt(event, kind="solver")
        assert ok
        identity = controller.reservation_identity()
        outcome = SolveOutcome(
            OUTCOME_CLEARED,
            kind=SOLVE_KIND_CAPTCHA,
            evidence="验证成功",
            generation=identity.generation,
            session_id=identity.session_id,
            attempt_id=identity.attempt_id,
        )
        notice = await controller.submit_outcome(event, outcome)
        assert notice  # 返回状态文本
        assert controller.state["status"] == STATUS_PASSED

    _run(scenario())


@requires_controller
def test_controller_load_fault_blocks_and_requires_explicit_dispose(tmp_path):
    """A11 重放：状态文件损坏 → 新控制器报告故障并阻断写操作；显式 cancel 才处置。"""

    async def scenario() -> None:
        plugin, controller = _make_controller(tmp_path)
        event = _fake_event()
        controller.state = new_session(now=1000.0)
        apply_awaiting(controller.state, reason="review", now=1001.0)
        assert controller._save()
        controller.store.path.write_text('{"id":', encoding="utf-8")

        fresh = PhoneVerification(type(plugin)(tmp_path))
        assert fresh.state == {}
        health = fresh.health_reason()
        assert health
        assert "JSON 无法解析" in health
        assert "阻断" in health
        assert "/phone verify cancel" in health
        # 写操作被阻断（A11 验收口径）
        denied = fresh.guard_tool("phone_tap", write=True)
        assert denied
        assert "阻断" in denied or "损坏" in denied
        assert fresh.guard_command("auto")  # 命令层同样阻断
        # 故障期间不写盘、不覆盖原文件
        assert fresh._save() is False
        assert fresh.store.path.read_text(encoding="utf-8") == '{"id":'

        # 停止命令不静默处置
        stop_note = await fresh.cancel_for_stop()
        assert "/phone verify cancel" in stop_note
        assert fresh.load_fault() is True
        assert fresh.store.path.exists()

        result = await fresh.cancel("any-id", event)
        assert "已保留" in result
        assert fresh.health_reason() == ""
        assert fresh.guard_tool("phone_tap", write=True) == ""
        backups = list(tmp_path.glob("verification.json.corrupt-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == '{"id":'
        assert not fresh.store.path.exists()

        # 处置后重新构造控制器：首次缺文件属正常空状态
        clean = PhoneVerification(type(plugin)(tmp_path))
        assert clean.state == {}
        assert clean.health_reason() == ""
        assert clean.guard_tool("phone_tap", write=True) == ""

    _run(scenario())


@requires_controller
def test_controller_legal_empty_state_is_not_a_fault(tmp_path):
    async def scenario() -> None:
        plugin = _FakePlugin(tmp_path)
        store = VerificationStore(tmp_path)
        assert store.save({})
        controller = PhoneVerification(plugin)
        assert controller.state == {}
        assert controller.health_reason() == ""
        assert controller.guard_tool("phone_tap", write=True) == ""

    _run(scenario())


@requires_controller
def test_controller_stop_does_not_dispose_load_fault(tmp_path):
    async def scenario() -> None:
        plugin = _FakePlugin(tmp_path)
        controller = PhoneVerification(plugin)
        controller.store.path.write_text("[broken", encoding="utf-8")
        broken = PhoneVerification(plugin)
        assert broken.load_fault() is True
        note = await broken.cancel("", dispose_load_fault=False)
        assert broken.load_fault() is True
        assert broken.store.path.read_text(encoding="utf-8") == "[broken"
        assert "阻断" in note or "损坏" in note

    _run(scenario())


# ======================================================================
# 提交身份纯函数（不依赖 AstrBot）
# ======================================================================


def _identity_state(**overrides):
    state = _session()
    state["status"] = STATUS_ATTEMPTING
    state["attempts"] = 2
    state["cancel_generation"] = 0
    state.update(overrides)
    return state


def test_submission_mismatch_accepts_matching_identity():
    state = _identity_state()
    identity = current_identity(state)
    outcome = SolveOutcome(
        OUTCOME_CLEARED,
        generation=identity.generation,
        session_id=identity.session_id,
        attempt_id=identity.attempt_id,
    )
    assert submission_mismatch_reason(state, outcome) == ""


def test_submission_mismatch_rejects_stale_session_with_generation_zero():
    state = _identity_state()
    outcome = SolveOutcome(
        OUTCOME_CLEARED, generation=0, session_id="ver-old", attempt_id=2
    )
    reason = submission_mismatch_reason(state, outcome)
    assert "会话" in reason


def test_submission_mismatch_rejects_generation_zero_change():
    state = _identity_state(cancel_generation=1)
    identity = current_identity(state)
    outcome = SolveOutcome(
        OUTCOME_CLEARED,
        generation=0,
        session_id=identity.session_id,
        attempt_id=identity.attempt_id,
    )
    reason = submission_mismatch_reason(state, outcome)
    assert "代次" in reason


def test_submission_mismatch_rejects_stale_attempt():
    state = _identity_state(attempts=3)
    identity = current_identity(state)
    outcome = SolveOutcome(
        OUTCOME_CLEARED,
        generation=identity.generation,
        session_id=identity.session_id,
        attempt_id=2,
    )
    assert "尝试" in submission_mismatch_reason(state, outcome)


def test_submission_mismatch_rejects_paused_closed_or_expired_attempt():
    state = _identity_state()
    identity = current_identity(state)
    outcome = SolveOutcome(
        OUTCOME_CLEARED,
        generation=identity.generation,
        session_id=identity.session_id,
        attempt_id=identity.attempt_id,
    )
    paused_state = _identity_state()
    apply_awaiting(paused_state, reason="attempts", now=1001.0)
    assert "暂停" in submission_mismatch_reason(paused_state, outcome)
    closed_state = _identity_state()
    mark_passed(closed_state, now=1001.0)
    assert "结束" in submission_mismatch_reason(closed_state, outcome)
    observing_state = _identity_state(status=STATUS_OBSERVING, attempt_started_at=0.0)
    assert "结束" in submission_mismatch_reason(observing_state, outcome)
    assert "没有" in submission_mismatch_reason({}, outcome)
