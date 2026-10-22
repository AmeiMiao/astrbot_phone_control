"""core.config_apply 单元测试：配置字段分类与热应用动作清单。"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from core.config_apply import (
    ACTION_START,
    ACTION_STOP,
    LIVE_KEYS,
    RECONCILE_KEYS,
    build_apply_plan,
    config_field_paths,
)
from core.config import PhoneConfig


def _deep_merge(base: dict, patch: dict) -> dict:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _config(**overrides) -> PhoneConfig:
    """构造配置快照：以默认配置为底，按分组覆盖（保持完整卡片结构）。"""
    raw = asdict(PhoneConfig.from_mapping(None))
    return PhoneConfig.from_mapping(_deep_merge(raw, overrides))


# ----------------------------------------------------------------------
# 字段分类完整性
# ----------------------------------------------------------------------


def test_live_and_reconcile_keys_cover_all_config_fields():
    """每个配置字段必须显式归入实时读取或需重建两类，防止新增字段漏接。"""
    all_keys = config_field_paths()
    assert all_keys == LIVE_KEYS | RECONCILE_KEYS, (
        "配置字段分类不完整：新增字段需归入 LIVE_KEYS 或 RECONCILE_KEYS；"
        f"未分类={sorted(all_keys - LIVE_KEYS - RECONCILE_KEYS)}；"
        f"多余={sorted((LIVE_KEYS | RECONCILE_KEYS) - all_keys)}"
    )


def test_live_and_reconcile_keys_disjoint():
    assert LIVE_KEYS.isdisjoint(RECONCILE_KEYS)


def test_key_sets_not_empty():
    assert LIVE_KEYS
    assert RECONCILE_KEYS


# ----------------------------------------------------------------------
# 无变化
# ----------------------------------------------------------------------


def test_unchanged_config_produces_no_actions():
    current = _config()
    plan = build_apply_plan(current, current)
    assert plan.changed is False
    assert plan.notes == ()
    assert plan.needs_backends_reset is False


def test_live_only_change_produces_no_actions():
    previous = _config()
    current = _config(
        perception={"wait_default_seconds": 1},
        security={"allow_comments": True, "owner_ids": ["1"]},
        quota={"max_tokens": 1000},
        browse={"max_items": 3},
        prompt={"diary_limit": 2},
        debug={"log_actions": False},
        control={"scrcpy": {"idle_stop_seconds": 30}},
    )
    plan = build_apply_plan(previous, current)
    assert plan.changed is False
    assert plan.notes == ()


# ----------------------------------------------------------------------
# ADB 连接参数
# ----------------------------------------------------------------------


def test_adb_target_change_requests_reconnect():
    previous = _config(adb={"host": "10.0.0.1", "port": 5555})
    current = _config(adb={"host": "10.0.0.2", "port": 6666})
    plan = build_apply_plan(previous, current)
    assert plan.adb_changed is True
    assert plan.needs_backends_reset is True
    assert any("ADB" in note and "10.0.0.2:6666" in note for note in plan.notes)


def test_adb_path_or_timeout_change_requests_reconnect():
    previous = _config(adb={"host": "10.0.0.1", "port": 5555})
    current = _config(
        adb={
            "host": "10.0.0.1",
            "port": 5555,
            "path": "/opt/adb",
            "command_timeout": 5,
        }
    )
    plan = build_apply_plan(previous, current)
    assert plan.adb_changed is True


def test_adb_unconfigured_note_is_actionable():
    previous = _config(adb={"host": "10.0.0.1", "port": 5555})
    current = _config(adb={"host": "10.0.0.1", "port": 70000})
    plan = build_apply_plan(previous, current)
    assert plan.adb_changed is True
    assert any("未配置" in note and "重新加载插件" in note for note in plan.notes)


# ----------------------------------------------------------------------
# 控制/感知后端
# ----------------------------------------------------------------------


def test_control_backend_change_resets_backends():
    plan = build_apply_plan(
        _config(control={"backend": "auto"}),
        _config(control={"backend": "adb"}),
    )
    assert plan.backends_changed is True
    assert plan.needs_backends_reset is True
    assert any("后端" in note for note in plan.notes)


def test_screen_source_change_resets_backends():
    plan = build_apply_plan(
        _config(perception={"screen_source": "adb"}),
        _config(perception={"screen_source": "scrcpy"}),
    )
    assert plan.backends_changed is True


def test_scrcpy_max_size_change_resets_backends():
    plan = build_apply_plan(
        _config(control={"scrcpy": {"max_size": 720}}),
        _config(control={"scrcpy": {"max_size": 540}}),
    )
    assert plan.backends_changed is True


def test_minitouch_binary_change_resets_backends():
    plan = build_apply_plan(
        _config(control={"minitouch": {"binary_path": ""}}),
        _config(control={"minitouch": {"binary_path": "/data/minitouch"}}),
    )
    assert plan.backends_changed is True


# ----------------------------------------------------------------------
# 持续刷手机 / 实时屏幕 / 随机玩手机 / 自主浏览
# ----------------------------------------------------------------------


def test_continuous_enable_action():
    plan = build_apply_plan(
        _config(auto={"continuous_enabled": False}),
        _config(auto={"continuous_enabled": True}),
    )
    assert plan.continuous_action == ACTION_START
    assert any("持续刷手机" in note for note in plan.notes)


def test_continuous_disable_action():
    plan = build_apply_plan(
        _config(auto={"continuous_enabled": True}),
        _config(auto={"continuous_enabled": False}),
    )
    assert plan.continuous_action == ACTION_STOP
    assert any("持续刷手机" in note for note in plan.notes)


def test_live_screen_enable_and_disable_actions():
    start = build_apply_plan(
        _config(perception={"live_screen_enabled": False}),
        _config(perception={"live_screen_enabled": True}),
    )
    assert start.live_screen_action == ACTION_START
    stop = build_apply_plan(
        _config(perception={"live_screen_enabled": True}),
        _config(perception={"live_screen_enabled": False}),
    )
    assert stop.live_screen_action == ACTION_STOP
    assert any("持续截图" in note for note in stop.notes)


@pytest.mark.parametrize(
    "patch",
    [
        {"random_interval_min": 10},
        {"random_interval_max": 200},
        {"random_probability": 0.9},
        {"random_enabled": False},
    ],
)
def test_random_settings_change_reschedules(patch):
    plan = build_apply_plan(_config(), _config(auto=patch))
    assert plan.random_changed is True
    assert any("随机玩手机" in note for note in plan.notes)


def test_continuous_gap_change_is_live_only():
    plan = build_apply_plan(
        _config(),
        _config(auto={"continuous_gap_seconds": 30, "continuous_apps": "bilibili"}),
    )
    assert plan.changed is False


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": True},
        {"cron": "0 */3 * * *"},
        {"apps": "bilibili"},
        {"session": "llbot-onebot:FriendMessage:1"},
        {"max_items": 2},
        {"prompt": "只看游戏"},
    ],
)
def test_scheduler_settings_change_requests_sync(patch):
    plan = build_apply_plan(_config(), _config(auto=patch))
    assert plan.scheduler_changed is True


def test_scheduler_disabled_note_mentions_manual_cleanup():
    previous = _config(auto={"enabled": True, "cron": "0 */1 * * *"})
    current = _config(auto={"enabled": False, "cron": "0 */3 * * *"})
    plan = build_apply_plan(previous, current)
    assert plan.scheduler_changed is True
    assert any("自主浏览" in note for note in plan.notes)
