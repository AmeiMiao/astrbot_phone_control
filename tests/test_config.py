"""core.config 单元测试：嵌套/扁平解析、类型容错与默认值。"""

from __future__ import annotations

import dataclasses

import pytest

from core.config import (
    DEFAULT_AUTO_CRON,
    DEFAULT_SETTLE_SECONDS,
    AdbSettings,
    PhoneConfig,
)


def test_empty_mapping_returns_defaults():
    config = PhoneConfig.from_mapping(None)
    assert config == PhoneConfig()
    # 新安装不预填 ADB：空地址/端口表示未配置，插件以假开启状态加载。
    assert config.adb.host == ""
    assert config.adb.port == 0
    assert config.adb.target == ""
    assert config.adb.configured is False
    assert config.security.require_admin is True
    assert config.security.allowed_user_ids == ()
    assert config.security.allow_interactions is False
    assert config.security.auto_clean_after_task is True
    assert config.perception.max_ui_nodes == 80
    assert config.perception.screen_source == "adb"
    assert config.browse.default_app == "douyin"
    assert config.browse.default_items == 5
    assert config.browse.max_items == 20
    # 浏览日志默认上限 500，清理时保护最近 3 轮任务。
    assert config.browse.diary_max_entries == 500
    assert config.browse.diary_protect_rounds == 3
    assert config.auto.enabled is False
    assert config.auto.cron == DEFAULT_AUTO_CRON
    assert config.prompt.inject_guidance is True
    assert config.debug.log_actions is True


def test_nested_card_config_is_parsed():
    raw = {
        "adb": {
            "host": "10.0.0.2",
            "port": "5555",
            "path": "/opt/adb",
            "serial": "10.0.0.2:5555",
            "connect_timeout": "9",
            "command_timeout": 12.5,
        },
        "security": {
            "require_admin": False,
            "allowed_user_ids": ["1001", 1002, "1001"],
            "allow_shell_tool": True,
            "max_likes_per_session": "3",
        },
        "perception": {
            "max_ui_nodes": 200,
            "dump_timeout": "15",
            "include_screenshot_by_default": True,
            "max_text_len": "80",
        },
        "browse": {
            "default_app": "bilibili",
            "default_items": "7",
            "max_items": 30,
            "swipe_duration_ms": 1200,
            "settle_seconds": "1.5",
            "collect_screenshots": False,
            "send_screenshots_to_user": True,
            "auto_remember": False,
        },
        "auto": {
            "enabled": True,
            "cron": "  0   */3 * * *  ",
            "apps": "douyin，bilibili",
            "session": "aiocqhttp:FriendMessage:10086",
            "max_items": "4",
            "prompt": "只看游戏资讯",
        },
        "prompt": {"inject_guidance": False, "diary_limit": 8},
        "debug": {"log_actions": False},
    }
    config = PhoneConfig.from_mapping(raw)
    assert config.adb == AdbSettings(
        host="10.0.0.2",
        port=5555,
        path="/opt/adb",
        serial="10.0.0.2:5555",
        connect_timeout=9.0,
        command_timeout=12.5,
    )
    assert config.adb.configured is True
    assert config.security.require_admin is False
    assert config.security.allowed_user_ids == ("1001", "1002")
    assert config.security.allow_shell_tool is True
    assert config.security.max_likes_per_session == 3
    assert config.perception.max_ui_nodes == 200
    assert config.perception.include_screenshot_by_default is True
    assert config.browse.default_app == "bilibili"
    assert config.browse.default_items == 7
    assert config.browse.max_items == 30
    assert config.browse.swipe_duration_ms == 1200
    assert config.browse.settle_seconds == 1.5
    assert config.browse.collect_screenshots is False
    assert config.browse.send_screenshots_to_user is True
    assert config.browse.auto_remember is False
    assert config.auto.enabled is True
    assert config.auto.cron == "0 */3 * * *"
    assert config.auto.app_list() == ("douyin", "bilibili")
    assert config.auto.session == "aiocqhttp:FriendMessage:10086"
    assert config.auto.max_items == 4
    assert config.auto.prompt == "只看游戏资讯"
    assert config.prompt.inject_guidance is False
    assert config.prompt.diary_limit == 8
    assert config.debug.log_actions is False


def test_legacy_flat_config_is_supported():
    raw = {
        "host": "192.168.1.8",
        "port": 6000,
        "serial": "192.168.1.8:6000",
        "require_admin": "false",
        "allowed_user_ids": "1001, 1002 1003\n1004",
        "default_app": "xiaoheihe",
        "default_items": 3,
        "settle_seconds": 1.0,
        "cron": "*/30 * * * *",
        "apps": "bilibili,douyin",
        "diary_limit": 2,
    }
    warnings: list[str] = []
    config = PhoneConfig.from_mapping(raw, warn=warnings.append)
    assert config.adb.host == "192.168.1.8"
    assert config.adb.port == 6000
    assert config.adb.serial == "192.168.1.8:6000"
    assert config.adb.configured is True
    assert config.security.require_admin is False
    assert config.security.allowed_user_ids == ("1001", "1002", "1003", "1004")
    assert config.browse.default_app == "xiaoheihe"
    assert config.browse.default_items == 3
    assert config.browse.settle_seconds == 1.0
    assert config.auto.cron == "*/30 * * * *"
    assert config.auto.app_list() == ("bilibili", "douyin")
    assert config.prompt.diary_limit == 2
    assert warnings == []


def test_nested_card_wins_over_flat_key():
    config = PhoneConfig.from_mapping(
        {"adb": {"host": "10.1.1.1"}, "host": "9.9.9.9"}
    )
    assert config.adb.host == "10.1.1.1"


