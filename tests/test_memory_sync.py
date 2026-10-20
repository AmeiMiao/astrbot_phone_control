"""core.memory_sync 单元测试：存储去向归一化与 MemoryCompanion 同步载荷。"""

from __future__ import annotations

import pytest

from core.memory_sync import (
    APP_MEMORY_TYPE,
    DEFAULT_MEMORY_BACKEND,
    LEARNING_MEMORY_TYPE,
    MEMORY_BACKENDS,
    build_app_experience_payload,
    build_bot_action_payload,
    build_learning_payload,
    normalize_memory_backend,
    wants_companion,
    wants_livingmemory,
    wants_local,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("local", "local"),
        ("companion", "companion"),
        ("both", "companion+livingmemory"),
        (" BOTH ", "companion+livingmemory"),
        ("livingmemory", "livingmemory"),
        ("companion+livingmemory", "companion+livingmemory"),
        ("all", "companion+livingmemory"),
        ("Companion", "companion"),
        ("", "local"),
        (None, "local"),
        ("memory", "local"),
        (123, "local"),
    ],
)
def test_normalize_memory_backend(raw, expected):
    assert normalize_memory_backend(raw) == expected


def test_memory_backend_helpers():
    assert MEMORY_BACKENDS == (
        "local",
        "companion",
        "livingmemory",
        "companion+livingmemory",
    )
    assert wants_local("local") is True
    assert wants_local("both") is True
    assert wants_local("companion") is True  # 本地档案始终保留
    assert wants_companion("local") is False
    assert wants_companion("both") is True
    assert wants_companion("companion") is True
    assert wants_companion("livingmemory") is False
    assert wants_livingmemory("livingmemory") is True
    assert wants_livingmemory("companion+livingmemory") is True
    assert wants_livingmemory("companion") is False
    assert wants_local("bad") is True  # 非法回退 local
    assert wants_companion("bad") is False
    assert DEFAULT_MEMORY_BACKEND == "local"


def test_build_learning_payload():
    payload = build_learning_payload(
        content="  明白了 B 站的转正答题要 60 分  ",
        source="哔哩哔哩",
        tags=["B站", "规则"],
    )
    assert payload["memory_type"] == LEARNING_MEMORY_TYPE
    assert payload["content"].startswith("【手机学习】哔哩哔哩：")
    assert "60 分" in payload["content"]
    assert payload["tags"][:2] == ["phone", "learning"]
    assert "B站" in payload["tags"]
    assert payload["payload"]["kind"] == "learning"
    assert payload["payload"]["source"] == "哔哩哔哩"
    assert payload["idempotency_key"].startswith("phone-learning:哔哩哔哩:")
    # 空内容返回空 dict
    assert build_learning_payload(content="", source="x") == {}
    assert build_learning_payload(content="   ", source="x") == {}


def test_build_app_experience_payload_from_mapping():
    payload = build_app_experience_payload(
        {
            "app": "tv.danmaku.bili",
            "label": "哔哩哔哩",
            "page": "首页布局",
            "note": "底部导航是 首页/关注/发布/会员购/我的",
            "steps": ["打开 B站", "看底部导航"],
            "version": 2,
        }
    )
    assert payload["memory_type"] == APP_MEMORY_TYPE
    assert "【App 使用经验】" in payload["content"]
    assert "哔哩哔哩（首页布局）" in payload["content"]
    assert "步骤：" in payload["content"]
    assert payload["tags"][:2] == ["phone", "app-experience"]
    assert payload["payload"]["version"] == 2
    assert payload["idempotency_key"].startswith("phone-app-exp:tv.danmaku.bili:首页布局:v2")
    assert build_app_experience_payload({"note": ""}) == {}


def test_build_app_experience_payload_from_object():
    class Entry:
        app = "com.max.xiaoheihe"
        label = "小黑盒"
        page = ""
        note = "启动后可以直接看推荐流"
        steps = ("打开小黑盒",)
        version = 1

    payload = build_app_experience_payload(Entry())
    assert "【App 使用经验】小黑盒" in payload["content"]
    assert "推荐流" in payload["content"]
    assert payload["payload"]["steps"] == ["打开小黑盒"]

def test_wants_livingmemory_and_legacy_alias():
    assert wants_livingmemory("livingmemory") is True
    assert wants_livingmemory("both") is True
    assert wants_livingmemory("companion") is False
    assert normalize_memory_backend("both") == "companion+livingmemory"


def test_build_bot_action_payload():
    text = build_bot_action_payload(content="今天学会了 B 站首页布局", source="B站")
    assert text.startswith("【自行刷手机】B站：")
    assert "首页布局" in text
    assert build_bot_action_payload(content="   ") == ""
