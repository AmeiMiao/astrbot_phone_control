"""结构化结果判定与新状态机原语单测。

覆盖：过期仍暂停（需显式取消）、失败计数与人工等待、答题进度不累计失败、
单次尝试期限 = min(会话剩余, 尝试剩余)、以及 judge_solve_outcome 各分支。
"""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from core.models import Rect, ScreenSnapshot, UiNode
from core.ui import render_ui
from core.verification import (
    CLOSED_STATUSES,
    OBSERVATION_EMPTY,
    OBSERVATION_LOADING,
    OBSERVATION_NO_TEXT,
    OBSERVATION_OK,
    OBSERVATION_SYSTEM_ONLY,
    OBSERVATION_TEXT_MARKERS,
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
    current_identity,
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
    observation_quality_reason,
    observation_text_insufficient,
    question_signature,
    quiz_question_progressed,
    remaining_attempt_seconds,
    race_with_cancel,
    reserve_attempt,
    session_timeout_reached,
    status_text,
    submission_mismatch_reason,
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


# ----------------------------------------------------------------------
# 观察质量：UI dump 成功 != 页面可观察（A14 重放）
# ----------------------------------------------------------------------


def _snapshot(nodes, **kwargs):
    data = dict(
        package="review.app",
        activity="review.app.MainActivity",
        width=1080,
        height=1920,
        nodes=nodes,
        captured_at=time.time(),
    )
    data.update(kwargs)
    return ScreenSnapshot(**data)


def _node(ref: int, **kwargs):
    data = dict(
        text="",
        desc="",
        class_name="android.widget.TextView",
        package="review.app",
        bounds=Rect(0, 0, 100, 100),
    )
    data.update(kwargs)
    return UiNode(ref=ref, **data)


def _judge_rendered(snapshot, *, pre_text="请完成滑块验证", kind=SOLVE_KIND_CAPTCHA):
    rendered = render_ui(snapshot)
    return judge_solve_outcome(
        kind=kind,
        pre_text=pre_text,
        post_text=rendered,
        observation_ok=True,
    )


def test_a14_empty_ui_rendered_text_is_unknown_not_cleared():
    """A14 重放：真实 render_ui 输出的零可读节点文本不得判 cleared。"""
    snapshot = _snapshot([])
    rendered = render_ui(snapshot)
    assert "当前界面没有可读节点" in rendered
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请完成滑块验证",
        post_text=rendered,
        observation_ok=True,
        generation=0,
    )
    assert outcome.outcome == OUTCOME_UNKNOWN
    assert "观察" in outcome.evidence or "可读" in outcome.evidence
    assert outcome.outcome != OUTCOME_CLEARED


def test_judge_pure_system_nodes_render_is_unknown():
    snapshot = _snapshot(
        [
            _node(0, text="12:00", package="com.android.systemui"),
            _node(1, package="android", clickable=True),
        ]
    )
    outcome = _judge_rendered(snapshot)
    assert outcome.outcome == OUTCOME_UNKNOWN


def test_judge_loading_page_render_is_unknown():
    snapshot = _snapshot([_node(0, text="加载中…"), _node(1, text="请稍候")])
    outcome = _judge_rendered(snapshot)
    assert outcome.outcome == OUTCOME_UNKNOWN
    assert "加载" in outcome.evidence


def test_judge_clickable_only_nodes_render_is_unknown():
    snapshot = _snapshot([_node(0, clickable=True), _node(1, clickable=True)])
    outcome = _judge_rendered(snapshot)
    assert outcome.outcome == OUTCOME_UNKNOWN


@pytest.mark.parametrize(
    "quality",
    [OBSERVATION_EMPTY, OBSERVATION_SYSTEM_ONLY, OBSERVATION_NO_TEXT, OBSERVATION_LOADING],
)
def test_judge_insufficient_observation_quality_is_unknown(quality):
    """结构化观察质量不足时，即使文本看起来可读也不能解除门禁。"""
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请完成滑块验证",
        post_text="首页推荐视频列表",
        observation_ok=True,
        observation_quality=quality,
    )
    assert outcome.outcome == OUTCOME_UNKNOWN
    assert observation_quality_reason(quality) in outcome.evidence


def test_judge_observation_quality_ok_keeps_known_success_page():
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请完成滑块验证",
        post_text="首页推荐视频列表",
        observation_ok=True,
        observation_quality=OBSERVATION_OK,
    )
    assert outcome.outcome == OUTCOME_CLEARED


def test_judge_unchanged_page_without_positive_evidence_is_unknown():
    """前后界面一致（动作未生效）时不能仅凭缺少挑战关键词判通过。"""
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="首页推荐视频列表",
        post_text="首页推荐视频列表",
        observation_ok=True,
        observation_quality=OBSERVATION_OK,
    )
    assert outcome.outcome == OUTCOME_UNKNOWN