def test_invalid_values_fall_back_with_warning():
    warnings: list[str] = []
    raw = {
        "adb": {"port": "not-a-port", "connect_timeout": -3, "command_timeout": 999},
        "perception": {"max_ui_nodes": "many", "max_text_len": 1},
        "browse": {"max_items": 0, "default_items": "x", "settle_seconds": "fast"},
        "auto": {"cron": "every day"},
        "security": {"allowed_user_ids": 123},
    }
    config = PhoneConfig.from_mapping(raw, warn=warnings.append)
    assert config.adb.port == 0
    assert config.adb.configured is False
    assert config.adb.connect_timeout == 1.0
    assert config.adb.command_timeout == 300.0
    assert config.perception.max_ui_nodes == 80
    assert config.perception.max_text_len == 10
    assert config.browse.max_items == 1
    assert config.browse.default_items == 1
    assert config.browse.settle_seconds == DEFAULT_SETTLE_SECONDS
    assert config.auto.cron == DEFAULT_AUTO_CRON
    assert config.security.allowed_user_ids == ()
    assert len(warnings) >= 8
    assert all(isinstance(message, str) and message for message in warnings)


def test_default_items_clamped_to_max_items():
    config = PhoneConfig.from_mapping(
        {"browse": {"max_items": 4, "default_items": 10}}
    )
    assert config.browse.max_items == 4
    assert config.browse.default_items == 4


def test_bool_string_variants():
    for raw_value, expected in (
        ("true", True),
        ("YES", True),
        ("开启", True),
        (1, True),
        ("off", False),
        ("关闭", False),
        (0, False),
    ):
        config = PhoneConfig.from_mapping({"security": {"require_admin": raw_value}})
        assert config.security.require_admin is expected


def test_config_snapshot_is_frozen():
    config = PhoneConfig.from_mapping({})
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.adb.host = "1.2.3.4"  # type: ignore[misc]


def test_auto_app_list_falls_back_and_dedupes():
    assert PhoneConfig().auto.app_list() == ("douyin",)
    config = PhoneConfig.from_mapping(
        {"auto": {"apps": " , douyin,抖音,douyin "}}
    )
    assert config.auto.app_list() == ("douyin", "抖音")


def test_adb_empty_or_partial_config_stays_unconfigured():
    assert PhoneConfig.from_mapping({}).adb.target == ""
    assert PhoneConfig.from_mapping({}).adb.configured is False
    # 只填主机或只填端口都不足以启用：地址与端口必须同时有效。
    assert PhoneConfig.from_mapping({"adb": {"host": "10.0.0.2"}}).adb.target == ""
    assert PhoneConfig.from_mapping({"adb": {"port": 6000}}).adb.target == ""
    assert PhoneConfig.from_mapping({"adb": {"host": "", "port": 6000}}).adb.configured is False
    # 显式 serial 可直接作为连接目标并视为已配置（无需主机/端口）。
    serial_only = PhoneConfig.from_mapping({"adb": {"serial": "emulator-5554"}})
    assert serial_only.adb.target == "emulator-5554"
    assert serial_only.adb.configured is True
    assert PhoneConfig.from_mapping({"adb": {"host": "", "port": 0}}).adb.target == ""


def test_adb_port_out_of_range_is_unconfigured():
    for raw_port in (-1, 65536, "70000"):
        warnings: list[str] = []
        config = PhoneConfig.from_mapping(
            {"adb": {"host": "10.0.0.2", "port": raw_port}},
            warn=warnings.append,
        )
        assert config.adb.port == 0, raw_port
        assert config.adb.configured is False
        assert warnings, raw_port


def test_adb_target_prefers_serial():
    config = PhoneConfig.from_mapping(
        {
            "adb": {
                "host": "10.0.0.2",
                "port": 5555,
                "serial": "emulator-5554",
            }
        }
    )
    assert config.adb.target == "emulator-5554"
    configured = PhoneConfig.from_mapping(
        {"adb": {"host": "10.0.0.2", "port": "5555"}}
    )
    assert configured.adb.port == 5555
    assert configured.adb.target == "10.0.0.2:5555"


def test_unconfigured_message_is_actionable():
    from core.config import UNCONFIGURED_MESSAGE

    assert "adb.host" in UNCONFIGURED_MESSAGE
    assert "adb.port" in UNCONFIGURED_MESSAGE
    assert "未配置" in UNCONFIGURED_MESSAGE


def test_conf_schema_uses_empty_adb_defaults():
    """新安装不预填 ADB 地址：空值表示未配置，避免默认连不上的本机端口。"""
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    adb_items = schema["adb"]["items"]
    assert adb_items["host"]["default"] == ""
    assert adb_items["port"]["default"] == 0
    assert "留空" in adb_items["host"]["hint"]
    assert "留空" in adb_items["port"]["hint"]
    dumped = json.dumps(schema, ensure_ascii=False)
    assert "16448" not in dumped
    assert "192.168.250.10" not in dumped


def test_unconfigured_config_is_valid_and_invalid_port_is_unconfigured():
    """空地址/端口是合法的未启用配置；显式非法端口按未配置处理并告警。"""
    assert PhoneConfig().adb.configured is False
    warnings: list[str] = []
    config = PhoneConfig.from_mapping(
        {"adb": {"host": "10.0.0.2", "port": 70000}}, warn=warnings.append
    )
    assert config.adb.configured is False
    assert warnings
    configured = PhoneConfig.from_mapping(
        {"adb": {"host": "10.0.0.2", "port": 5555}}
    )
    assert configured.adb.configured is True
    assert configured.adb.target == "10.0.0.2:5555"


def test_diary_max_entries_config_is_clamped():
    config = PhoneConfig.from_mapping(
        {"browse": {"diary_max_entries": 5, "diary_protect_rounds": 200}}
    )
    assert config.browse.diary_max_entries == 10
    assert config.browse.diary_protect_rounds == 100
    custom = PhoneConfig.from_mapping(
        {"browse": {"diary_max_entries": 120, "diary_protect_rounds": 0}}
    )
    assert custom.browse.diary_max_entries == 120
    assert custom.browse.diary_protect_rounds == 0


