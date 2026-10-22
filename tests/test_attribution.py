"""core.attribution 单元测试：学习记录归属判定。"""

from __future__ import annotations

from core.attribution import (
    AUTO_BROWSE_ORIGIN,
    BACKGROUND_ORIGIN,
    CONTINUOUS_ORIGIN,
    KIND_AUTONOMOUS,
    KIND_UNKNOWN,
    KIND_USER,
    PLAY_ORIGIN,
    RANDOM_PLAY_ORIGIN,
    counts_user_memory,
    is_autonomous,
    kind_label,
    origin_kind,
)


def test_autonomous_origins():
    for origin in (RANDOM_PLAY_ORIGIN, CONTINUOUS_ORIGIN, AUTO_BROWSE_ORIGIN):
        assert origin_kind(origin) == KIND_AUTONOMOUS
        assert is_autonomous(origin_kind(origin)) is True
        assert counts_user_memory(origin_kind(origin)) is False


def test_user_directed_origins():
    for origin in ("", PLAY_ORIGIN, BACKGROUND_ORIGIN):
        assert origin_kind(origin) == KIND_USER
        assert counts_user_memory(origin_kind(origin)) is True
        assert is_autonomous(origin_kind(origin)) is False


def test_unknown_origin_is_not_counted():
    assert origin_kind("phone_unknown") == KIND_UNKNOWN
    assert counts_user_memory(KIND_UNKNOWN) is False
    assert is_autonomous(KIND_UNKNOWN) is False


def test_kind_labels():
    assert kind_label(KIND_AUTONOMOUS) == "自行唤醒"
    assert kind_label(KIND_USER) == "用户指定"
    assert kind_label(KIND_UNKNOWN) == "未标注"
