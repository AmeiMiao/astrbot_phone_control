"""分享策略：白天免打扰窗口、会话类型与「最近有交流」判定（纯标准库）。

AI 自刷/随机/持续刷玩手机时是否主动分享、分享到哪个群/人，由 AI 根据
``phone_recent_chat`` 里的平时聊天内容自行判断；本模块只提供硬性边界：

* **免打扰时段**：默认中国时区（Asia/Shanghai）08:00–22:00 之外不主动分享；
* **会话类型**：区分群聊 / 私聊，私聊只允许分享给「最近有交流」的用户（默认 24 小时）；
* **目标校验**：只允许分享到聊天记忆里真实记录过的会话。

时区解析优先 ``zoneinfo``；运行环境缺少 tzdata 时回退固定 UTC+8，保证中国时区语义。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_SHARE_TIMEZONE = "Asia/Shanghai"
DEFAULT_SHARE_START_HOUR = 8
DEFAULT_SHARE_END_HOUR = 22
DEFAULT_USER_RECENT_HOURS = 24
MIN_USER_RECENT_HOURS = 1
_FALLBACK_TZ = timezone(timedelta(hours=8))


def resolve_timezone(name: Any = DEFAULT_SHARE_TIMEZONE):
    """解析时区；非法/缺少 tzdata 时回退中国标准时间 UTC+8。"""
    text = str(name or "").strip() or DEFAULT_SHARE_TIMEZONE
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(text)
    except Exception:  # noqa: BLE001 - zoneinfo/tzdata 不可用
        return _FALLBACK_TZ


def normalize_hour(raw: Any, default: int) -> int:
    """归一化 0–23 的小时配置；非法回退默认值。"""
    try:
        hour = int(float(raw))
    except (TypeError, ValueError):
        return int(default)
    if hour < 0 or hour > 23:
        return int(default)
    return hour


def normalize_user_recent_hours(raw: Any) -> int:
    """归一化「最近有交流」窗口小时数：默认 24，最小 1（不设上限）。"""
    try:
        hours = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_USER_RECENT_HOURS
    return max(MIN_USER_RECENT_HOURS, hours)


def is_share_window(
    now: float | None = None,
    *,
    start_hour: Any = DEFAULT_SHARE_START_HOUR,
    end_hour: Any = DEFAULT_SHARE_END_HOUR,
    tz_name: Any = DEFAULT_SHARE_TIMEZONE,
) -> bool:
    """当前是否处于允许主动分享的白天时段（默认 08:00–22:00 中国时区）。

    支持跨午夜窗口（如 22:00–06:00）：start > end 时视为跨天区间。
    """
    start = normalize_hour(start_hour, DEFAULT_SHARE_START_HOUR)
    end = normalize_hour(end_hour, DEFAULT_SHARE_END_HOUR)
    if start == end:
        return True
    current = datetime.fromtimestamp(
        float(now) if now is not None else time.time(), tz=resolve_timezone(tz_name)
    )
    hour = current.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def share_window_text(
    *,
    start_hour: Any = DEFAULT_SHARE_START_HOUR,
    end_hour: Any = DEFAULT_SHARE_END_HOUR,
    tz_name: Any = DEFAULT_SHARE_TIMEZONE,
) -> str:
    """返回人类可读的分享时段描述。"""
    start = normalize_hour(start_hour, DEFAULT_SHARE_START_HOUR)
    end = normalize_hour(end_hour, DEFAULT_SHARE_END_HOUR)
    zone = str(tz_name or DEFAULT_SHARE_TIMEZONE).strip() or DEFAULT_SHARE_TIMEZONE
    zone_label = "中国时区" if zone == DEFAULT_SHARE_TIMEZONE else zone
    return f"{start:02d}:00–{end:02d}:00（{zone_label}）"


def classify_session_umo(umo: Any) -> str:
    """按 UMO 判断会话类型：``group`` / ``user`` / ``other``。"""
    text = str(umo or "").strip()
    if not text:
        return "other"
    lowered = text.lower()
    if "group" in lowered or "讨论组" in text:
        return "group"
    if any(
        keyword in lowered
        for keyword in ("friend", "private", "c2c", "friendmessage")
    ):
        return "user"
    return "other"


def is_recent_interaction(
    last_ts: Any,
    *,
    now: float | None = None,
    hours: Any = DEFAULT_USER_RECENT_HOURS,
) -> bool:
    """判断最后交流时间是否在最近 N 小时内（私聊分享门槛）。"""
    try:
        value = float(last_ts)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    current = float(now) if now is not None else time.time()
    window = normalize_user_recent_hours(hours) * 3600
    return (current - value) <= window
