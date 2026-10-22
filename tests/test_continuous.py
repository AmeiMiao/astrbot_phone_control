"""core.continuous 单元测试：间隔归一化、偏好 App、轮次延迟与静默提示词。"""

from __future__ import annotations

import pytest

from core.continuous import (
    DEFAULT_GAP_SECONDS,
    MIN_GAP_SECONDS,
    build_continuous_note,
    next_run_delay,
    normalize_continuous_apps,
    normalize_gap_seconds,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (60, 60),
        ("45", 45),
        (45.9, 45),
        (MIN_GAP_SECONDS, MIN_GAP_SECONDS),
        (5, 5),
        (0, MIN_GAP_SECONDS),
        (-100, MIN_GAP_SECONDS),
        (3600, 3600),
        (3601, 3601),
        (999999, 999999),
        (None, DEFAULT_GAP_SECONDS),
        ("bad", DEFAULT_GAP_SECONDS),
        ([], DEFAULT_GAP_SECONDS),
    ],
)
def test_normalize_gap_seconds(value, expected):
    assert normalize_gap_seconds(value) == expected


def test_normalize_continuous_apps_splits_cleans_and_dedupes():
    assert normalize_continuous_apps("抖音，哔哩哔哩, 抖音 ,") == ["抖音", "哔哩哔哩"]
    assert normalize_continuous_apps(" 小红书 ， 抖音 ") == ["小红书", "抖音"]
    assert normalize_continuous_apps([" A ", "", "A", "B"]) == ["A", "B"]
    assert normalize_continuous_apps(("抖音", "抖音", "B站")) == ["抖音", "B站"]
    assert normalize_continuous_apps(None) == []
    assert normalize_continuous_apps("") == []
    assert normalize_continuous_apps(" , ，") == []
    assert normalize_continuous_apps(123) == []
    assert normalize_continuous_apps({"a": 1}) == []


def test_normalize_continuous_apps_caps_at_ten():
    assert len(normalize_continuous_apps(",".join(f"App{i}" for i in range(15)))) == 10
    assert len(normalize_continuous_apps([f"App{i}" for i in range(15)])) == 10


def test_next_run_delay():
    # 还没刷过：立即开始
    assert next_run_delay(60, None, 1_000.0) == 0.0
    # 未到间隔：返回剩余秒数
    assert next_run_delay(60, 1_000.0, 1_005.0) == 55.0
    # 刚好到点 / 已超时：立即开始
    assert next_run_delay(60, 1_000.0, 1_060.0) == 0.0
    assert next_run_delay(60, 1_000.0, 5_000.0) == 0.0
    # 间隔先归一化（0 → 下限 1 秒；已过 5 秒即立即开始）
    assert next_run_delay(0, 1_000.0, 1_005.0) == 0.0
    assert next_run_delay(0, 1_000.0, 1_000.5) == 0.5
    # 非法参数不抛异常
    assert next_run_delay("bad", None, 1_000.0) == 0.0
    assert next_run_delay(60, "bad", 1_000.0) == 0.0
    # 时钟回拨时按公式多等一点，结果始终非负
    assert next_run_delay(60, 1_010.0, 1_000.0) == 70.0


def _default_note() -> str:
    return build_continuous_note(target_tokens=0)


def test_note_contains_required_rules():
    note = _default_note()
    assert "连续自由活动的一轮" in note
    # 先查收件箱 + 被动回复门禁
    assert "phone_check_inbox" in note
    assert "被动回复" in note
    assert "phone_interaction_gate" in note
    assert "kind=passive_reply" in note
    # 验证绕过
    assert "phone_bypass_verify" in note
    # 读懂内容而不是无脑滑
    assert "无脑滑动" in note
    # 行为流程：自己的兴趣优先，中途不感兴趣可退出，分享/互动放在结束后判断
    assert "按自己的兴趣看" in note
    assert "不感兴趣直接退出" in note
    assert "分享/互动结束后再判断" in note
    assert "标题" in note and "正文" in note and "评论" in note
    # 每轮必须记录学习内容
    assert "必须至少调用一次 phone_learn" in note
    assert "1–2 条" in note
    assert "观点" in note and "知识点" in note and "梗" in note
    assert "社区规则" in note
    assert "不要照抄" in note
    assert "记不清就继续看，不要空刷" in note
    # 静默与时长
    assert "不要向用户逐步汇报" in note
    assert "静默" in note
    assert "10–30 分钟" in note
    assert "如实" in note
    # 提示词保持简短：行为流程改写后上限放宽到 720 字符
    assert len(note) <= 720


def test_note_has_no_history_narrative_by_default():
    note = _default_note()
    for word in ("上次", "上一轮", "之前刷", "历史", "【最近学到】"):
        assert word not in note


def test_note_budget_and_apps():
    note = build_continuous_note(
        target_tokens=12000, apps="抖音，哔哩哔哩", learn_tool_available=True
    )
    assert "本轮预算 12000 tokens" in note
    assert "抖音、哔哩哔哩" in note
    # 提示词保持简短：行为流程改写后上限放宽到 720 字符
    assert len(note) <= 720
    # 预算关闭时不出预算提示；非法预算也不抛异常
    assert "预算" not in build_continuous_note(target_tokens=0)
    assert "预算" not in build_continuous_note(target_tokens=-5)
    assert "预算" not in build_continuous_note(target_tokens="bad")
    # 无偏好 App 时不出现偏好提示
    assert "可以优先浏览这些 App" not in build_continuous_note(apps=None)
    assert "可以优先浏览这些 App" not in build_continuous_note(apps=" , ")


def test_note_without_learn_tool_omits_phone_learn():
    note = build_continuous_note(learn_tool_available=False)
    assert "phone_learn" not in note
    assert "phone_interaction_gate" in note
    assert "不要空刷" in note
    assert "长期记忆工具" in note


def test_note_injects_recent_learning():
    recent = "【最近学到】\n- （10-19 20:00）[哔哩哔哩] 学到了装饰器写法（#Python）"
    note = build_continuous_note(recent_learning=recent)
    assert recent in note
    assert "上几轮的学习档案" in note
    assert "不要向用户复述" in note
    # 空白注入不产生记忆段落
    assert "上几轮的学习档案" not in build_continuous_note(recent_learning="   ")
    assert "上几轮的学习档案" not in build_continuous_note(recent_learning=None)


def test_note_is_deterministic():
    assert _default_note() == _default_note()
    assert build_continuous_note(target_tokens=500, apps=["A"]) == build_continuous_note(
        target_tokens=500, apps=["A"]
    )


def test_continuous_note_includes_share_hint():
    note = build_continuous_note(
        target_tokens=0,
        apps=["小黑盒"],
        share_hint="\n分享：目标由你根据最近聊天判断。",
    )
    assert "目标由你根据最近聊天判断" in note
    assert build_continuous_note(target_tokens=0).count("分享：") == 0


def test_continuous_note_injects_novelty_hint():
    """新鲜感/厌倦感状态按需追加到持续刷提示词。"""
    hint = "【内容新鲜感】阈值 60/100：「CS2」疲劳 82/100（已看腻）。"
    note = build_continuous_note(target_tokens=0, novelty_hint=hint)
    assert "【内容新鲜感】" in note
    assert "CS2" in note
    # 空/空白不注入
    assert "【内容新鲜感】" not in build_continuous_note(novelty_hint="   ")
    assert "【内容新鲜感】" not in build_continuous_note()
