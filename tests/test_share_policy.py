"""core.share_policy 单元测试：白天免打扰时段、会话类型与最近交流判定。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.share_policy import (
    DEFAULT_SHARE_END_HOUR,
    DEFAULT_SHARE_START_HOUR,
    DEFAULT_SHARE_TIMEZONE,
    DEFAULT_USER_RECENT_HOURS,
    classify_session_umo,
    is_recent_interaction,
    is_share_window,
    normalize_hour,
    normalize_user_recent_hours,
    resolve_timezone,
    share_window_text,
)

_CST = timezone(timedelta(hours=8))


def _ts(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=_CST).timestamp()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(8, 8), ("9", 9), (23, 23), (0, 0), (-1, 8), (24, 8), (None, 8), ("bad", 8)],
)
def test_normalize_hour(raw, expected):
    assert normalize_hour(raw, 8) == expected


def test_normalize_user_recent_hours():
    assert normalize_user_recent_hours(24) == 24
    assert normalize_user_recent_hours("48") == 48
    assert normalize_user_recent_hours(0) == 1
    assert normalize_user_recent_hours(-3) == 1
    assert normalize_user_recent_hours(None) == DEFAULT_USER_RECENT_HOURS
    assert normalize_user_recent_hours("bad") == DEFAULT_USER_RECENT_HOURS


def test_is_share_window_default_daytime():
    # 默认 08:00–22:00 中国时区
    assert is_share_window(_ts(20, 7, 59)) is False
    assert is_share_window(_ts(20, 8, 0)) is True
    assert is_share_window(_ts(20, 12, 30)) is True
    assert is_share_window(_ts(20, 21, 59)) is True
    assert is_share_window(_ts(20, 22, 0)) is False
    assert is_share_window(_ts(20, 3, 0)) is False


def test_is_share_window_custom_hours_and_overnight():
    assert is_share_window(_ts(20, 10, 0), start_hour=9, end_hour=18) is True
    assert is_share_window(_ts(20, 8, 30), start_hour=9, end_hour=18) is False
    # 跨午夜窗口 22:00–06:00
    assert is_share_window(_ts(20, 23, 0), start_hour=22, end_hour=6) is True
    assert is_share_window(_ts(20, 5, 30), start_hour=22, end_hour=6) is True
    assert is_share_window(_ts(20, 12, 0), start_hour=22, end_hour=6) is False
    # start == end 视为全天
    assert is_share_window(_ts(20, 3, 0), start_hour=8, end_hour=8) is True


def test_resolve_timezone_and_text():
    zone = resolve_timezone("Asia/Shanghai")
    offset = zone.utcoffset(datetime(2026, 10, 20, 12, 0))
    assert offset == timedelta(hours=8)
    assert resolve_timezone("Not/AZone").utcoffset(None) == timedelta(hours=8)
    assert DEFAULT_SHARE_TIMEZONE == "Asia/Shanghai"
    assert DEFAULT_SHARE_START_HOUR == 8
    assert DEFAULT_SHARE_END_HOUR == 22
    assert "08:00–22:00" in share_window_text()
    assert "中国时区" in share_window_text()


@pytest.mark.parametrize(
    ("umo", "expected"),
    [
        ("aiocqhttp:GroupMessage:123", "group"),
        ("webchat:GroupMessage:abc", "group"),
        ("aiocqhttp:FriendMessage:456", "user"),
        ("webchat:PrivateMessage:x", "user"),
        ("telegram:FriendMessage:1", "user"),
        ("webchat:FriendMessage:1", "user"),
        ("cron:active_agent:xxx", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_classify_session_umo(umo, expected):
    assert classify_session_umo(umo) == expected


def test_is_recent_interaction():
    now = 1_000_000.0
    assert is_recent_interaction(now - 3600, now=now, hours=24) is True
    assert is_recent_interaction(now - 25 * 3600, now=now, hours=24) is False
    assert is_recent_interaction(now - 60, now=now, hours=1) is True
    assert is_recent_interaction(now - 3700, now=now, hours=1) is False
    assert is_recent_interaction(0, now=now) is False
    assert is_recent_interaction(None, now=now) is False
    assert is_recent_interaction("bad", now=now) is False
