"""结构化结果判定与新状态机原语单测。

覆盖：过期仍暂停（需显式取消）、失败计数与人工等待、答题进度不累计失败、
单次尝试期限 = min(会话剩余, 尝试剩余)、以及 judge_solve_outcome 各分支。
"""

from __future__ import annotations

import math

import pytest

from core.verification import (
    CLOSED_STATUSES,
    OUTCOME_CLEARED,
    OUTCOME_FAILED,
    OUTCOME_NOT_CHALLENGE,
    OUTCOME_PROGRESS,
    OUTCOME_UNKNOWN,
    PAUSED_STATUSES,
    SOLVE_KIND_CAPTCHA,
    SOLVE_KIND_QUIZ,
    STATUS_AWAITING_USER,
    STATUS_EXPIRED,
    STATUS_OBSERVING,
    STATUS_PASSED,
    SolveOutcome,
    apply_outcome,
    attempt_deadline_reached,
    effective_limits,
    execution_deadline_seconds,
    failure_limit_reached,
    finish_attempt,
    is_closed,
    is_paused,
    is_terminal,
    judge_solve_outcome,
    mark_cancelled,
    mark_expired,
    mark_passed,
    new_session,
    question_signature,
    quiz_question_progressed,
    remaining_attempt_seconds,
    reserve_attempt,
    session_timeout_reached,
    status_text,
)


def _session(**kwargs):
    defaults = dict(device="dev", app="B站", origin="auto", now=1000.0)
    defaults.update(kwargs)
    return new_session(**defaults)


def _limits(state, **current):
    return effective_limits(state, current)


# ----------------------------------------------------------------------
# 过期仍暂停：必须显式 cancel 才解除
# ----------------------------------------------------------------------


def test_expired_is_paused_not_closed():
    state = _session(manual_wait_timeout_seconds=60)
    mark_expired(state, now=1100.0)
    assert state["status"] == STATUS_EXPIRED
    assert state["status"] in PAUSED_STATUSES
    assert is_paused(state)
    assert not is_closed(state)
    assert not is_terminal(state)
    assert STATUS_EXPIRED not in CLOSED_STATUSES
    assert "仍暂停" in status_text(state)
    mark_cancelled(state, reason="user_cancel", now=1101.0)
    assert is_closed(state)
    assert not is_paused(state)


def test_expired_reserve_rejected_without_resetting_state():
    state = _session(max_attempts=3, manual_wait_timeout_seconds=60)
    limits = _limits(state)
    reserve_attempt(state, limits=limits, now=1000.0)
    finish_attempt(state, passed=False, limits=limits, now=1001.0)
    mark_expired(state, now=1100.0)
    session_id = state["id"]
    attempts = state["attempts"]
    failures = state["failure_count"]
    ok, reason = reserve_attempt(state, limits=limits, now=1101.0)
    assert not ok
    assert "到期" in reason
    assert f"/phone verify cancel {session_id}" in reason
    # 拒绝时不得清空/重建会话，也不得递增尝试
    assert state["status"] == STATUS_EXPIRED
    assert state["id"] == session_id
    assert state["attempts"] == attempts
    assert state["failure_count"] == failures


def test_expired_ignores_late_outcome():
    state = _session()
    reserve_attempt(state, limits=_limits(state), now=1000.0)
    mark_expired(state, now=1100.0)
    result = apply_outcome(
        state,
        SolveOutcome(OUTCOME_PROGRESS, kind=SOLVE_KIND_QUIZ),
        limits=_limits(state),
        now=1101.0,
    )
    assert result["status"] == STATUS_EXPIRED
    assert result["quiz_answered"] == 0
    assert result["failure_count"] == 0