def test_new_config_keys_defaults():
    config = PhoneConfig.from_mapping({})
    assert config.security.auto_clean_after_browse is True
    assert config.security.auto_clean_after_task is True
    assert config.perception.screenshot_mode == "auto"
    assert config.perception.screenshot_slow_ms == 800
    assert config.perception.screen_source == "adb"
    auto = config.auto
    assert auto.random_enabled is True
    assert auto.random_interval_min == 45
    assert auto.random_interval_max == 120
    assert auto.random_probability == 0.5
    assert auto.share_only_interesting is True
    assert auto.share_target == ""


def test_new_config_keys_parsing():
    warnings: list[str] = []
    config = PhoneConfig.from_mapping(
        {
            "security": {"auto_clean_after_browse": False},
            "perception": {
                "screenshot_mode": "RAW",
                "screenshot_slow_ms": 1500,
            },
            "auto": {
                "share_target": "aiocqhttp:FriendMessage:10086",
                "random_enabled": False,
                "random_interval_min": 300,
                "random_interval_max": 100,
                "random_probability": 1.5,
                "share_only_interesting": False,
            },
        },
        warn=warnings.append,
    )
    assert config.security.auto_clean_after_browse is False
    assert config.perception.screenshot_mode == "raw"
    assert config.perception.screenshot_slow_ms == 1500
    assert config.auto.share_target == "aiocqhttp:FriendMessage:10086"
    assert config.auto.random_enabled is False
    # 最大间隔小于最小间隔 → 收敛为最小间隔
    assert config.auto.random_interval_min == 300
    assert config.auto.random_interval_max == 300
    # 概率越界 → 收敛到 1.0
    assert config.auto.random_probability == 1.0
    assert config.auto.share_only_interesting is False
    assert warnings


def test_auto_clean_after_task_config():
    """玩手机/控制类任务结束自动回桌面清理后台：默认开启、可关闭、宽松布尔。"""
    assert PhoneConfig.from_mapping({}).security.auto_clean_after_task is True
    disabled = PhoneConfig.from_mapping(
        {"security": {"auto_clean_after_task": False}}
    )
    assert disabled.security.auto_clean_after_task is False
    for raw_value, expected in (
        ("true", True),
        ("YES", True),
        ("开启", True),
        (1, True),
        ("false", False),
        ("off", False),
        ("关闭", False),
        ("0", False),
        (0, False),
    ):
        config = PhoneConfig.from_mapping(
            {"security": {"auto_clean_after_task": raw_value}}
        )
        assert config.security.auto_clean_after_task is expected, raw_value
    warnings: list[str] = []
    fallback = PhoneConfig.from_mapping(
        {"security": {"auto_clean_after_task": "maybe"}}, warn=warnings.append
    )
    assert fallback.security.auto_clean_after_task is True
    assert warnings


def test_screenshot_mode_invalid_falls_back():
    warnings: list[str] = []
    config = PhoneConfig.from_mapping(
        {"perception": {"screenshot_mode": "ultra"}}, warn=warnings.append
    )
    assert config.perception.screenshot_mode == "auto"
    assert warnings


def test_quota_config_defaults_and_convergence():
    config = PhoneConfig.from_mapping({})
    quota = config.quota
    assert quota.enabled is False
    assert quota.window_hours == 24
    assert quota.max_tokens == 0
    assert quota.play_task_tokens == 0
    assert quota.per_step_tokens == 0

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {
            "quota": {
                "enabled": False,
                "window_hours": 9999,
                "max_tokens": -1,
                "play_task_tokens": "50000",
                "per_step_tokens": 0,
            }
        },
        warn=warnings.append,
    )
    assert tuned.quota.enabled is False
    # 没有硬编码上限：任意大窗口按原值使用
    assert tuned.quota.window_hours == 9999
    assert tuned.quota.max_tokens == 0
    assert tuned.quota.play_task_tokens == 50000
    assert tuned.quota.per_step_tokens == 0
    assert warnings


def test_security_owner_ids_parsing():
    config = PhoneConfig.from_mapping({"security": {"owner_ids": ["10001", 10002]}})
    assert config.security.owner_ids == ("10001", "10002")
    empty = PhoneConfig.from_mapping({})
    assert empty.security.owner_ids == ()


def test_interaction_config_defaults_and_parsing():
    config = PhoneConfig.from_mapping({})
    security = config.security
    assert security.allow_comments is False
    assert security.allow_posts is False
    assert security.max_comments_per_day == 0
    assert security.max_posts_per_day == 0
    assert security.interaction_cooldown_seconds == 0

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {
            "security": {
                "allow_comments": True,
                "allow_posts": "yes",
                "max_comments_per_day": "99",
                "max_posts_per_day": -2,
                "interaction_cooldown_seconds": 99999,
            }
        },
        warn=warnings.append,
    )
    assert tuned.security.allow_comments is True
    assert tuned.security.allow_posts is True
    assert tuned.security.max_comments_per_day == 99
    assert tuned.security.max_posts_per_day == 0
    # 冷却没有硬编码上限
    assert tuned.security.interaction_cooldown_seconds == 99999
    assert warnings


def test_live_screen_config_defaults_and_convergence():
    config = PhoneConfig.from_mapping({})
    perception = config.perception
    assert perception.live_screen_enabled is False
    assert perception.live_screen_interval_ms == 1200
    assert perception.live_screen_frames == 3
    assert perception.ocr_provider_id == ""

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {
            "perception": {
                "live_screen_enabled": False,
                "live_screen_interval_ms": 99999,
                "live_screen_frames": 0,
                "ocr_provider_id": "my-ocr-provider",
            }
        },
        warn=warnings.append,
    )
    assert tuned.perception.live_screen_enabled is False
    assert tuned.perception.live_screen_interval_ms == 5000
    assert tuned.perception.live_screen_frames == 1
    assert tuned.perception.ocr_provider_id == "my-ocr-provider"
    assert warnings

    low = PhoneConfig.from_mapping(
        {"perception": {"live_screen_interval_ms": 50, "live_screen_frames": 99}}
    )
    assert low.perception.live_screen_interval_ms == 300
    assert low.perception.live_screen_frames == 10


