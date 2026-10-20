"""core.task_priority 单元测试：用户任务活跃登记与让路/暂停/恢复判定。"""

from __future__ import annotations

import pytest

from core.task_priority import (
    AUTONOMOUS_ORIGINS,
    UserTaskPriority,
    is_autonomous_origin,
    should_pause_autonomous,
    should_resume_continuous,
    should_yield_to_user,
)

# 非自主任务 origin：用户会话空 origin、旧写法、未知值与空白变体。
NON_AUTONOMOUS_ORIGINS = [
    None,
    "",
    "   ",
    "play",
    "phone",
    "phone_wait",
    "phone_screen",
    "PHONE_PLAY",
    " phone_play",
    "phone_play ",
    "unknown",
]


# ----------------------------------------------------------------------
# 常量与 origin 判定
# ----------------------------------------------------------------------


def test_autonomous_origins_is_frozen_set():
    assert isinstance(AUTONOMOUS_ORIGINS, frozenset)
    assert AUTONOMOUS_ORIGINS == frozenset(
        {
            "phone_play",
            "phone_random_play",
            "phone_background_task",
            "phone_continuous",
        }
    )
    assert len(AUTONOMOUS_ORIGINS) == 4


@pytest.mark.parametrize("origin", sorted(AUTONOMOUS_ORIGINS))
def test_is_autonomous_origin_known(origin):
    assert is_autonomous_origin(origin) is True


@pytest.mark.parametrize("origin", NON_AUTONOMOUS_ORIGINS)
def test_is_autonomous_origin_non_autonomous(origin):
    assert is_autonomous_origin(origin) is False


@pytest.mark.parametrize("origin", [0, 1, 3.14, b"phone_play", ["phone_play"], {}])
def test_is_autonomous_origin_rejects_non_string(origin):
    # origin 只接受字符串，其他类型一律视为未知任务（不能碰巧命中集合）
    assert is_autonomous_origin(origin) is False


# ----------------------------------------------------------------------
# should_yield_to_user：自主任务是否让路
# ----------------------------------------------------------------------


@pytest.mark.parametrize("origin", sorted(AUTONOMOUS_ORIGINS))
@pytest.mark.parametrize("user_active", [True, False])
def test_should_yield_to_user_matrix(origin, user_active):
    assert should_yield_to_user(origin=origin, user_active=user_active) is user_active


@pytest.mark.parametrize("origin", NON_AUTONOMOUS_ORIGINS)
@pytest.mark.parametrize("user_active", [True, False])
def test_should_yield_to_user_never_yields_user_origins(origin, user_active):
    # 用户自己的任务（含空 origin/未知 origin）永远不需要让路
    assert should_yield_to_user(origin=origin, user_active=user_active) is False


# ----------------------------------------------------------------------
# should_pause_autonomous：自主任务循环是否暂停创建新轮次
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "continuous_enabled",
        "continuous_job_active",
        "random_enabled",
    ),
    [
        (True, False, False),
        (True, True, False),
        (False, True, True),
        (False, False, True),
        (True, True, True),
        (False, False, False),
    ],
)
def test_should_pause_autonomous_user_active_always_pauses(
    continuous_enabled, continuous_job_active, random_enabled
):
    assert (
        should_pause_autonomous(
            user_active=True,
            continuous_enabled=continuous_enabled,
            continuous_job_active=continuous_job_active,
            random_enabled=random_enabled,
        )
        is True
    )


@pytest.mark.parametrize(
    ("continuous_enabled", "random_enabled"),
    [
        (True, False),
        (False, True),
        (True, True),
    ],
)
def test_should_pause_autonomous_active_job_waits(continuous_enabled, random_enabled):
    # 用户任务不在，但已有一轮自主任务在途：按原逻辑等它结束，不叠加新轮次
    assert (
        should_pause_autonomous(
            user_active=False,
            continuous_enabled=continuous_enabled,
            continuous_job_active=True,
            random_enabled=random_enabled,
        )
        is True
    )


@pytest.mark.parametrize(
    ("continuous_enabled", "random_enabled"),
    [
        (True, False),
        (False, True),
        (True, True),
    ],
)
def test_should_pause_autonomous_enabled_without_pending_round_continues(
    continuous_enabled, random_enabled
):
    # 用户任务不在、无在途轮次且至少一种自主模式开启：交回调用方原逻辑继续调度
    assert (
        should_pause_autonomous(
            user_active=False,
            continuous_enabled=continuous_enabled,
            continuous_job_active=False,
            random_enabled=random_enabled,
        )
        is False
    )


def test_should_pause_autonomous_all_disabled_pauses():
    # 持续刷与随机都未开启且无在途轮次：没有可调度的自主任务
    assert (
        should_pause_autonomous(
            user_active=False,
            continuous_enabled=False,
            continuous_job_active=False,
            random_enabled=False,
        )
        is True
    )