def test_closed_session_ignores_outcome_and_cancel():
    state = _session()
    reserve_attempt(state, limits=_limits(state), now=1000.0)
    apply_outcome(state, SolveOutcome(OUTCOME_CLEARED), now=1001.0)
    assert state["status"] == STATUS_PASSED
    apply_outcome(state, SolveOutcome(OUTCOME_FAILED), now=1002.0)
    assert state["status"] == STATUS_PASSED
    assert state["failure_count"] == 0
    mark_cancelled(state, reason="late")
    assert state["status"] == STATUS_PASSED


# ----------------------------------------------------------------------
# 失败计数：只有失败/无法确认累计，达上限或总时限才转人工等待
# ----------------------------------------------------------------------


def test_failure_and_unknown_count_then_awaiting():
    state = _session(max_attempts=2, total_timeout_seconds=300)
    limits = _limits(state)
    ok, _ = reserve_attempt(state, limits=limits, now=1000.0)
    assert ok
    apply_outcome(
        state, SolveOutcome(OUTCOME_FAILED, kind=SOLVE_KIND_CAPTCHA), limits=limits, now=1001.0
    )
    assert state["failure_count"] == 1
    assert state["status"] == STATUS_OBSERVING
    assert not is_paused(state)
    ok, _ = reserve_attempt(state, limits=limits, now=1002.0)
    assert ok
    apply_outcome(
        state, SolveOutcome(OUTCOME_UNKNOWN, kind=SOLVE_KIND_QUIZ), limits=limits, now=1003.0
    )
    assert state["failure_count"] == 2
    assert state["status"] == STATUS_AWAITING_USER
    assert state["last_reason"] == "attempts"
    assert is_paused(state)
    ok, reason = reserve_attempt(state, limits=limits, now=1004.0)
    assert not ok
    assert "2/2" in reason


def test_total_timeout_converts_failure_to_awaiting():
    state = _session(max_attempts=5, total_timeout_seconds=30)
    limits = _limits(state)
    reserve_attempt(state, limits=limits, now=1000.0)
    apply_outcome(
        state, SolveOutcome(OUTCOME_UNKNOWN), limits=limits, now=1031.0
    )
    assert state["failure_count"] == 1
    assert state["status"] == STATUS_AWAITING_USER
    assert state["last_reason"] == "timeout"


def test_failure_limit_helper():
    state = _session(max_attempts=2)
    assert not failure_limit_reached(state)
    state["failure_count"] = 2
    assert failure_limit_reached(state)
    empty = _session(max_attempts=0)
    assert failure_limit_reached(empty)
    assert not failure_limit_reached({})


# ----------------------------------------------------------------------
# 答题进度：连续答对只算进度，不累计失败、不进入人工等待
# ----------------------------------------------------------------------


def test_quiz_progress_does_not_accumulate_failures():
    state = _session(max_attempts=1, total_timeout_seconds=300)
    limits = _limits(state)
    for index in range(3):
        ok, reason = reserve_attempt(
            state, limits=limits, now=1000.0 + index * 2
        )
        assert ok, reason
        apply_outcome(
            state,
            SolveOutcome(
                OUTCOME_PROGRESS,
                kind=SOLVE_KIND_QUIZ,
                evidence=f"进入第 {index + 2} 题",
                question=index + 2,
            ),
            limits=limits,
            now=1001.0 + index * 2,
        )
        assert state["status"] == STATUS_OBSERVING
        assert not is_paused(state)
        assert state["failure_count"] == 0
    assert state["quiz_answered"] == 3
    assert state["attempts"] == 3
    assert state["failure_count"] == 0
    assert not failure_limit_reached(state)
    # 尝试次数超过 max_attempts 也不阻止继续答题（人工等待只看失败次数）
    ok, reason = reserve_attempt(state, limits=limits, now=1010.0)
    assert ok, reason
    # 答题完成：正常关闭，不进入人工等待
    apply_outcome(state, SolveOutcome(OUTCOME_CLEARED), limits=limits, now=1011.0)
    assert state["status"] == STATUS_PASSED
    assert is_closed(state)
    assert state["failure_count"] == 0