def test_live_screen_idle_stop_config():
    config = PhoneConfig.from_mapping({})
    assert config.perception.live_screen_idle_stop_seconds == 90
    low = PhoneConfig.from_mapping(
        {"perception": {"live_screen_idle_stop_seconds": 5}}
    )
    assert low.perception.live_screen_idle_stop_seconds == 30
    high = PhoneConfig.from_mapping(
        {"perception": {"live_screen_idle_stop_seconds": 9999}}
    )
    assert high.perception.live_screen_idle_stop_seconds == 600


def test_control_backend_config_defaults_and_convergence():
    config = PhoneConfig.from_mapping({})
    control = config.control
    assert control.backend == "auto"
    assert control.scrcpy.max_size == 720
    assert control.scrcpy.server_jar == ""
    assert control.scrcpy.idle_stop_seconds == 120
    assert control.minitouch.binary_path == ""
    assert config.perception.screen_source == "adb"

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {
            "control": {
                "backend": "SCRCPY",
                "scrcpy": {
                    "max_size": 99999,
                    "server_jar": "/opt/scrcpy-server.jar",
                    "idle_stop_seconds": 5,
                },
                "minitouch": {"binary_path": "/opt/minitouch"},
            },
            "perception": {"screen_source": "invalid"},
        },
        warn=warnings.append,
    )
    assert tuned.control.backend == "scrcpy"
    assert tuned.control.scrcpy.max_size == 8192
    assert tuned.control.scrcpy.server_jar == "/opt/scrcpy-server.jar"
    assert tuned.control.scrcpy.idle_stop_seconds == 10
    assert tuned.control.minitouch.binary_path == "/opt/minitouch"
    # 非法值回退默认 adb（scrcpy 仅控制通道）
    assert tuned.perception.screen_source == "adb"
    assert warnings

    # auto/scrcpy 显式选择仍可用（启用 scrcpy 视频镜像）
    assert (
        PhoneConfig.from_mapping(
            {"perception": {"screen_source": "AUTO"}}
        ).perception.screen_source
        == "auto"
    )
    assert (
        PhoneConfig.from_mapping(
            {"perception": {"screen_source": "scrcpy"}}
        ).perception.screen_source
        == "scrcpy"
    )
    invalid = PhoneConfig.from_mapping(
        {"control": {"backend": "bad"}, "perception": {"screen_source": "adb"}}
    )
    assert invalid.control.backend == "auto"
    assert invalid.perception.screen_source == "adb"


def test_conf_schema_task_cleanup_and_screen_source_defaults():
    """卡片默认值：任务结束自动清理默认开，感知来源默认 adb。"""
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    security = schema["security"]["items"]
    assert security["auto_clean_after_browse"]["default"] is True
    assert security["auto_clean_after_task"]["type"] == "bool"
    assert security["auto_clean_after_task"]["default"] is True
    assert "保留" in security["auto_clean_after_task"]["hint"]
    assert "com.wireguard.android" in security["background_keep_packages"]["default"]
    source = schema["perception"]["items"]["screen_source"]
    assert source["type"] == "string"
    assert source["default"] == "adb"
    for keyword in ("adb", "auto", "scrcpy"):
        assert keyword in source["hint"]
    scrcpy = schema["control"]["items"]["scrcpy"]
    assert "perception.screen_source" in scrcpy["hint"]
    assert "video=false" in scrcpy["hint"]


def test_passive_reply_config_defaults_and_parsing():
    """被动回复：默认关闭、独立每日限额与独立冷却，宽松解析并收敛越界值。"""
    config = PhoneConfig.from_mapping({})
    security = config.security
    assert security.allow_passive_replies is False
    assert security.max_passive_replies_per_day == 0
    assert security.passive_reply_cooldown_seconds == 0
    # 主动评论/发帖字段保持各自默认，互不串值
    assert security.allow_comments is False
    assert security.max_comments_per_day == 0
    assert security.interaction_cooldown_seconds == 0

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {
            "security": {
                "allow_passive_replies": "on",
                "max_passive_replies_per_day": "12",
                "passive_reply_cooldown_seconds": 600,
            }
        },
        warn=warnings.append,
    )
    assert tuned.security.allow_passive_replies is True
    assert tuned.security.max_passive_replies_per_day == 12
    assert tuned.security.passive_reply_cooldown_seconds == 600
    assert warnings == []

    # 显式关闭也接受字符串形式
    for raw_value, expected in (
        ("true", True),
        ("YES", True),
        ("开启", True),
        (1, True),
        ("false", False),
        ("off", False),
        ("关闭", False),
        (0, False),
    ):
        parsed = PhoneConfig.from_mapping(
            {"security": {"allow_passive_replies": raw_value}}
        )
        assert parsed.security.allow_passive_replies is expected, raw_value

    warnings = []
    clamped = PhoneConfig.from_mapping(
        {
            "security": {
                "max_passive_replies_per_day": 999,
                "passive_reply_cooldown_seconds": -1,
            }
        },
        warn=warnings.append,
    )
    assert clamped.security.max_passive_replies_per_day == 999
    assert clamped.security.passive_reply_cooldown_seconds == 0
    assert warnings