def test_should_pause_autonomous_returns_bool():
    result = should_pause_autonomous(
        user_active=False,
        continuous_enabled=True,
        continuous_job_active=False,
        random_enabled=False,
    )
    assert result is False


# ----------------------------------------------------------------------
# should_resume_continuous：用户任务结束后是否立即恢复持续刷
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("continuous_enabled", "user_active_after_end", "expected"),
    [
        (True, False, True),
        (True, True, False),
        (False, False, False),
        (False, True, False),
    ],
)
def test_should_resume_continuous(
    continuous_enabled, user_active_after_end, expected
):
    assert (
        should_resume_continuous(
            continuous_enabled=continuous_enabled,
            user_active_after_end=user_active_after_end,
        )
        is expected
    )


# ----------------------------------------------------------------------
# UserTaskPriority：用户任务活跃登记
# ----------------------------------------------------------------------


def test_priority_initial_state():
    priority = UserTaskPriority()
    assert priority.active is False
    assert priority.sessions == frozenset()
    assert len(priority) == 0


def test_priority_begin_returns_first_transition_only():
    priority = UserTaskPriority()
    assert priority.begin("session-a") is True  # 无活跃 → 有活跃
    assert priority.active is True
    assert priority.sessions == frozenset({"session-a"})

    # 同会话重复 begin 幂等：不重复计数，也不算新的「首次抢占」
    assert priority.begin("session-a") is False
    assert priority.sessions == frozenset({"session-a"})
    assert len(priority) == 1

    # 已有其他活跃任务时再登记新会话：不算首次抢占
    assert priority.begin("session-b") is False
    assert priority.sessions == frozenset({"session-a", "session-b"})
    assert len(priority) == 2


def test_priority_end_returns_whether_none_remains():
    priority = UserTaskPriority()
    priority.begin("session-a")
    priority.begin("session-b")

    # 还有别的会话活跃：不能触发自动恢复
    assert priority.end("session-a") is False
    assert priority.active is True
    assert priority.sessions == frozenset({"session-b"})

    # 最后一个用户任务结束：可以自动恢复持续刷
    assert priority.end("session-b") is True
    assert priority.active is False
    assert priority.sessions == frozenset()
    assert len(priority) == 0


def test_priority_end_unknown_session_keeps_state():
    priority = UserTaskPriority()
    priority.begin("session-a")

    # 未知会话不改变状态；仍有活跃任务时返回 False
    assert priority.end("session-unknown") is False
    assert priority.sessions == frozenset({"session-a"})

    # 已无活跃任务时，重复 end/未知 end 不报错
    assert priority.end("session-a") is True
    assert priority.end("session-a") is True
    assert priority.end("session-unknown") is True
    assert priority.active is False


def test_priority_begin_empty_session_is_ignored():
    priority = UserTaskPriority()
    for empty in (None, "", "   "):
        assert priority.begin(empty) is False
        assert priority.end(empty) is True
    assert priority.active is False
    assert priority.sessions == frozenset()


def test_priority_session_key_is_normalized():
    # 会话标识做 str().strip() 归一化，begin/end 使用同一 key
    priority = UserTaskPriority()
    assert priority.begin("  session-a  ") is True
    assert priority.sessions == frozenset({"session-a"})
    assert priority.end("session-a") is True
    assert priority.active is False

    # 非字符串会话转成字符串后登记
    priority.begin(42)
    assert priority.sessions == frozenset({"42"})
    assert priority.end(42) is True


def test_priority_sessions_is_readonly_snapshot():
    priority = UserTaskPriority()
    priority.begin("session-a")
    snapshot = priority.sessions
    priority.begin("session-b")
    assert snapshot == frozenset({"session-a"})
    assert priority.sessions == frozenset({"session-a", "session-b"})
    # frozenset 快照本身不可变
    with pytest.raises(AttributeError):
        snapshot.add("session-c")  # type: ignore[attr-defined]


def test_priority_multi_session_interleaved():
    priority = UserTaskPriority()
    assert priority.begin("a") is True
    assert priority.begin("b") is False
    assert priority.end("a") is False
    assert priority.begin("a") is False  # 仍有 b 活跃，不是首次抢占
    assert priority.end("b") is False
    assert priority.active is True
    assert priority.end("a") is True
    assert priority.active is False


def test_priority_clear():
    priority = UserTaskPriority()
    priority.begin("a")
    priority.begin("b")
    assert priority.active is True

    priority.clear()
    assert priority.active is False
    assert priority.sessions == frozenset()
    assert len(priority) == 0

    # clear 后重新 begin 视为新的首次抢占
    assert priority.begin("a") is True


def test_priority_repr_contains_active_state():
    priority = UserTaskPriority()
    assert "active=False" in repr(priority)
    priority.begin("session-a")
    assert "active=True" in repr(priority)
    assert "session-a" in repr(priority)