def test_quiz_progress_after_failure_still_counts_only_failures():
    state = _session(max_attempts=1, total_timeout_seconds=300)
    limits = _limits(state)
    reserve_attempt(state, limits=limits, now=1000.0)
    apply_outcome(state, SolveOutcome(OUTCOME_FAILED), limits=limits, now=1001.0)
    # 一次失败已达上限（max_attempts=1）→ 人工等待；此前的答题进度保留
    assert state["failure_count"] == 1
    assert state["status"] == STATUS_AWAITING_USER


def test_not_challenge_closes_without_failure():
    state = _session()
    limits = _limits(state)
    reserve_attempt(state, limits=limits, now=1000.0)
    apply_outcome(
        state, SolveOutcome(OUTCOME_NOT_CHALLENGE), limits=limits, now=1001.0
    )
    assert state["status"] == STATUS_PASSED
    assert is_closed(state)
    assert state["failure_count"] == 0
    assert state["attempt_started_at"] == 0.0


def test_unknown_outcome_values_fail_closed():
    state = _session(max_attempts=2)
    limits = _limits(state)
    reserve_attempt(state, limits=limits, now=1000.0)
    apply_outcome(state, SolveOutcome("something_else"), limits=limits, now=1001.0)
    assert state["failure_count"] == 1
    assert state["status"] == STATUS_OBSERVING


# ----------------------------------------------------------------------
# 尝试期限 = min(会话剩余, 单次尝试剩余)
# ----------------------------------------------------------------------


def test_attempt_deadline_primitives():
    state = _session(
        total_timeout_seconds=120, attempt_timeout_seconds=30, now=1000.0
    )
    limits = _limits(state)
    # 无尝试：尝试剩余 0；执行期限只受会话总时限约束
    assert remaining_attempt_seconds(state, limits=limits, now=1010.0) == 0.0
    assert not attempt_deadline_reached(state, limits=limits, now=1010.0)
    assert execution_deadline_seconds(state, limits=limits, now=1010.0) == pytest.approx(110.0)
    ok, _ = reserve_attempt(state, limits=limits, now=1000.0)
    assert ok
    assert state["attempt_started_at"] == 1000.0
    # 10 秒后：尝试剩余 20s，执行期限取 min(20, 110)=20
    assert remaining_attempt_seconds(state, limits=limits, now=1010.0) == pytest.approx(20.0)
    assert execution_deadline_seconds(state, limits=limits, now=1010.0) == pytest.approx(20.0)
    assert not attempt_deadline_reached(state, limits=limits, now=1029.0)
    assert attempt_deadline_reached(state, limits=limits, now=1030.0)
    assert remaining_attempt_seconds(state, limits=limits, now=1031.0) == 0.0
    assert execution_deadline_seconds(state, limits=limits, now=1031.0) == 0.0


def test_attempt_deadline_capped_by_session_remaining():
    state = _session(total_timeout_seconds=30, attempt_timeout_seconds=60, now=1000.0)
    limits = _limits(state)
    reserve_attempt(state, limits=limits, now=1000.0)
    # 会话剩余 10s 先到：尝试剩余被会话总剩余截断
    assert remaining_attempt_seconds(state, limits=limits, now=1020.0) == pytest.approx(10.0)
    assert attempt_deadline_reached(state, limits=limits, now=1029.0) is False
    assert attempt_deadline_reached(state, limits=limits, now=1030.0) is True
    assert session_timeout_reached(state, limits=limits, now=1030.0)


def test_attempt_deadline_resets_after_outcome():
    state = _session(total_timeout_seconds=300, attempt_timeout_seconds=30, now=1000.0)
    limits = _limits(state)
    reserve_attempt(state, limits=limits, now=1000.0)
    apply_outcome(state, SolveOutcome(OUTCOME_PROGRESS), limits=limits, now=1005.0)
    assert state["attempt_started_at"] == 0.0
    assert remaining_attempt_seconds(state, limits=limits, now=1006.0) == 0.0
    assert not attempt_deadline_reached(state, limits=limits, now=1006.0)
    # 无尝试时执行期限回到会话剩余
    assert execution_deadline_seconds(state, limits=limits, now=1006.0) == pytest.approx(294.0)