def test_judge_success_requires_affirmative_evidence():
    affirmative = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请完成滑块验证",
        post_text="验证成功，已解除限制",
        observation_ok=True,
        observation_quality=OBSERVATION_OK,
    )
    assert affirmative.outcome == OUTCOME_CLEARED
    assert "成功" in affirmative.evidence


def test_judge_challenge_page_still_failed_with_quality_ok():
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请完成滑块验证",
        post_text="请拖动滑块完成验证",
        observation_ok=True,
        observation_quality=OBSERVATION_OK,
    )
    assert outcome.outcome == OUTCOME_FAILED


def test_observation_text_markers_match_render_ui_outputs():
    """判定器兜底文案必须覆盖 render_ui 的观察不足标记（防两处漂移）。"""
    empty_text = render_ui(_snapshot([]))
    loading_text = render_ui(_snapshot([_node(0, text="加载中")]))
    assert observation_text_insufficient(empty_text)
    assert observation_text_insufficient(loading_text)
    for marker in OBSERVATION_TEXT_MARKERS:
        assert marker in empty_text or marker in loading_text
    assert observation_text_insufficient("首页推荐视频列表") == ""


def test_judge_propagates_attempt_identity():
    outcome = judge_solve_outcome(
        kind=SOLVE_KIND_CAPTCHA,
        pre_text="请完成滑块验证",
        post_text="验证成功",
        observation_ok=True,
        generation=0,
        session_id="ver-abc",
        attempt_id=2,
    )
    assert outcome.session_id == "ver-abc"
    assert outcome.attempt_id == 2
    assert outcome.generation == 0
    assert outcome.identity.session_id == "ver-abc"


# ----------------------------------------------------------------------
# 取消与完成同事件循环周期：取消优先
# ----------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_race_with_cancel_same_cycle_cancel_wins():
    async def scenario():
        cancelled = asyncio.Event()
        finished_state = {"stopped": False}

        async def work():
            # 与取消信号在同一事件循环周期就绪
            cancelled.set()
            return "stale-result"

        finished, result = await race_with_cancel(work(), cancelled)
        assert finished is False
        assert result is None
        assert finished_state["stopped"] is False

    _run(scenario())


def test_race_with_cancel_returns_result_without_cancel():
    async def scenario():
        async def work():
            return 42

        finished, result = await race_with_cancel(work(), asyncio.Event())
        assert finished is True
        assert result == 42

    _run(scenario())


def test_race_with_cancel_cancels_slow_work():
    async def scenario():
        cancelled = asyncio.Event()
        state = {"started": False, "cancelled": False}

        async def work():
            state["started"] = True
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            return "不应返回"

        async def trigger():
            while not state["started"]:
                await asyncio.sleep(0)
            cancelled.set()

        trigger_task = asyncio.create_task(trigger())
        finished, result = await race_with_cancel(work(), cancelled)
        await trigger_task
        assert finished is False
        assert result is None
        assert state["cancelled"] is True

    _run(scenario())


def test_race_with_cancel_supports_non_waitable_signal():
    async def scenario():
        async def work():
            return "done"

        finished, result = await race_with_cancel(work(), None)
        assert (finished, result) == (True, "done")

    _run(scenario())


# ----------------------------------------------------------------------
# 提交身份纯函数与观察质量文本兜底
# ----------------------------------------------------------------------


def test_submission_mismatch_accepts_current_identity():
    state = new_session(now=1000.0)
    reserve_attempt(state, limits=effective_limits(state), now=1000.0)
    identity = current_identity(state)
    outcome = SolveOutcome(
        OUTCOME_CLEARED,
        generation=identity.generation,
        session_id=identity.session_id,
        attempt_id=identity.attempt_id,
    )
    assert submission_mismatch_reason(state, outcome, now=1000.0) == ""


def test_submission_mismatch_rejects_old_session_identity_with_zero_generation():
    """两个会话代次都是 0：必须靠 session_id 区分（A13 根因）。"""
    old_state = new_session(now=1000.0)
    reserve_attempt(old_state, limits=effective_limits(old_state), now=1000.0)
    old_identity = current_identity(old_state)

    new_state = new_session(now=2000.0)
    reserve_attempt(new_state, limits=effective_limits(new_state), now=2000.0)
    outcome = SolveOutcome(
        OUTCOME_CLEARED,
        generation=old_identity.generation,
        session_id=old_identity.session_id,
        attempt_id=old_identity.attempt_id,
    )
    reason = submission_mismatch_reason(new_state, outcome, now=2000.0)
    assert "会话" in reason


def test_observation_text_insufficient_handles_plain_text_and_empty():
    assert observation_text_insufficient("") == "UI 观察文本为空"
    assert "可读节点" in observation_text_insufficient("（当前界面没有可读节点）")
    assert observation_text_insufficient("验证成功") == ""
