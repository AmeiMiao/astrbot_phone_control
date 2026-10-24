"""每日活动时段：边界、跨午夜、配置拒绝与任务隔离。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.config import PhoneConfig
from core.play_window import activity_allowed, in_windows, parse_windows


def instant(text, zone='Asia/Shanghai'):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(zone)).timestamp()


@pytest.mark.parametrize('clock,allowed', [('07:59:59', False), ('08:00:00', True), ('09:59:59', True), ('10:00:00', False), ('19:00:00', True), ('22:00:00', False)])
def test_multiple_windows_at_exact_boundaries(clock, allowed):
    assert in_windows(('08:00-10:00', '19:00-22:00'), now=instant('2026-10-24T'+clock)) is allowed


@pytest.mark.parametrize('clock,allowed', [('22:59:59', False), ('23:00:00', True), ('00:00:00', True), ('01:59:59', True), ('02:00:00', False)])
def test_cross_midnight(clock, allowed):
    assert in_windows(('23:00-02:00',), now=instant('2026-10-24T'+clock)) is allowed


def test_overlap_union_timezone_and_empty():
    assert in_windows((), now=0)
    assert in_windows(('08:00-10:00','09:00-12:00'), now=instant('2026-10-24T11:00:00'))
    now=instant('2026-10-24T08:00:00')
    assert in_windows(('08:00-10:00',), 'Asia/Shanghai', now)
    assert not in_windows(('08:00-10:00',), 'UTC', now)


@pytest.mark.parametrize('value', ['08:00-10:00', ['24:00-02:00'], ['08:60-10:00'], ['8:00-10:00'], ['08:00-08:00'], [12], ['']])
def test_bad_configuration_rejected(value):
    with pytest.raises(ValueError, match='random_windows'):
        PhoneConfig.from_mapping({'auto': {'random_windows':value}})


def test_timezone_and_normalization():
    with pytest.raises(ValueError, match='play_timezone'):
        PhoneConfig.from_mapping({'auto': {'play_timezone':'Fake/Zone'}})
    assert parse_windows([' 08:00-10:00 ', '08:00-10:00'])==('08:00-10:00',)


def test_random_continuous_have_independent_windows_and_users_unchanged():
    auto=PhoneConfig.from_mapping({'auto':{'random_windows':['08:00-10:00'],'continuous_windows':['19:00-22:00']}}).auto
    now=instant('2026-10-24T09:00:00')
    assert activity_allowed(auto,'phone_random_play',now)
    assert not activity_allowed(auto,'phone_continuous',now)
    for origin in ('', 'phone_play', 'phone_background_task'):
        assert activity_allowed(auto,origin,now)