def test_execution_deadline_without_session_or_when_closed():
    assert execution_deadline_seconds({}) == math.inf
    state = _session()
    assert execution_deadline_seconds(state, now=1000.0) == pytest.approx(120.0)
    mark_passed(state, now=1001.0)
    assert execution_deadline_seconds(state) == math.inf


def test_attempt_timeout_counts_failure_but_needs_limit_for_manual_wait():
    """单次尝试超时先按无法确认结束；只有失败次数或总时限才转人工等待。"""
    state = _session(max_attempts=2, total_timeout_seconds=300, attempt_timeout_seconds=30)
    limits = _limits(state)
    reserve_attempt(state, limits=limits, now=1000.0)
    assert attempt_deadline_reached(state, limits=limits, now=1031.0)
    apply_outcome(state, SolveOutcome(OUTCOME_UNKNOWN), limits=limits, now=1031.0)
    assert state["failure_count"] == 1
    assert state["status"] == STATUS_OBSERVING
    assert not is_paused(state)
    reserve_attempt(state, limits=limits, now=1032.0)
    assert attempt_deadline_reached(state, limits=limits, now=1063.0)
    apply_outcome(state, SolveOutcome(OUTCOME_UNKNOWN), limits=limits, now=1063.0)
    assert state["failure_count"] == 2
    assert state["status"] == STATUS_AWAITING_USER


# ----------------------------------------------------------------------
# 答题页换题识别与题干指纹
# ----------------------------------------------------------------------


def test_quiz_question_progressed_by_number():
    assert quiz_question_progressed("第1题 选出红色", "第2题 选出蓝色") == (True, 2)
    # 同一题不前进（瞬时提示被归一化剔除）
    assert quiz_question_progressed(
        "第2题 选出蓝色", "第2题 选出蓝色 回答正确"
    ) == (False, 2)
    # 题号消失（可能进入结果页）：不按换题处理
    assert quiz_question_progressed("第3题 选出绿色", "答题结束，得分 90") == (False, 0)


def test_quiz_question_progressed_by_signature():
    assert quiz_question_progressed("选出红色", "选出蓝色") == (True, 0)
    assert quiz_question_progressed("选出红色", "选出红色") == (False, 0)
    # 空文本不产生虚假换题
    assert quiz_question_progressed("", "选出红色") == (False, 0)


def test_question_signature_ignores_number_and_transient_hints():
    first = question_signature("第1题 苹果是什么颜色？ 回答正确")
    second = question_signature("第2题 苹果是什么颜色")
    assert first == second
    assert question_signature("第1题 苹果是什么颜色") != question_signature(
        "第1题 香蕉是什么颜色"
    )
    assert question_signature("") == ""


# ----------------------------------------------------------------------
# judge_solve_outcome 各分支
# ----------------------------------------------------------------------


def test_judge_observation_failed_is_unknown():
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_QUIZ,
        pre_text="第1题 选出红色",
        post_text="",
        observation_ok=False,
        generation=7,
    )
    assert outcome.outcome == OUTCOME_UNKNOWN
    assert outcome.generation == 7
    assert outcome.kind == SOLVE_KIND_QUIZ


@pytest.mark.parametrize(
    "post_text",
    ["验证失败，请重新操作", "请重试", "操作错误", "答案不正确", "再试一次吧"],
)
def test_judge_failure_words(post_text):
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请拖动滑块完成验证",
        post_text=post_text,
        observation_ok=True,
    )
    assert outcome.outcome == OUTCOME_FAILED


@pytest.mark.parametrize(
    "post_text",
    ["验证成功", "已完成验证", "验证通过", "提交成功", "已通过审核"],
)
def test_judge_success_words(post_text):
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请拖动滑块完成验证",
        post_text=post_text,
        observation_ok=True,
    )
    assert outcome.outcome == OUTCOME_CLEARED


