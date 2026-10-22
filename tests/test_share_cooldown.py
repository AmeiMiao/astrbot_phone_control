"""自主玩手机行为流程与分享冷却：纯逻辑、配置与本地状态。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import PhoneConfig
from core.play_flow import FLOW_RULE, share_cooldown_text, share_rule_text
from core.random_play import build_play_note, build_random_play_note
from core.service import PhoneService
from core.share_policy import (
    DEFAULT_SHARE_COOLDOWN_MINUTES,
    MAX_SHARE_STATE_TARGETS,
    load_share_state,
    normalize_share_cooldown_minutes,
    record_share,
    save_share_state,
    share_cooldown_remaining_seconds,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _schema() -> dict:
    return json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def _service(tmp_path: Path, *, minutes: int = 30) -> PhoneService:
    return PhoneService(
        raw_config={"auto": {"share_cooldown_minutes": minutes}},
        adb=None,
        actions=None,
        diary=None,  # type: ignore[arg-type]
        data_dir=tmp_path,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_SHARE_COOLDOWN_MINUTES),
        ("bad", DEFAULT_SHARE_COOLDOWN_MINUTES),
        ("45", 45),
        (30.9, 30),
        (-5, 0),
        (0, 0),
        (99999, 1440),
    ],
)
def test_normalize_share_cooldown_minutes(raw, expected):
    assert normalize_share_cooldown_minutes(raw) == expected


def test_share_cooldown_remaining_seconds():
    now = 1_000_000.0
    # 没分享过 → 可分享
    assert share_cooldown_remaining_seconds(None, minutes=30, now=now) == 0
    assert share_cooldown_remaining_seconds(0, minutes=30, now=now) == 0
    # 刚分享过 → 剩余接近 30 分钟
    assert share_cooldown_remaining_seconds(now, minutes=30, now=now) == 1800
    assert share_cooldown_remaining_seconds(now - 600, minutes=30, now=now) == 1200
    # 冷却结束
    assert share_cooldown_remaining_seconds(now - 1800, minutes=30, now=now) == 0
    # 0=不限制
    assert share_cooldown_remaining_seconds(now, minutes=0, now=now) == 0
    # 未配置时用默认 30 分钟
    assert share_cooldown_remaining_seconds(now, now=now) == 1800


def test_share_state_roundtrip_and_cap(tmp_path):
    path = tmp_path / "share_state.json"
    state = record_share({}, "webchat:GroupMessage:100", ts=100.0)
    record_share(state, "webchat:FriendMessage:1", ts=200.0)
    save_share_state(path, state)
    loaded = load_share_state(path)
    assert loaded["webchat:GroupMessage:100"] == 100.0
    assert loaded["webchat:FriendMessage:1"] == 200.0

    many = {f"umo:{index}": float(index) for index in range(MAX_SHARE_STATE_TARGETS + 20)}
    save_share_state(path, many)
    assert len(load_share_state(path)) == MAX_SHARE_STATE_TARGETS

    path.write_text("{bad json", encoding="utf-8")
    assert load_share_state(path) == {}


def test_share_cooldown_config_defaults_and_schema():
    auto = PhoneConfig.from_mapping({}).auto
    assert auto.share_cooldown_minutes == DEFAULT_SHARE_COOLDOWN_MINUTES
    assert PhoneConfig.from_mapping(
        {"auto": {"share_cooldown_minutes": 0}}
    ).auto.share_cooldown_minutes == 0
    assert PhoneConfig.from_mapping(
        {"auto": {"share_cooldown_minutes": 45}}
    ).auto.share_cooldown_minutes == 45
    assert PhoneConfig.from_mapping(
        {"auto": {"share_cooldown_minutes": -1}}
    ).auto.share_cooldown_minutes == 0

    item = _schema()["auto"]["items"]["share_cooldown_minutes"]
    assert item["type"] == "int"
    assert item["default"] == DEFAULT_SHARE_COOLDOWN_MINUTES
    assert item["hint"]


def test_service_share_cooldown_uses_config_and_records_target(tmp_path):
    service = _service(tmp_path, minutes=30)
    assert service.share_cooldown_remaining("webchat:GroupMessage:100") == 0
    service.record_share("webchat:GroupMessage:100")
    remaining = service.share_cooldown_remaining("webchat:GroupMessage:100")
    assert 1700 <= remaining <= 1800
    # 其他目标不受影响
    assert service.share_cooldown_remaining("webchat:FriendMessage:1") == 0

    unlimited = _service(tmp_path, minutes=0)
    unlimited.record_share("webchat:GroupMessage:100")
    assert unlimited.share_cooldown_remaining("webchat:GroupMessage:100") == 0


def test_flow_text_and_play_notes_carry_new_behavior():
    assert "不是因为「会话可能感兴趣」才去刷" in FLOW_RULE
    assert "中途不感兴趣直接退出换内容" in FLOW_RULE
    assert "看完/玩完后再单独判断" in FLOW_RULE
    assert "30 分钟" in share_cooldown_text(30)
    assert "未设置目标会话分享冷却" in share_cooldown_text(0)
    rule = share_rule_text(30)
    assert "phone_share_to_user" in rule
    assert "30 分钟" in rule

    hint = "\n分享：" + share_rule_text(30)
    for note in (
        build_random_play_note(share_hint=hint),
        build_play_note(0, share_hint=hint),
    ):
        assert "不是因为「会话可能感兴趣」才去刷" in note
        assert "中途不感兴趣直接退出换内容" in note
        assert "30 分钟" in note
        assert "phone_share_to_user" in note
    # 静态提示也包含行为流程与看/分享两重判断
    static = build_random_play_note()
    assert "中途不感兴趣直接退出换内容" in static
    assert "看和分享是两次独立判断" in static
