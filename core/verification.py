"""验证会话与人工接管状态控制（纯标准库，可本地单测）。

用于「做题/人机验证」场景的有限自动尝试与人工协助降级：

* 每个设备同时只允许一个未解决验证会话（``verification_id``）；
* 失败次数（``max_attempts``）、会话总时限（``total_timeout_seconds``）、
  单次尝试时限（``attempt_timeout_seconds``）与人工等待时限（``manual_wait_timeout_seconds``）
  由配置控制；失败次数或时间先到者生效；
* 状态持久化到 JSON（原子写），插件重启/重载不丢次数与人工等待状态；
* 状态分为三类：进行中（``ACTIVE_STATUSES``）、暂停（``PAUSED_STATUSES``，含已过期的
  ``expired``，必须显式取消才解除）与已关闭（``CLOSED_STATUSES``，只有 passed/cancelled）；
* 到限后进入 ``awaiting_user``：暂停手机自动操作、保留验证页面，由上层发出一次求助通知；
* 用户通过 ``/phone verify resume|cancel <id>`` 恢复或取消。

本模块只做状态与文案；通知发送、设备暂停与调度钩子在 ``core/plugin/*`` 与 ``PhoneService`` 完成。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_TOTAL_TIMEOUT_SECONDS = 120
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 30
DEFAULT_MANUAL_WAIT_TIMEOUT_SECONDS = 600
VERIFICATION_STORE_FILENAME = "verification.json"

STATUS_OBSERVING = "observing"
STATUS_ATTEMPTING = "attempting"
STATUS_AWAITING_USER = "awaiting_user"
STATUS_CHECKING = "checking"
STATUS_PASSED = "passed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

# 进行中：仍允许自动尝试/观察
ACTIVE_STATUSES = frozenset(
    {STATUS_OBSERVING, STATUS_ATTEMPTING, STATUS_AWAITING_USER, STATUS_CHECKING}
)
# 暂停：手机自动操作一律停下；expired 属于暂停，必须显式 cancel 才解除
PAUSED_STATUSES = frozenset({STATUS_AWAITING_USER, STATUS_CHECKING, STATUS_EXPIRED})
# 已关闭：会话真正结束，只有显式通过/取消两种
CLOSED_STATUSES = frozenset({STATUS_PASSED, STATUS_CANCELLED})
# 兼容别名：终态即已关闭（expired 只是暂停，不在这里）
TERMINAL_STATUSES = CLOSED_STATUSES

# 答题进度与结果判定的结构化关键词（均为纯 UI 文本启发式，最终以关闭条件为准）
CHALLENGE_KEYWORDS = (
    "验证码",
    "滑动验证",
    "拖动滑块",
    "滑块",
    "拼图",
    "人机验证",
    "安全验证",
    "请完成验证",
    "点击完成",
    "顺序点击",
    "验证失败",
    "请重试",
)
FAILURE_KEYWORDS = (
    "验证失败",
    "验证未通过",
    "未通过验证",
    "请重试",
    "再试一次",
    "错误",
    "不正确",
)
SUCCESS_KEYWORDS = (
    "验证成功",
    "已完成验证",
    "验证通过",
    "通过验证",
    "已通过",
    "提交成功",
    "答对",
    "回答正确",
    "回答无误",
    "全部正确",
    "正确无误",
    "验证完成",
)
RESULT_PAGE_KEYWORDS = (
    "答题结束",
    "测试结束",
    "答题完成",
    "完成答题",
    "已完成",
    "得分",
    "成绩",
    "结果页",
    "感谢参与",
)

# 结构化判定结果：已解除 / 答题进度 / 未通过 / 无法确认 / 非验证页面
OUTCOME_CLEARED = "cleared"
OUTCOME_PROGRESS = "progress"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_NOT_CHALLENGE = "not_challenge"

SOLVE_KIND_CAPTCHA = "captcha"
SOLVE_KIND_QUIZ = "quiz"

# UI 观察质量：UI dump 成功不等于页面可观察（WebView/自绘页面/加载过渡页可能没有可读节点）。
# 已给定质量等级时，只有 ``OBSERVATION_OK`` 可按「没有挑战关键词 + 页面已变化」推断解除；
# 其余等级一律判无法确认。未给定质量时按渲染文本中的观察不足标记兜底。
OBSERVATION_OK = "ok"
OBSERVATION_EMPTY = "empty"
OBSERVATION_SYSTEM_ONLY = "system_only"
OBSERVATION_NO_TEXT = "no_text"
OBSERVATION_LOADING = "loading"
# 观察质量不足以判定结果：不能解除门禁，必须按「无法确认」计失败
INSUFFICIENT_OBSERVATIONS = frozenset(
    {
        OBSERVATION_EMPTY,
        OBSERVATION_SYSTEM_ONLY,
        OBSERVATION_NO_TEXT,
        OBSERVATION_LOADING,
    }
)

# 渲染文本中的观察不足标记（与 ``core/ui.py`` 的 render_ui 文案保持一致）
OBSERVATION_TEXT_MARKERS = (
    "当前界面没有可读节点",
    "界面仍在加载",
)

_OBSERVATION_REASONS = {
    OBSERVATION_EMPTY: "UI 树为空",
    OBSERVATION_SYSTEM_ONLY: "只有系统界面节点",
    OBSERVATION_NO_TEXT: "UI 节点没有可读文本",
    OBSERVATION_LOADING: "界面处于加载过渡页",
}

NOTIFY_NONE = "none"
NOTIFY_PENDING = "pending"
NOTIFY_SENT = "sent"
NOTIFY_FAILED = "failed"
NOTIFY_UNKNOWN = "unknown"

# 签名归一化时剔除的瞬时 UI 文案（成功/失败提示、按钮与倒计时），避免同一题被误判为换题
_TRANSIENT_UI_NOISE = (
    "回答正确",
    "回答错误",
    "答对了",
    "太棒了",
    "恭喜您",
    "恭喜",
    "请重试",
    "验证失败",
    "验证成功",
    "提交成功",
    "下一题",
    "继续答题",
    "继续",
    "倒计时",
)

_QUESTION_NUMBER_PATTERNS = (
    re.compile(r"第\s*(\d{1,3})\s*题"),
    re.compile(r"(?:题目|问题|question|quiz)\s*[：:#]?\s*(\d{1,3})", re.IGNORECASE),
    re.compile(r"(\d{1,3})\s*/\s*(\d{1,3})"),
)


def now_ts() -> float:
    return time.time()


def new_verification_id(now: float | None = None) -> str:
    """生成验证会话编号（带短随机后缀，便于用户输入）。"""
    stamp = int((now if now is not None else now_ts()) * 1000)
    return f"ver-{stamp}-{secrets.token_hex(2)}"


def is_active(state: dict[str, Any] | None) -> bool:
    return bool(state) and str(state.get("status") or "") in ACTIVE_STATUSES


def is_paused(state: dict[str, Any] | None) -> bool:
    """是否处于暂停（含已过期的 expired：过期仍暂停，需显式取消）。"""
    return bool(state) and str(state.get("status") or "") in PAUSED_STATUSES


def is_closed(state: dict[str, Any] | None) -> bool:
    """会话是否已关闭（仅 passed/cancelled）。"""
    return bool(state) and str(state.get("status") or "") in CLOSED_STATUSES


def is_terminal(state: dict[str, Any] | None) -> bool:
    """``is_closed`` 的兼容别名：终态=已关闭，expired 不属于终态。"""
    return is_closed(state)


def normalize_int(raw: Any, default: int, *, minimum: int = 0) -> int:
    """归一化整数配置（下限保护；不设业务上限）。"""
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return int(default)
    return max(int(minimum), value)


def effective_limits(
    state: dict[str, Any],
    current: dict[str, Any] | None = None,
) -> dict[str, int]:
    """计算当前生效限制。

    降低配置立即生效（取较小值）；调高配置不重置已用次数/会话起点，也不延长期限
    （仍取会话开始时的值），避免通过改配置续命。
    """
    limits = {
        "max_attempts": normalize_int(
            state.get("max_attempts"), DEFAULT_MAX_ATTEMPTS, minimum=0
        ),
        "total_timeout_seconds": normalize_int(
            state.get("total_timeout_seconds"),
            DEFAULT_TOTAL_TIMEOUT_SECONDS,
            minimum=10,
        ),
        "attempt_timeout_seconds": normalize_int(
            state.get("attempt_timeout_seconds"),
            DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
            minimum=5,
        ),
        "manual_wait_timeout_seconds": normalize_int(
            state.get("manual_wait_timeout_seconds"),
            DEFAULT_MANUAL_WAIT_TIMEOUT_SECONDS,
            minimum=30,
        ),
    }
    if current:
        for key in ("max_attempts", "total_timeout_seconds"):
            if key not in current:
                continue
            value = normalize_int(
                current.get(key), limits[key], minimum=0 if key == "max_attempts" else 10
            )
            limits[key] = min(limits[key], value)
    return limits


def elapsed_ms(state: dict[str, Any], now: float | None = None) -> int:
    try:
        started = float(state.get("started_at") or 0)
    except (TypeError, ValueError):
        started = 0.0
    if started <= 0:
        return 0
    current = float(now) if now is not None else now_ts()
    return max(0, int((current - started) * 1000))


def remaining_ms(
    state: dict[str, Any],
    limits: dict[str, int],
    now: float | None = None,
) -> int:
    total_ms = int(limits.get("total_timeout_seconds") or 0) * 1000
    return max(0, total_ms - elapsed_ms(state, now))


def _current_ts(now: float | None = None) -> float:
    return float(now) if now is not None else now_ts()


def _attempt_started_at(state: dict[str, Any] | None) -> float:
    """当前尝试开始时间；0 表示没有进行中的尝试。"""
    if not state:
        return 0.0
    try:
        started = float(state.get("attempt_started_at") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, started)


def new_session(
    *,
    device: str = "",
    app: str = "",
    activity: str = "",
    origin: str = "user",
    initiator_umo: str = "",
    notify_umo: str = "",
    max_attempts: Any = DEFAULT_MAX_ATTEMPTS,
    total_timeout_seconds: Any = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    attempt_timeout_seconds: Any = DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    manual_wait_timeout_seconds: Any = DEFAULT_MANUAL_WAIT_TIMEOUT_SECONDS,
    now: float | None = None,
) -> dict[str, Any]:
    """创建持久化验证会话状态。"""
    current = _current_ts(now)
    state = {
        "id": new_verification_id(current),
        "device": str(device or ""),
        "app": str(app or ""),
        "activity": str(activity or ""),
        "origin": str(origin or "user"),
        "initiator_umo": str(initiator_umo or ""),
        "notify_umo": str(notify_umo or ""),
        "started_at": current,
        "updated_at": current,
        "status": STATUS_OBSERVING,
        "attempts": 0,
        "attempt_started_at": 0.0,
        "failure_count": 0,
        "quiz_answered": 0,
        "quiz_total": 0,
        "max_attempts": normalize_int(max_attempts, DEFAULT_MAX_ATTEMPTS, minimum=0),
        "total_timeout_seconds": normalize_int(
            total_timeout_seconds, DEFAULT_TOTAL_TIMEOUT_SECONDS, minimum=10
        ),
        "attempt_timeout_seconds": normalize_int(
            attempt_timeout_seconds, DEFAULT_ATTEMPT_TIMEOUT_SECONDS, minimum=5
        ),
        "manual_wait_timeout_seconds": normalize_int(
            manual_wait_timeout_seconds,
            DEFAULT_MANUAL_WAIT_TIMEOUT_SECONDS,
            minimum=30,
        ),
        "cancel_generation": 0,
        "awaiting_since": 0.0,
        "last_reason": "",
        "challenge_version": 0,
        "notify": {
            "state": NOTIFY_NONE,
            "attempts": 0,
            "last_error": "",
            "sent_at": 0.0,
            "updated_at": 0.0,
        },
    }
    return state


def _touch(state: dict[str, Any], now: float | None = None) -> None:
    state["updated_at"] = _current_ts(now)


def _failure_usage(state: dict[str, Any], limits: dict[str, int]) -> str:
    failures = int(state.get("failure_count") or 0)
    maximum = int(limits.get("max_attempts") or 0)
    attempts = int(state.get("attempts") or 0)
    return f"失败 {failures}/{maximum} 次，已尝试 {attempts} 次"


def failure_limit_reached(
    state: dict[str, Any],
    *,
    limits: dict[str, int] | None = None,
) -> bool:
    """失败/无法确认次数是否达到上限（人工等待依据）。"""
    if not state:
        return False
    limit = limits or effective_limits(state)
    return int(state.get("failure_count") or 0) >= int(limit.get("max_attempts") or 0)


def remaining_attempt_seconds(
    state: dict[str, Any],
    *,
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> float:
    """当前单次尝试剩余秒数（同时受会话总剩余约束）；无尝试/已暂停返回 0。"""
    if not state or is_paused(state) or is_closed(state):
        return 0.0
    started = _attempt_started_at(state)
    if started <= 0:
        return 0.0
    limit = limits or effective_limits(state)
    current = _current_ts(now)
    attempt_limit = float(limit.get("attempt_timeout_seconds") or 0)
    attempt_remaining = max(0.0, attempt_limit - (current - started))
    session_remaining = remaining_ms(state, limit, current) / 1000.0
    return min(attempt_remaining, session_remaining)


def attempt_deadline_reached(
    state: dict[str, Any],
    *,
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> bool:
    """当前尝试是否已超过期限（单次尝试上限或会话总剩余先到者）。"""
    if not state or is_paused(state) or is_closed(state):
        return False
    if _attempt_started_at(state) <= 0:
        return False
    return remaining_attempt_seconds(state, limits=limits, now=now) <= 0


def execution_deadline_seconds(
    state: dict[str, Any],
    *,
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> float:
    """本次执行剩余秒数 = min(会话剩余, 单次尝试剩余)。

    没有进行中的验证会话时返回 ``inf``（设备操作不受验证期限约束）。
    """
    if not state or is_closed(state) or not is_active(state):
        return float("inf")
    limit = limits or effective_limits(state)
    current = _current_ts(now)
    session_remaining = remaining_ms(state, limit, current) / 1000.0
    if _attempt_started_at(state) <= 0 or is_paused(state):
        return session_remaining
    return min(
        session_remaining,
        remaining_attempt_seconds(state, limits=limit, now=current),
    )


def reserve_attempt(
    state: dict[str, Any],
    *,
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> tuple[bool, str]:
    """预占一次自动尝试（含第一次）。返回 ``(是否允许, 拒绝原因)``。

    调用方必须在真正发起模型请求/动作前调用并持久化；无动作的观察不占次数。
    到期（``expired``）属于暂停状态：拒绝且不得清空/重建会话，必须用户显式取消。
    """
    if not state:
        return False, "当前没有进行中的验证会话"
    status = str(state.get("status") or "")
    if status == STATUS_EXPIRED:
        return False, (
            "error: 人工等待已到期，手机自动操作仍保持暂停；"
            f"请发送 `/phone verify cancel {state.get('id')}` 结束等待后再继续"
        )
    if not is_active(state):
        return False, "当前没有进行中的验证会话"
    limit = limits or effective_limits(state)
    current = _current_ts(now)
    if is_paused(state):
        return False, _awaiting_message(state, limit)
    if remaining_ms(state, limit, current) <= 0:
        apply_awaiting(state, reason="timeout", limits=limit, now=current)
        return False, _awaiting_message(state, limit)
    if failure_limit_reached(state, limits=limit):
        apply_awaiting(state, reason="attempts", limits=limit, now=current)
        return False, _awaiting_message(state, limit)
    state["attempts"] = int(state.get("attempts") or 0) + 1
    state["status"] = STATUS_ATTEMPTING
    state["attempt_started_at"] = current
    state["last_reason"] = ""
    _touch(state, current)
    return True, ""


def _register_failure(
    state: dict[str, Any],
    *,
    reason: str,
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """登记一次失败/无法确认；达到失败上限或总时限则转人工等待，否则回到观察。"""
    if not state or is_closed(state) or is_paused(state):
        return state
    limit = limits or effective_limits(state)
    current = _current_ts(now)
    state["failure_count"] = int(state.get("failure_count") or 0) + 1
    state["attempt_started_at"] = 0.0
    state["last_reason"] = str(reason or "failed")
    if failure_limit_reached(state, limits=limit):
        apply_awaiting(state, reason="attempts", limits=limit, now=current)
    elif remaining_ms(state, limit, current) <= 0:
        apply_awaiting(state, reason="timeout", limits=limit, now=current)
    else:
        state["status"] = STATUS_OBSERVING
        _touch(state, current)
    return state


def finish_attempt(
    state: dict[str, Any],
    *,
    passed: bool = False,
    reason: str = "",
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """结束一次尝试：通过则关闭会话；否则累计失败并决定是否转人工协助。"""
    if not state or is_closed(state):
        return state
    current = _current_ts(now)
    limit = limits or effective_limits(state)
    if passed:
        state["status"] = STATUS_PASSED
        state["last_reason"] = reason or "passed"
        state["attempt_started_at"] = 0.0
        notify = state.get("notify")
        if isinstance(notify, dict):
            notify["state"] = NOTIFY_NONE
        _touch(state, current)
        return state
    return _register_failure(
        state, reason=reason or "failed", limits=limit, now=current
    )


def apply_awaiting(
    state: dict[str, Any],
    *,
    reason: str,
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """进入 ``awaiting_user``：保留次数/时间证据，提升取消代次。"""
    current = _current_ts(now)
    status = str(state.get("status") or "")
    if status in CLOSED_STATUSES or status == STATUS_EXPIRED:
        return state
    state["status"] = STATUS_AWAITING_USER
    state["awaiting_since"] = current
    state["attempt_started_at"] = 0.0
    state["last_reason"] = str(reason or "")
    state["cancel_generation"] = int(state.get("cancel_generation") or 0) + 1
    _touch(state, current)
    return state


def mark_passed(
    state: dict[str, Any], *, reason: str = "passed", now: float | None = None
) -> dict[str, Any]:
    if is_closed(state):
        return state
    state["status"] = STATUS_PASSED
    state["last_reason"] = str(reason or "passed")
    state["attempt_started_at"] = 0.0
    _touch(state, now)
    return state


def mark_cancelled(
    state: dict[str, Any], *, reason: str = "cancelled", now: float | None = None
) -> dict[str, Any]:
    if is_closed(state):
        return state
    state["status"] = STATUS_CANCELLED
    state["last_reason"] = str(reason or "cancelled")
    state["attempt_started_at"] = 0.0
    state["cancel_generation"] = int(state.get("cancel_generation") or 0) + 1
    _touch(state, now)
    return state


def mark_expired(
    state: dict[str, Any], *, reason: str = "expired", now: float | None = None
) -> dict[str, Any]:
    """标记人工等待到期：手机自动操作保持暂停，等待用户显式取消。"""
    if is_closed(state):
        return state
    state["status"] = STATUS_EXPIRED
    state["last_reason"] = str(reason or "expired")
    _touch(state, now)
    return state


def enter_checking(
    state: dict[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    """用户确认完成后进入一次有界结果观察。"""
    state["status"] = STATUS_CHECKING
    _touch(state, now)
    return state


def finish_checking(
    state: dict[str, Any],
    *,
    passed: bool,
    reason: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    if passed:
        return mark_passed(state, reason=reason or "user_confirmed", now=now)
    state["status"] = STATUS_AWAITING_USER
    state["last_reason"] = reason or "still_present"
    _touch(state, now)
    return state


def manual_wait_expired(
    state: dict[str, Any],
    *,
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> bool:
    """人工等待是否已到期（到期后进入 expired，设备仍暂停）。"""
    if str(state.get("status") or "") != STATUS_AWAITING_USER:
        return False
    limit = limits or effective_limits(state)
    try:
        since = float(state.get("awaiting_since") or 0)
    except (TypeError, ValueError):
        since = 0.0
    if since <= 0:
        return False
    current = _current_ts(now)
    return (current - since) > int(limit.get("manual_wait_timeout_seconds") or 0)


def session_timeout_reached(
    state: dict[str, Any],
    *,
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> bool:
    if not is_active(state) or is_paused(state):
        return False
    limit = limits or effective_limits(state)
    return remaining_ms(state, limit, now) <= 0


def challenge_present(text: str) -> bool:
    """按 UI 文本关键词粗略判断验证挑战是否仍在（用于恢复时轻量观察）。"""
    content = str(text or "")
    return any(keyword in content for keyword in CHALLENGE_KEYWORDS)


def _match_keyword(text: str, keywords: tuple[str, ...]) -> str:
    content = str(text or "")
    for keyword in keywords:
        if keyword in content:
            return keyword
    return ""


def _normalize_ui_text(text: str) -> str:
    """归一化 UI 文本：去瞬时提示、空白与标点，保留中英文与数字。"""
    content = unicodedata.normalize("NFKC", str(text or "")).lower()
    for noise in _TRANSIENT_UI_NOISE:
        content = content.replace(noise, "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", content)


def _stem_text(text: str) -> str:
    """题干指纹用文本：在归一化基础上去掉数字（题号/倒计时不参与指纹）。"""
    return re.sub(r"\d+", "", _normalize_ui_text(text))


def extract_question_number(text: str) -> int:
    """从 UI 文本中提取当前题号；提取不到返回 0。"""
    content = unicodedata.normalize("NFKC", str(text or ""))
    for pattern in _QUESTION_NUMBER_PATTERNS:
        match = pattern.search(content)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                continue
    return 0


def question_signature(text: str) -> str:
    """题干指纹（同一题忽略题号与瞬时提示后稳定；无内容返回空串）。"""
    stem = _stem_text(text)
    if not stem:
        return ""
    return hashlib.sha1(stem.encode("utf-8")).hexdigest()[:16]


def quiz_question_progressed(
    pre_text: str, post_text: str
) -> tuple[bool, int]:
    """答题页是否进入下一题。返回 ``(是否前进, 新题号)``。

    优先比较题号；两边都没有题号时比较题干指纹。
    """
    pre_number = extract_question_number(pre_text)
    post_number = extract_question_number(post_text)
    if pre_number > 0 and post_number > 0:
        return post_number > pre_number, post_number
    if post_number > 0 and pre_number == 0:
        return True, post_number
    if pre_number > 0 and post_number == 0:
        return False, 0
    pre_signature = question_signature(pre_text)
    post_signature = question_signature(post_text)
    if pre_signature and post_signature and pre_signature != post_signature:
        return True, 0
    return False, 0


def _match_success(text: str) -> str:
    """匹配成功提示：先精确短语，再用受限的「通过/正确」上下文匹配。"""
    exact = _match_keyword(text, SUCCESS_KEYWORDS)
    if exact:
        return exact
    if re.search(
        r"(已|验证|审核|答题|回答|作答|全部|完全)[^。；;！!?？\n]{0,4}通过", text
    ):
        return "通过"
    if re.search(
        r"(回答|答案|选择|作答|全部|完全)[^。；;！!?？\n]{0,4}正确", text
    ):
        return "正确"
    return ""


def _page_changed(pre_text: str, post_text: str) -> bool:
    pre_stem = _stem_text(pre_text)
    post_stem = _stem_text(post_text)
    if not pre_stem or not post_stem:
        return False
    return pre_stem != post_stem


def observation_quality_reason(quality: str) -> str:
    """把观察质量等级转成可读原因；质量足够或未指定时返回空串。"""
    level = str(quality or "").strip().lower()
    if level not in INSUFFICIENT_OBSERVATIONS:
        return ""
    return _OBSERVATION_REASONS.get(level, "")


def observation_text_insufficient(text: str) -> str:
    """从渲染后的 UI 文本识别「观察不足」；返回原因，空串表示有可读内容。

    UI dump 成功与页面内容可观察是不同条件：渲染文本可能只有尺寸/包名等头部信息，
    或只有坐标没有文本。此函数用于判定器兜底，避免仅凭「没有挑战关键词」判通过。
    """
    content = str(text or "")
    if not content.strip():
        return "UI 观察文本为空"
    for marker in OBSERVATION_TEXT_MARKERS:
        if marker in content:
            return f"UI 观察不足：{marker}"
    body: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("屏幕 "):
            continue
        if stripped.startswith("…（"):
            continue
        body.append(stripped)
    if not body:
        return "UI 观察文本只有头部信息"
    node_lines = [line for line in body if line.startswith("[")]
    if node_lines and not any(
        ("text=" in line or "desc=" in line) for line in node_lines
    ):
        return "UI 观察不足：节点没有可读文本"
    return ""


@dataclass(frozen=True)
class AttemptIdentity:
    """一次验证尝试的身份（预占时生成，提交结果时逐一匹配）。"""

    session_id: str = ""
    attempt_id: int = 0
    generation: int = 0


@dataclass(frozen=True)
class SolveOutcome:
    """一次求解尝试的结构化结果（由 ``judge_solve_outcome`` 产生，控制器据此更新状态）。

    ``session_id``/``attempt_id``/``generation`` 绑定产生结果的验证会话与尝试：
    提交时三者必须与当前执行完全一致（``generation`` 为 0 也参与比较），
    避免旧会话/旧尝试的结果写入新会话。
    """

    outcome: str
    kind: str = SOLVE_KIND_QUIZ
    evidence: str = ""
    question: int = 0
    generation: int = 0
    session_id: str = ""
    attempt_id: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "outcome", str(self.outcome or OUTCOME_UNKNOWN).strip()
        )
        object.__setattr__(
            self, "kind", str(self.kind or SOLVE_KIND_QUIZ).strip()
        )
        object.__setattr__(self, "evidence", str(self.evidence or ""))
        object.__setattr__(self, "session_id", str(self.session_id or "").strip())
        try:
            object.__setattr__(self, "attempt_id", max(0, int(self.attempt_id or 0)))
        except (TypeError, ValueError):
            object.__setattr__(self, "attempt_id", 0)

    @property
    def identity(self) -> AttemptIdentity:
        """结果携带的执行身份（供控制器/工具核对）。"""
        return AttemptIdentity(
            session_id=self.session_id,
            attempt_id=self.attempt_id,
            generation=self.generation,
        )


def apply_outcome(
    state: dict[str, Any],
    outcome: SolveOutcome,
    *,
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """把结构化结果应用到会话状态（纯函数，不持久化、不产生副作用）。"""
    if not state or is_closed(state) or is_paused(state):
        return state
    limit = limits or effective_limits(state)
    current = _current_ts(now)
    result = str(getattr(outcome, "outcome", "") or "")
    kind = str(getattr(outcome, "kind", "") or SOLVE_KIND_QUIZ)
    evidence = str(getattr(outcome, "evidence", "") or "")
    if result == OUTCOME_CLEARED:
        return mark_passed(state, reason=evidence or "cleared", now=current)
    if result == OUTCOME_NOT_CHALLENGE:
        return mark_passed(state, reason="not_challenge", now=current)
    if result == OUTCOME_PROGRESS:
        state["status"] = STATUS_OBSERVING
        state["quiz_answered"] = int(state.get("quiz_answered") or 0) + 1
        state["attempt_started_at"] = 0.0
        state["last_reason"] = "quiz_progress"
        _touch(state, current)
        return state
    if result == OUTCOME_FAILED:
        return _register_failure(
            state, reason=evidence or "failed", limits=limit, now=current
        )
    if result == OUTCOME_UNKNOWN:
        return _register_failure(
            state, reason=evidence or "unknown", limits=limit, now=current
        )
    # 未知结果按无法确认处理（fail-closed：计入失败而不是放行）
    return _register_failure(
        state, reason=evidence or result or kind, limits=limit, now=current
    )


def current_identity(state: dict[str, Any] | None) -> AttemptIdentity:
    """当前会话的执行身份（会话 ID + 已预占尝试次数 + 取消代次）。"""
    state = state or {}
    return AttemptIdentity(
        session_id=str(state.get("id") or ""),
        attempt_id=normalize_int(state.get("attempts"), 0, minimum=0),
        generation=normalize_int(state.get("cancel_generation"), 0, minimum=0),
    )


def submission_mismatch_reason(
    state: dict[str, Any] | None,
    outcome: Any,
    *,
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> str:
    """核对结果身份与当前执行是否仍匹配；返回不匹配原因，空串表示可以提交。

    会话 ID、尝试序号与取消代次全部精确比较（``generation`` 为 0 也参与），
    并复核会话未暂停/未关闭、本次尝试未到期，避免旧执行的结果写入新会话。
    """
    if not state:
        return "当前没有进行中的验证会话"
    if is_closed(state):
        return "验证会话已结束"
    if is_paused(state):
        return "验证会话已暂停等待人工协助"
    if str(state.get("status") or "") != STATUS_ATTEMPTING:
        return f"当前执行已结束（状态 {state.get('status') or '未知'}）"
    expected = current_identity(state)
    session_id = str(getattr(outcome, "session_id", "") or "").strip()
    if session_id != expected.session_id:
        return (
            "求解结果所属会话已失效"
            f"（结果会话 {session_id or '未知'}，当前会话 {expected.session_id or '未知'}）"
        )
    try:
        attempt_id = int(getattr(outcome, "attempt_id", 0) or 0)
    except (TypeError, ValueError):
        attempt_id = -1
    if attempt_id != expected.attempt_id:
        return (
            "求解结果所属尝试已失效"
            f"（结果尝试 {attempt_id}，当前尝试 {expected.attempt_id}）"
        )
    try:
        generation = int(getattr(outcome, "generation", 0) or 0)
    except (TypeError, ValueError):
        generation = -1
    if generation != expected.generation:
        return (
            "求解结果取消代次已变化"
            f"（结果代次 {generation}，当前代次 {expected.generation}）"
        )
    limit = limits or effective_limits(state)
    if attempt_deadline_reached(state, limits=limit, now=now):
        return "本次尝试已到期"
    return ""


async def race_with_cancel(work: Any, cancelled: Any) -> tuple[bool, Any]:
    """让求解任务与「验证暂停/取消」信号竞速，返回 ``(是否取得结果, 结果)``。

    取消优先：取消信号与求解完成落在同一事件循环周期时丢弃求解结果并取消求解
    任务，避免已经暂停/取消的会话仍提交结果。``cancelled`` 不可等待时退化为串行
    等待（与不传入取消信号等价）。
    """
    task = asyncio.create_task(work)
    wait_task: asyncio.Task | None = None
    if cancelled is not None and hasattr(cancelled, "wait"):
        wait_task = asyncio.create_task(cancelled.wait())
    if wait_task is None:
        return True, await task
    try:
        done, _pending = await asyncio.wait(
            {task, wait_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        task.cancel()
        wait_task.cancel()
        raise
    if wait_task in done:
        # 取消与完成同周期时取消优先：求解结果属于已失效执行，不能提交。
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        if not wait_task.done():
            wait_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wait_task
        return False, None
    if not wait_task.done():
        wait_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await wait_task
    result = await task
    # 二次复核：求解完成与取消信号可能落在同一事件循环周期，await 期间信号已置位
    # 时本次结果仍属于已失效执行，不能提交。
    try:
        if cancelled is not None and cancelled.is_set():
            return False, None
    except Exception:  # noqa: BLE001 - 取消信号不支持 is_set 时保持已得结果
        pass
    return True, result


def judge_solve_outcome(
    *,
    kind: str,
    pre_text: str,
    post_text: str,
    observation_ok: bool,
    generation: int = 0,
    session_id: str = "",
    attempt_id: int = 0,
    observation_quality: str = "",
) -> SolveOutcome:
    """基于执行前后的 UI 文本给出结构化判定（纯启发式，只依赖纯标准库）。

    判定顺序：观察失败/观察不足 → 失败词 → 答题换题 → 成功词 → 挑战仍在 →
    页面变化判定。答题换题先于成功词：逐题答对时页面上的「回答正确」只代表本题
    通过，不能把整场答题直接判为解除。

    观察质量由 ``core.ui.assess_observation`` 从 UI 快照评估后传入；空树、纯系统
    节点、无可读文本与加载页一律无法确认（不解除门禁），渲染文本中的观察不足标记
    也会作为兜底证据。``session_id``/``attempt_id``/``generation`` 原样写入结果，
    供控制器在提交时核对执行身份。
    """
    solve_kind = str(kind or SOLVE_KIND_QUIZ).strip()
    observed = str(post_text or "")

    def build(result: str, evidence: str, question: int = 0) -> SolveOutcome:
        return SolveOutcome(
            result,
            kind=solve_kind,
            evidence=evidence,
            question=question,
            generation=generation,
            session_id=session_id,
            attempt_id=attempt_id,
        )

    if not observation_ok:
        return build(OUTCOME_UNKNOWN, "执行后观察失败")
    quality_reason = observation_quality_reason(observation_quality)
    if quality_reason:
        return build(OUTCOME_UNKNOWN, f"UI 观察不足（{quality_reason}），无法确认验证结果")
    text_reason = observation_text_insufficient(observed)
    if text_reason:
        return build(OUTCOME_UNKNOWN, text_reason)
    failure = _match_keyword(observed, FAILURE_KEYWORDS)
    if failure:
        return build(OUTCOME_FAILED, f"出现失败提示：{failure}")
    if solve_kind == SOLVE_KIND_QUIZ:
        progressed, question = quiz_question_progressed(pre_text, observed)
        if progressed:
            evidence = f"进入第 {question} 题" if question > 0 else "题干已变化"
            return build(OUTCOME_PROGRESS, evidence, question=question)
    success = _match_success(observed)
    if success:
        return build(OUTCOME_CLEARED, f"出现成功提示：{success}")
    challenge = _match_keyword(observed, CHALLENGE_KEYWORDS)
    if challenge:
        return build(OUTCOME_FAILED, f"验证挑战仍在：{challenge}")
    changed = _page_changed(pre_text, observed)
    if solve_kind == SOLVE_KIND_CAPTCHA:
        # 人机验证只有「执行前后界面确实发生变化」才可按挑战消失推断解除；
        # 页面没变化说明动作没有生效，不能仅凭缺少挑战关键词放行。
        if not changed:
            return build(OUTCOME_UNKNOWN, "执行前后界面一致，缺少验证解除的肯定证据")
        return build(OUTCOME_CLEARED, "验证挑战已解除（界面已变化）")
    if changed or _match_keyword(observed, RESULT_PAGE_KEYWORDS):
        return build(OUTCOME_CLEARED, "答题页已结束")
    return build(OUTCOME_FAILED, "答题页无明显变化")


def should_notify(state: dict[str, Any]) -> bool:
    """进入/保持人工等待时是否需要发送（或重试）一次求助通知。"""
    if str(state.get("status") or "") != STATUS_AWAITING_USER:
        return False
    notify = state.get("notify") or {}
    state_name = str(notify.get("state") or NOTIFY_NONE)
    attempts = int(notify.get("attempts") or 0)
    if state_name in {NOTIFY_SENT, NOTIFY_PENDING}:
        return False
    return attempts < 3


def mark_notify(
    state: dict[str, Any],
    *,
    state_name: str,
    error: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    notify = state.setdefault("notify", {})
    notify["state"] = str(state_name)
    notify["last_error"] = str(error or "")[:500]
    notify["updated_at"] = _current_ts(now)
    if state_name == NOTIFY_SENT:
        notify["sent_at"] = notify["updated_at"]
    elif state_name in {NOTIFY_FAILED, NOTIFY_UNKNOWN}:
        notify["attempts"] = int(notify.get("attempts") or 0) + 1
    return state


def _reason_text(reason: str, limits: dict[str, int], state: dict[str, Any]) -> str:
    failures = int(state.get("failure_count") or 0)
    maximum = int(limits.get("max_attempts") or 0)
    attempts = int(state.get("attempts") or 0)
    if reason == "timeout":
        return (
            f"已等待 {int(limits.get('total_timeout_seconds') or 0)} 秒"
            f"，失败 {failures}/{maximum} 次，已尝试 {attempts} 次"
        )
    if reason == "attempts":
        return f"失败次数已达上限（{failures}/{maximum}），仍未确认通过"
    if reason == "attempt_timeout":
        return (
            f"单次尝试超过 {int(limits.get('attempt_timeout_seconds') or 0)} 秒"
            "，仍未确认通过"
        )
    if reason == "unsupported":
        return "遇到暂不支持的验证形式"
    if reason == "restart":
        return "插件重启后验证状态待确认"
    if reason == "notification_failed":
        return "无法发送求助通知"
    return str(reason or "验证未通过")


def build_awaiting_message(
    state: dict[str, Any],
    *,
    limits: dict[str, int] | None = None,
    now: float | None = None,
) -> str:
    """生成请求人工协助的固定文案（确定性通知，不依赖模型）。"""
    limit = limits or effective_limits(state)
    app = str(state.get("app") or "手机上")
    reason = str(state.get("last_reason") or "")
    why = _reason_text(reason, limit, state)
    short_id = str(state.get("id") or "")
    return (
        f"我在【{app}】的验证页面卡住了：{why}。我已暂停手机自动操作，"
        "请在你的模拟器/设备上帮忙完成验证，完成后发送 "
        f"`/phone verify resume {short_id}`；也可以发送 "
        f"`/phone verify cancel {short_id}` 结束这次等待。"
    )


def _awaiting_message(state: dict[str, Any], limits: dict[str, int]) -> str:
    usage = _failure_usage(state, limits)
    return (
        "error: 验证已暂停，等待人工协助"
        f"（{usage}，id={state.get('id')}）。请不要继续操作手机；由用户发送 "
        f"`/phone verify resume {state.get('id')}` 或 "
        f"`/phone verify cancel {state.get('id')}`。"
    )


def status_text(state: dict[str, Any] | None, *, now: float | None = None) -> str:
    """渲染验证会话状态（供开关命令/日志使用）；已关闭或无会话显示空状态。"""
    if not state or is_closed(state):
        return "验证与人工协助：当前没有进行中的验证会话"
    current = _current_ts(now)
    limit = effective_limits(state)
    notify = state.get("notify") or {}
    status = str(state.get("status") or "")
    lines = [
        f"验证会话：{state.get('id')}",
        f"- 状态：{status}"
        + (f"（{state.get('last_reason')}）" if state.get("last_reason") else ""),
        f"- App：{state.get('app') or '未知'}；来源：{state.get('origin') or 'user'}",
        f"- 尝试：{int(state.get('attempts') or 0)} 次；"
        f"失败：{int(state.get('failure_count') or 0)}/{int(limit.get('max_attempts') or 0)}",
        f"- 已用时：{elapsed_ms(state, current) / 1000:.1f}s / "
        f"{int(limit.get('total_timeout_seconds') or 0)}s",
        f"- 求助通知：{notify.get('state') or NOTIFY_NONE}"
        + (f"（{notify.get('last_error')}）" if notify.get("last_error") else ""),
    ]
    answered = int(state.get("quiz_answered") or 0)
    total = int(state.get("quiz_total") or 0)
    if answered or total:
        lines.append(
            f"- 答题进度：已答 {answered} 题" + (f"/共 {total} 题" if total else "")
        )
    if status == STATUS_ATTEMPTING and _attempt_started_at(state) > 0:
        lines.append(
            f"- 本次尝试剩余：{remaining_attempt_seconds(state, limits=limit, now=current):.1f}s"
        )
    if status == STATUS_EXPIRED:
        lines.append(
            "- 手机自动操作：仍暂停（人工等待已到期，请发送 "
            f"`/phone verify cancel {state.get('id')}` 释放）"
        )
    elif status == STATUS_AWAITING_USER:
        lines.append(
            f"- 请在设备上完成验证后发送 /phone verify resume {state.get('id')}；"
            f"或 /phone verify cancel {state.get('id')} 结束等待"
        )
    elif is_paused(state):
        lines.append("- 手机自动操作：已暂停（等待结果观察）")
    return "\n".join(lines)


# 状态文件加载结果的种类：区分「首次没有文件」与真正的读取故障。
STORE_LOAD_MISSING = "missing"
STORE_LOAD_EMPTY = "empty"
STORE_LOAD_OK = "ok"
STORE_LOAD_READ_ERROR = "read_error"
STORE_LOAD_PARSE_ERROR = "parse_error"
STORE_LOAD_STRUCTURE_ERROR = "structure_error"
# 需要人工处置才能恢复的故障种类：控制器据此保持设备阻断且不覆盖原文件
STORE_LOAD_FAULTS = frozenset(
    {STORE_LOAD_READ_ERROR, STORE_LOAD_PARSE_ERROR, STORE_LOAD_STRUCTURE_ERROR}
)

# 结构校验时要求可解析的数值字段（缺省允许，用于兼容更早的状态文件）
_STORE_NUMERIC_FIELDS = (
    "attempts",
    "failure_count",
    "quiz_answered",
    "cancel_generation",
    "started_at",
    "updated_at",
    "attempt_started_at",
)


@dataclass(frozen=True)
class StoreLoadResult:
    """``VerificationStore.load_result`` 的返回值：状态 + 加载种类 + 故障说明。"""

    state: dict[str, Any]
    kind: str = STORE_LOAD_OK
    reason: str = ""

    @property
    def fault(self) -> bool:
        """是否为需要人工处置的读取故障（首次缺文件与合法空状态不算故障）。"""
        return self.kind in STORE_LOAD_FAULTS


class VerificationStore:
    """验证会话的 JSON 持久化（原子写；读写失败不抛出到业务层）。"""

    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / VERIFICATION_STORE_FILENAME

    # ---- 读取 ---------------------------------------------------------

    def load_result(self) -> StoreLoadResult:
        """读取状态并区分：首次缺文件、合法空状态、读取/解析/结构故障。

        - 文件不存在：正常空状态（首次启动），不产生故障；
        - 合法空状态（``{}``）：正常空状态；
        - 读取异常（权限/IO）、JSON 解析失败、结构校验失败：保留故障说明，
          调用方必须阻断设备操作并等待人工处置，不得当成「没有状态」放行；
        - 状态文件始终保持原样，不在读取阶段删除或覆盖。
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return StoreLoadResult({}, STORE_LOAD_MISSING, "")
        except UnicodeDecodeError as exc:
            return StoreLoadResult(
                {},
                STORE_LOAD_PARSE_ERROR,
                f"验证状态文件编码无法解析（{self.path}）：{exc}；"
                "已保留原文件并阻断手机自动操作，请执行 `/phone verify cancel <任意编号>` 处置",
            )
        except OSError as exc:
            return StoreLoadResult(
                {},
                STORE_LOAD_READ_ERROR,
                f"验证状态文件读取失败（{self.path}）：{exc}；"
                "已保留原文件并阻断手机自动操作，请检查文件权限后执行 "
                "`/phone verify cancel <任意编号>` 处置",
            )
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            return StoreLoadResult(
                {},
                STORE_LOAD_PARSE_ERROR,
                f"验证状态文件 JSON 无法解析（{self.path}）：{exc}；"
                "已保留原文件并阻断手机自动操作，请执行 `/phone verify cancel <任意编号>` 处置",
            )
        if not isinstance(payload, dict):
            return StoreLoadResult(
                {},
                STORE_LOAD_STRUCTURE_ERROR,
                f"验证状态文件结构校验失败（{self.path}）：顶层不是 JSON 对象；"
                "已保留原文件并阻断手机自动操作，请执行 `/phone verify cancel <任意编号>` 处置",
            )
        if not payload:
            return StoreLoadResult({}, STORE_LOAD_EMPTY, "")
        detail = self._structure_error_detail(payload)
        if detail:
            return StoreLoadResult(
                {},
                STORE_LOAD_STRUCTURE_ERROR,
                f"验证状态文件结构校验失败（{self.path}）：{detail}；"
                "已保留原文件并阻断手机自动操作，请执行 `/phone verify cancel <任意编号>` 处置",
            )
        return StoreLoadResult(payload, STORE_LOAD_OK, "")

    @staticmethod
    def _structure_error_detail(payload: dict[str, Any]) -> str:
        """校验已加载状态的必要结构；返回错误描述，空串表示结构合法。"""
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id.strip():
            return "会话 id 缺失或不是非空字符串"
        status = payload.get("status")
        known = ACTIVE_STATUSES | PAUSED_STATUSES | CLOSED_STATUSES
        if not isinstance(status, str) or status not in known:
            return f"会话状态非法：{status!r}"
        for key in _STORE_NUMERIC_FIELDS:
            if key not in payload:
                continue
            try:
                float(payload[key])
            except (TypeError, ValueError):
                return f"字段 {key} 不是数值：{payload[key]!r}"
        notify = payload.get("notify")
        if notify is not None and not isinstance(notify, dict):
            return "字段 notify 不是对象"
        return ""

    def load(self) -> dict[str, Any]:
        """兼容包装：只返回状态字典；判断是否故障请用 ``load_result``。"""
        return self.load_result().state

    # ---- 写入 / 处置 ---------------------------------------------------

    def save(self, state: dict[str, Any] | None) -> bool:
        """保存状态；空 dict 表示清空。写失败返回 False。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(state or {}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self.path)
            return True
        except OSError:
            return False

    def quarantine(self) -> Path | None:
        """把无法可靠读取的状态文件改名保留（不删除），返回备份路径。

        仅由用户在明确处置时调用；重命名失败返回 ``None``，调用方应继续保持阻断。
        """
        try:
            if not self.path.exists():
                return None
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            backup = self.path.with_name(
                f"{VERIFICATION_STORE_FILENAME}.corrupt-{stamp}-{secrets.token_hex(2)}"
            )
            self.path.replace(backup)
            return backup
        except OSError:
            return None

    def clear(self) -> bool:
        return self.save({})