def test_judge_success_word_takes_precedence_over_challenge_keyword():
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请拖动滑块完成验证",
        post_text="验证成功，滑块验证已通过",
        observation_ok=True,
    )
    assert outcome.outcome == OUTCOME_CLEARED


def test_judge_challenge_still_present_is_failed():
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请拖动滑块完成验证",
        post_text="请拖动滑块完成验证",
        observation_ok=True,
    )
    assert outcome.outcome == OUTCOME_FAILED
    assert "挑战" in outcome.evidence


def test_judge_captcha_cleared_without_challenge():
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请拖动滑块完成验证",
        post_text="首页推荐视频列表",
        observation_ok=True,
    )
    assert outcome.outcome == OUTCOME_CLEARED


def test_judge_quiz_progress_by_number():
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_QUIZ,
        pre_text="第1题 选出红色",
        post_text="第2题 选出蓝色",
        observation_ok=True,
        generation=3,
    )
    assert outcome.outcome == OUTCOME_PROGRESS
    assert outcome.question == 2
    assert outcome.generation == 3
    assert outcome.kind == SOLVE_KIND_QUIZ


def test_judge_quiz_progress_before_success_word():
    """逐题答对时页面上的“回答正确”只代表本题通过，不能把整场答题判为解除。"""
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_QUIZ,
        pre_text="第1题 选出红色",
        post_text="第2题 选出蓝色 回答正确",
        observation_ok=True,
    )
    assert outcome.outcome == OUTCOME_PROGRESS
    assert outcome.question == 2


def test_judge_quiz_result_page_is_cleared():
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_QUIZ,
        pre_text="第3题 选出绿色",
        post_text="答题结束 你的得分 90",
        observation_ok=True,
    )
    assert outcome.outcome == OUTCOME_CLEARED


def test_judge_quiz_no_change_is_failed():
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_QUIZ,
        pre_text="第1题 选出红色",
        post_text="第1题 选出红色",
        observation_ok=True,
    )
    assert outcome.outcome == OUTCOME_FAILED
    assert "无明显变化" in outcome.evidence


def test_judge_quiz_observation_without_challenge_hint_is_cleared():
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_QUIZ,
        pre_text="第1题 选出红色",
        post_text="第1题 选出红色 已完成",
        observation_ok=True,
    )
    assert outcome.outcome == OUTCOME_CLEARED


def test_judge_not_challenge_is_submitted_by_tool_not_judge():
    """非验证页面由调用方直接构造 not_challenge，判定函数不返回该结果。"""
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_QUIZ,
        pre_text="首页推荐视频",
        post_text="首页推荐视频",
        observation_ok=True,
    )
    assert outcome.outcome == OUTCOME_FAILED


# ----------------------------------------------------------------------
# 判定结果与状态机联动
# ----------------------------------------------------------------------


def test_judge_outcome_applied_to_session():
    state = _session(max_attempts=2, total_timeout_seconds=300)
    limits = _limits(state)
    reserve_attempt(state, limits=limits, now=1000.0)
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请拖动滑块完成验证",
        post_text="验证成功",
        observation_ok=True,
        generation=state["cancel_generation"],
    )
    apply_outcome(state, outcome, limits=limits, now=1001.0)
    assert state["status"] == STATUS_PASSED
    assert is_closed(state)


def test_judge_unknown_applied_counts_failure():
    state = _session(max_attempts=1, total_timeout_seconds=300)
    limits = _limits(state)
    reserve_attempt(state, limits=limits, now=1000.0)
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="验证码",
        post_text="",
        observation_ok=False,
    )
    assert outcome.outcome == OUTCOME_UNKNOWN
    apply_outcome(state, outcome, limits=limits, now=1001.0)
    assert state["failure_count"] == 1
    assert state["status"] == STATUS_AWAITING_USER
    assert is_paused(state)