def test_continuous_config_defaults_and_parsing():
    """持续刷手机：默认关闭、默认间隔/预算/App 列表，支持逗号分隔与越界收敛。"""
    auto = PhoneConfig.from_mapping({}).auto
    assert auto.continuous_enabled is False
    assert auto.continuous_gap_seconds == 60
    assert auto.continuous_target_tokens == 0
    assert auto.continuous_apps == ""
    # 与定时自主浏览字段互不影响
    assert auto.enabled is False
    assert auto.cron == DEFAULT_AUTO_CRON

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {
            "auto": {
                "continuous_enabled": "yes",
                "continuous_gap_seconds": "900",
                "continuous_target_tokens": "50000",
                "continuous_apps": "douyin，bilibili",
            }
        },
        warn=warnings.append,
    )
    assert tuned.auto.continuous_enabled is True
    assert tuned.auto.continuous_gap_seconds == 900
    assert tuned.auto.continuous_target_tokens == 50000
    assert tuned.auto.continuous_apps == "douyin,bilibili"
    assert warnings == []

    warnings = []
    clamped = PhoneConfig.from_mapping(
        {
            "auto": {
                "continuous_enabled": "maybe",
                "continuous_gap_seconds": 5,
                "continuous_target_tokens": -1,
            }
        },
        warn=warnings.append,
    )
    assert clamped.auto.continuous_enabled is False
    assert clamped.auto.continuous_gap_seconds == 5
    assert clamped.auto.continuous_target_tokens == 0
    assert len(warnings) >= 2

    high = PhoneConfig.from_mapping(
        {
            "auto": {
                "continuous_gap_seconds": 99999,
                "continuous_target_tokens": 99999999,
            }
        }
    )
    assert high.auto.continuous_gap_seconds == 99999
    assert high.auto.continuous_target_tokens == 99999999

    # 显式 0 的目标预算表示沿用 quota.play_task_tokens，不会被当作空值回退
    zero_target = PhoneConfig.from_mapping(
        {"auto": {"continuous_target_tokens": 0}}
    )
    assert zero_target.auto.continuous_target_tokens == 0


def test_conf_schema_passive_reply_and_continuous_fields():
    """卡片字段：被动回复 3 项、持续刷 4 项的类型/默认值/范围说明齐全。"""
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    security = schema["security"]["items"]
    assert security["allow_passive_replies"]["type"] == "bool"
    assert security["allow_passive_replies"]["default"] is False
    assert security["max_passive_replies_per_day"]["type"] == "int"
    assert security["max_passive_replies_per_day"]["default"] == 0
    assert "0=不限制" in security["max_passive_replies_per_day"]["hint"]
    assert security["max_passive_replies_per_day"]["condition"] == {
        "allow_passive_replies": True
    }
    assert security["passive_reply_cooldown_seconds"]["type"] == "int"
    assert security["passive_reply_cooldown_seconds"]["default"] == 0
    assert "0=无冷却" in security["passive_reply_cooldown_seconds"]["hint"]
    assert "独立" in security["passive_reply_cooldown_seconds"]["hint"]

    auto = schema["auto"]["items"]
    assert auto["continuous_enabled"]["type"] == "bool"
    assert auto["continuous_enabled"]["default"] is False
    assert "phone_learn" in auto["continuous_enabled"]["hint"]
    assert auto["continuous_gap_seconds"]["type"] == "int"
    assert auto["continuous_gap_seconds"]["default"] == 60
    assert "至少 1 秒" in auto["continuous_gap_seconds"]["hint"]
    assert auto["continuous_target_tokens"]["type"] == "int"
    assert auto["continuous_target_tokens"]["default"] == 0
    assert "0=不限制" in auto["continuous_target_tokens"]["hint"]
    assert auto["continuous_apps"]["type"] == "string"
    assert auto["continuous_apps"]["default"] == ""
    for key in (
        "continuous_gap_seconds",
        "continuous_target_tokens",
        "continuous_apps",
    ):
        assert auto[key]["condition"] == {"continuous_enabled": True}


def test_wait_and_verify_timeout_config_defaults_and_parsing():
    """等待与验证求解超时：默认值、显式解析、越界收敛。"""
    perception = PhoneConfig.from_mapping({}).perception
    assert perception.wait_default_seconds == 2.0
    assert perception.wait_max_seconds == 60
    assert perception.verify_timeout_seconds == 30

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {
            "perception": {
                "wait_default_seconds": "5.5",
                "wait_max_seconds": "120",
                "verify_timeout_seconds": 45,
            }
        },
        warn=warnings.append,
    )
    assert tuned.perception.wait_default_seconds == 5.5
    assert tuned.perception.wait_max_seconds == 120
    assert tuned.perception.verify_timeout_seconds == 45
    assert warnings == []

    warnings = []
    clamped = PhoneConfig.from_mapping(
        {
            "perception": {
                "wait_default_seconds": 0.01,
                "wait_max_seconds": 0,
                "verify_timeout_seconds": 1,
            }
        },
        warn=warnings.append,
    )
    assert clamped.perception.wait_default_seconds == 0.2
    assert clamped.perception.wait_max_seconds == 1
    assert clamped.perception.verify_timeout_seconds == 5
    assert len(warnings) >= 2

    high = PhoneConfig.from_mapping(
        {
            "perception": {
                "wait_default_seconds": 999,
                "wait_max_seconds": 9999,
                "verify_timeout_seconds": 999,
            }
        }
    )
    assert high.perception.wait_default_seconds == 60.0
    assert high.perception.wait_max_seconds == 600
    assert high.perception.verify_timeout_seconds == 120

    # 显式下限值不被当作空值回退
    low = PhoneConfig.from_mapping(
        {"perception": {"wait_default_seconds": 0.2, "verify_timeout_seconds": 5}}
    )
    assert low.perception.wait_default_seconds == 0.2
    assert low.perception.verify_timeout_seconds == 5


def test_conf_schema_wait_and_verify_timeout_fields():
    """卡片字段：等待默认/上限与验证求解超时的类型/默认值/范围说明。"""
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    perception = schema["perception"]["items"]
    assert perception["wait_default_seconds"]["type"] == "float"
    assert perception["wait_default_seconds"]["default"] == 2.0
    assert "0.2-60" in perception["wait_default_seconds"]["hint"]
    assert "phone_wait" in perception["wait_default_seconds"]["description"]
    assert perception["wait_max_seconds"]["type"] == "int"
    assert perception["wait_max_seconds"]["default"] == 60
    assert "1-600" in perception["wait_max_seconds"]["hint"]
    assert "上限" in perception["wait_max_seconds"]["description"]
    assert perception["verify_timeout_seconds"]["type"] == "int"
    assert perception["verify_timeout_seconds"]["default"] == 30
    assert "5-120" in perception["verify_timeout_seconds"]["hint"]
    assert "phone_solve_verification" in perception["verify_timeout_seconds"]["hint"]


