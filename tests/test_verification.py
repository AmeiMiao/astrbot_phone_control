"""core.verification 单元测试：尝试计数、失败上限、总时限、人工接管、恢复/取消与持久化。"""

from __future__ import annotations

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
    PAUSED_STATUSES,
    STATUS_AWAITING_USER,
    STATUS_CANCELLED,
    STATUS_CHECKING,
    STATUS_EXPIRED,
    STATUS_OBSERVING,
    STATUS_PASSED,
    TERMINAL_STATUSES,
    VerificationStore,
    apply_awaiting,
    build_awaiting_message,
    challenge_present,
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
    assert store.load() == {}


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