def test_app_memory_prompt_config_defaults_and_parsing():
    """App 记忆注入：默认开启、最多 5 个 App；支持关闭与上限解析。"""
    prompt = PhoneConfig.from_mapping({}).prompt
    assert prompt.app_memory_enabled is True
    assert prompt.app_memory_limit == 5

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {"prompt": {"app_memory_enabled": False, "app_memory_limit": 12}},
        warn=warnings.append,
    )
    assert tuned.prompt.app_memory_enabled is False
    assert tuned.prompt.app_memory_limit == 12
    assert warnings == []

    # 非法/负数按默认或下限处理
    low = PhoneConfig.from_mapping(
        {"prompt": {"app_memory_enabled": "maybe", "app_memory_limit": -3}},
        warn=warnings.append,
    )
    assert low.prompt.app_memory_enabled is True
    assert low.prompt.app_memory_limit == 0
    assert warnings


def test_user_priority_config_defaults_and_parsing():
    """用户任务优先：默认开启；可显式关闭。"""
    assert PhoneConfig.from_mapping({}).auto.user_priority is True
    off = PhoneConfig.from_mapping({"auto": {"user_priority": "off"}})
    assert off.auto.user_priority is False


def test_conf_schema_app_memory_and_priority_fields_present():
    """App 记忆、用户任务优先与配额不限额字段在 schema 中存在且默认值正确。"""
    import json
    from pathlib import Path as _Path

    schema = json.loads(
        (_Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    auto = schema["auto"]["items"]
    assert auto["user_priority"]["type"] == "bool"
    assert auto["user_priority"]["default"] is True
    prompt = schema["prompt"]["items"]
    assert prompt["app_memory_enabled"]["type"] == "bool"
    assert prompt["app_memory_enabled"]["default"] is True
    assert prompt["app_memory_limit"]["type"] == "int"
    assert prompt["app_memory_limit"]["default"] == 5
    # 配额默认不限制
    quota = schema["quota"]["items"]
    assert quota["enabled"]["default"] is False
    assert quota["max_tokens"]["default"] == 0
    assert quota["play_task_tokens"]["default"] == 0


def test_memory_storage_config_defaults_and_parsing():
    """记忆存储去向：默认 local；支持 companion/both 与绑定用户 ID。"""
    memory = PhoneConfig.from_mapping({}).memory
    assert memory.backend == "local"
    assert memory.companion_user_id == ""

    tuned = PhoneConfig.from_mapping(
        {"memory": {"backend": "both", "companion_user_id": "123456"}}
    )
    # 兼容旧 both 配置 → companion+livingmemory
    assert tuned.memory.backend == "companion+livingmemory"
    assert tuned.memory.bot_action_enabled is False
    living = PhoneConfig.from_mapping(
        {"memory": {"backend": "livingmemory", "bot_action_enabled": True}}
    )
    assert living.memory.backend == "livingmemory"
    assert living.memory.bot_action_enabled is True
    assert tuned.memory.companion_user_id == "123456"

    warnings: list[str] = []
    invalid = PhoneConfig.from_mapping(
        {"memory": {"backend": "cloud"}}, warn=warnings.append
    )
    assert invalid.memory.backend == "local"
    assert warnings


def test_conf_schema_memory_storage_card():
    """记忆存储卡片存在且字段默认值/选项正确。"""
    import json
    from pathlib import Path as _Path

    schema = json.loads(
        (_Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    memory = schema["memory"]["items"]
    assert memory["backend"]["type"] == "string"
    assert memory["backend"]["default"] == "local"
    assert memory["backend"]["options"] == [
        "local",
        "companion",
        "livingmemory",
        "companion+livingmemory",
    ]
    assert memory["bot_action_enabled"]["type"] == "bool"
    assert memory["bot_action_enabled"]["default"] is False
    assert memory["companion_user_id"]["type"] == "string"
    assert memory["companion_user_id"]["default"] == ""


def test_screenshot_cleanup_config_defaults_and_parsing():
    """截图自动清理：默认开启、保留 3 分钟；可配置且最小 1 分钟。"""
    perception = PhoneConfig.from_mapping({}).perception
    assert perception.screenshot_auto_clean is True
    assert perception.screenshot_retention_minutes == 3

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {
            "perception": {
                "screenshot_auto_clean": False,
                "screenshot_retention_minutes": 15,
            }
        },
        warn=warnings.append,
    )
    assert tuned.perception.screenshot_auto_clean is False
    assert tuned.perception.screenshot_retention_minutes == 15
    assert warnings == []

    low = PhoneConfig.from_mapping(
        {"perception": {"screenshot_retention_minutes": 0}},
        warn=warnings.append,
    )
    assert low.perception.screenshot_retention_minutes == 1
    assert warnings


def test_conf_schema_screenshot_cleanup_fields():
    import json
    from pathlib import Path as _Path

    schema = json.loads(
        (_Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    perception = schema["perception"]["items"]
    assert perception["screenshot_auto_clean"]["type"] == "bool"
    assert perception["screenshot_auto_clean"]["default"] is True
    assert perception["screenshot_retention_minutes"]["type"] == "int"
    assert perception["screenshot_retention_minutes"]["default"] == 3
    assert perception["screenshot_retention_minutes"]["condition"] == {
        "screenshot_auto_clean": True
    }


def test_auto_share_policy_config_defaults_and_parsing():
    """分享策略：默认白天 08:00–22:00 中国时区、私聊最近 24 小时。"""
    auto = PhoneConfig.from_mapping({}).auto
    assert auto.share_quiet_hours_enabled is True
    assert auto.share_start_hour == 8
    assert auto.share_end_hour == 22
    assert auto.share_timezone == "Asia/Shanghai"
    assert auto.share_user_recent_hours == 24

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {
            "auto": {
                "share_quiet_hours_enabled": False,
                "share_start_hour": 9,
                "share_end_hour": 23,
                "share_timezone": "UTC",
                "share_user_recent_hours": 48,
            }
        },
        warn=warnings.append,
    )
    assert tuned.auto.share_quiet_hours_enabled is False
    assert tuned.auto.share_start_hour == 9
    assert tuned.auto.share_end_hour == 23
    assert tuned.auto.share_timezone == "UTC"
    assert tuned.auto.share_user_recent_hours == 48

    low = PhoneConfig.from_mapping(
        {
            "auto": {
                "share_start_hour": -3,
                "share_end_hour": 99,
                "share_user_recent_hours": 0,
            }
        },
        warn=warnings.append,
    )
    # 越界值收敛到合法区间（0–23 / 最小 1）
    assert low.auto.share_start_hour == 0
    assert low.auto.share_end_hour == 23
    assert low.auto.share_user_recent_hours == 1
    assert warnings


def test_conf_schema_auto_share_policy_fields():
    import json
    from pathlib import Path as _Path

    schema = json.loads(
        (_Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    auto = schema["auto"]["items"]
    assert auto["share_quiet_hours_enabled"]["default"] is True
    assert auto["share_start_hour"]["default"] == 8
    assert auto["share_end_hour"]["default"] == 22
    assert auto["share_timezone"]["default"] == "Asia/Shanghai"
    assert auto["share_user_recent_hours"]["default"] == 24


def test_traffic_config_defaults_and_parsing():
    """抓包配置：默认开启、端口 18080、正文 1200、窗口 900 秒。"""
    traffic = PhoneConfig.from_mapping({}).traffic
    assert traffic.enabled is True
    assert traffic.port == 18080
    assert traffic.body_chars == 1200
    assert traffic.keep_seconds == 900
    assert traffic.mitmdump_path == ""

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {
            "traffic": {
                "enabled": False,
                "port": 19000,
                "body_chars": 4000,
                "keep_seconds": 120,
            }
        },
        warn=warnings.append,
    )
    assert tuned.traffic.enabled is False
    assert tuned.traffic.port == 19000
    assert tuned.traffic.body_chars == 4000
    assert tuned.traffic.keep_seconds == 120
    assert warnings == []

    clamped = PhoneConfig.from_mapping(
        {
            "traffic": {
                "port": 1,
                "body_chars": 10,
                "keep_seconds": 1,
            }
        },
        warn=warnings.append,
    )
    assert clamped.traffic.port == 1024
    assert clamped.traffic.body_chars == 200
    assert clamped.traffic.keep_seconds == 60
    assert warnings


def test_conf_schema_traffic_card():
    import json
    from pathlib import Path as _Path

    schema = json.loads(
        (_Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    traffic = schema["traffic"]["items"]
    assert traffic["enabled"]["default"] is True
    assert traffic["port"]["default"] == 18080
    assert traffic["body_chars"]["default"] == 1200
    assert traffic["keep_seconds"]["default"] == 900


def test_verification_config_defaults_and_parsing():
    """验证与人工协助：默认 3 次/120s/30s/600s，非法值收敛。"""
    verification = PhoneConfig.from_mapping({}).verification
    assert verification.max_attempts == 3
    assert verification.total_timeout_seconds == 120
    assert verification.attempt_timeout_seconds == 30
    assert verification.manual_wait_timeout_seconds == 600
    assert verification.notify_target == ""

    warnings: list[str] = []
    tuned = PhoneConfig.from_mapping(
        {
            "verification": {
                "max_attempts": 1,
                "total_timeout_seconds": 60,
                "attempt_timeout_seconds": 20,
                "manual_wait_timeout_seconds": 120,
                "notify_target": "aiocqhttp:FriendMessage:123",
            }
        },
        warn=warnings.append,
    )
    assert tuned.verification.max_attempts == 1
    assert tuned.verification.total_timeout_seconds == 60
    assert tuned.verification.attempt_timeout_seconds == 20
    assert tuned.verification.manual_wait_timeout_seconds == 120
    assert tuned.verification.notify_target == "aiocqhttp:FriendMessage:123"
    assert warnings == []

    clamped = PhoneConfig.from_mapping(
        {
            "verification": {
                "max_attempts": -2,
                "total_timeout_seconds": 1,
                "attempt_timeout_seconds": 1,
                "manual_wait_timeout_seconds": 1,
            }
        },
        warn=warnings.append,
    )
    assert clamped.verification.max_attempts == 0
    assert clamped.verification.total_timeout_seconds == 10
    assert clamped.verification.attempt_timeout_seconds == 5
    assert clamped.verification.manual_wait_timeout_seconds == 30
    assert warnings


def test_conf_schema_verification_card():
    import json
    from pathlib import Path as _Path

    schema = json.loads(
        (_Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    verification = schema["verification"]["items"]
    assert verification["max_attempts"]["default"] == 3
    assert verification["total_timeout_seconds"]["default"] == 120
    assert verification["attempt_timeout_seconds"]["default"] == 30
    assert verification["manual_wait_timeout_seconds"]["default"] == 600
    assert verification["notify_target"]["default"] == ""


def test_novelty_config_defaults_and_parsing():
    """新鲜感/厌倦感：默认阈值 60（0=关闭），数值越界收敛、非法值回退默认。"""
    from core.novelty import (
        DEFAULT_GAIN,
        DEFAULT_HALF_LIFE_HOURS,
        DEFAULT_INJECT_LIMIT,
        DEFAULT_MAX_ENTRIES,
        DEFAULT_SIMILARITY,
        DEFAULT_THRESHOLD,
    )

    novelty = PhoneConfig.from_mapping({}).novelty
    assert novelty.threshold == DEFAULT_THRESHOLD == 60
    assert novelty.half_life_hours == DEFAULT_HALF_LIFE_HOURS == 24
    assert novelty.gain == DEFAULT_GAIN == 20
    assert novelty.similarity == pytest.approx(DEFAULT_SIMILARITY)
    assert novelty.max_entries == DEFAULT_MAX_ENTRIES == 300
    assert novelty.inject_limit == DEFAULT_INJECT_LIMIT == 5
    assert novelty.enabled is True

    # 阈值 0 = 关闭机制
    assert PhoneConfig.from_mapping({"novelty": {"threshold": 0}}).novelty.enabled is False

    # 越界收敛
    clamped = PhoneConfig.from_mapping(
        {
            "novelty": {
                "threshold": 999,
                "half_life_hours": -3,
                "gain": 999,
                "similarity": 9,
                "max_entries": 1,
                "inject_limit": 99,
            }
        }
    ).novelty
    assert clamped.threshold == 100
    assert clamped.half_life_hours == 1
    assert clamped.gain == 100
    assert clamped.similarity == pytest.approx(0.90)
    assert clamped.max_entries == 20
    assert clamped.inject_limit == 10

    # 非法值回退默认
    fallback = PhoneConfig.from_mapping(
        {"novelty": {"threshold": "abc", "similarity": "x"}}
    ).novelty
    assert fallback.threshold == DEFAULT_THRESHOLD
    assert fallback.similarity == pytest.approx(DEFAULT_SIMILARITY)


def test_conf_schema_novelty_card():
    """novelty 卡片存在且默认值与阈值 0=关闭的语义写入 hint。"""
    import json
    from pathlib import Path as _Path

    schema = json.loads(
        (_Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    novelty = schema["novelty"]["items"]
    assert novelty["threshold"]["type"] == "int"
    assert novelty["threshold"]["default"] == 60
    assert "0=不启用" in novelty["threshold"]["description"]
    assert novelty["half_life_hours"]["default"] == 24
    assert novelty["gain"]["default"] == 20
    assert novelty["similarity"]["type"] == "float"
    assert novelty["similarity"]["default"] == 0.15
    assert novelty["max_entries"]["default"] == 300
    assert novelty["inject_limit"]["default"] == 5
    assert "0" in novelty["threshold"]["hint"]


def test_conf_schema_description_width_within_limit():
    """卡片 description 控制在中文字符宽度 26 以内（CJK/全角=1，ASCII/半角=0.5）。"""
    import json
    import unicodedata
    from pathlib import Path as _Path

    schema = json.loads(
        (_Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )

    def width(text: str) -> float:
        return sum(
            1.0 if unicodedata.east_asian_width(char) in ("W", "F") else 0.5
            for char in text
        )

    too_wide: list[str] = []

    def walk(node, path: str = "") -> None:
        if not isinstance(node, dict):
            return
        description = node.get("description")
        if isinstance(description, str) and width(description) > 26:
            too_wide.append(f"{path}: {description}")
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if isinstance(value, dict):
                walk(value, child)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        walk(item, f"{child}[{index}]")

    walk(schema)
    assert too_wide == []


def test_memory_backend_is_split_into_independent_switches():
    config = PhoneConfig.from_mapping(
        {"memory": {"companion_enabled": True, "livingmemory_enabled": True}}
    )
    assert config.memory.companion_enabled is True
    assert config.memory.livingmemory_enabled is True
    assert config.memory.backend == "companion+livingmemory"

    only_companion = PhoneConfig.from_mapping(
        {"memory": {"companion_enabled": True, "livingmemory_enabled": False}}
    )
    assert only_companion.memory.backend == "companion"
    only_living = PhoneConfig.from_mapping(
        {"memory": {"companion_enabled": False, "livingmemory_enabled": True}}
    )
    assert only_living.memory.backend == "livingmemory"
    assert PhoneConfig.from_mapping({}).memory.backend == "local"


def test_legacy_memory_backend_is_derived_when_switches_absent():
    companion = PhoneConfig.from_mapping({"memory": {"backend": "companion"}})
    assert companion.memory.companion_enabled is True
    assert companion.memory.livingmemory_enabled is False
    both = PhoneConfig.from_mapping(
        {"memory": {"backend": "companion+livingmemory"}}
    )
    assert both.memory.companion_enabled is True
    assert both.memory.livingmemory_enabled is True
    legacy_both = PhoneConfig.from_mapping({"memory": {"backend": "both"}})
    assert legacy_both.memory.backend == "companion+livingmemory"
    # 新开关优先于旧 backend（迁移后 backend 固定为 local）。
    migrated = PhoneConfig.from_mapping(
        {
            "memory": {
                "backend": "local",
                "companion_enabled": True,
                "livingmemory_enabled": False,
            }
        }
    )
    assert migrated.memory.backend == "companion"


def test_memory_config_migration_sets_switches_once():
    from core.config import migrate_memory_config

    raw = {"memory": {"backend": "companion+livingmemory"}}
    assert migrate_memory_config(raw) is True
    assert raw["memory"]["companion_enabled"] is True
    assert raw["memory"]["livingmemory_enabled"] is True
    assert raw["memory"]["backend"] == "local"
    # 已迁移/新配置不再修改。
    assert migrate_memory_config(raw) is False
    assert migrate_memory_config({"memory": {"backend": "local"}}) is False
    assert migrate_memory_config({"memory": {"backend": "local", "companion_enabled": True}}) is False


def test_conf_schema_memory_and_wireguard_conditions():
    """未开启对应开关时隐藏记忆/WireGuard 子配置项。"""
    import json
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    memory_items = schema["memory"]["items"]
    assert memory_items["backend"]["invisible"] is True
    assert memory_items["companion_user_id"]["condition"] == {
        "companion_enabled": True
    }
    assert memory_items["bot_action_enabled"]["condition"] == {
        "companion_enabled": True
    }
    wg_items = schema["wireguard"]["items"]
    for key, item in wg_items.items():
        if key == "enabled":
            assert "condition" not in item
            continue
        assert item.get("condition", {}).get("enabled") is True, key
    assert wg_items["server_endpoint"]["condition"] == {
        "enabled": True,
        "mode": "client",
    }
    assert wg_items["listen_port"]["condition"] == {
        "enabled": True,
        "mode": "server",
    }


def test_conf_schema_group_order_is_intentional():
    import json
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    groups = list(schema)
    assert groups[:4] == ["adb", "security", "browse", "auto"]
    assert groups.index("memory") < groups.index("prompt")
    assert groups.index("wireguard") > groups.index("perception")
    assert groups[-1] == "debug"
