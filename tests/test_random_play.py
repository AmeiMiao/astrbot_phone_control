"""core.random_play 单元测试：随机间隔、概率与随机玩提示词。"""

from __future__ import annotations

import random

from core.random_play import (
    MIN_INTERVAL_MINUTES,
    build_random_play_note,
    next_random_delay,
    normalize_intervals,
    should_play,
)


class FakeRng:
    """可预测的随机源。"""

    def __init__(self, uniform_value: float = 0.5, random_value: float = 0.0) -> None:
        self.uniform_value = uniform_value
        self.random_value = random_value
        self.uniform_calls: list[tuple[float, float]] = []

    def uniform(self, low: float, high: float) -> float:
        self.uniform_calls.append((low, high))
        return low + (high - low) * self.uniform_value

    def random(self) -> float:
        return self.random_value


def test_normalize_intervals_converges():
    assert normalize_intervals(45, 120) == (45.0, 120.0)
    assert normalize_intervals(0, 0) == (MIN_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES)
    assert normalize_intervals(-10, 5) == (MIN_INTERVAL_MINUTES, 5.0)
    assert normalize_intervals(100, 50) == (100.0, 100.0)
    # 不设上限：任意大间隔按原值使用
    assert normalize_intervals(5000, 9000) == (5000.0, 9000.0)
    assert normalize_intervals(999999, 999999) == (999999.0, 999999.0)
    # 非法值回退下限
    assert normalize_intervals("bad", None) == (MIN_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES)


def test_next_random_delay_bounds_and_unit():
    rng = FakeRng(uniform_value=0.5)
    delay = next_random_delay(45, 120, rng=rng)
    assert delay == (45 + (120 - 45) * 0.5) * 60
    assert rng.uniform_calls == [(45.0, 120.0)]
    # 越界输入也应先收敛再采样，返回秒数
    rng2 = FakeRng(uniform_value=0.0)
    assert next_random_delay(0, 0, rng=rng2) == MIN_INTERVAL_MINUTES * 60
    rng3 = FakeRng(uniform_value=1.0)
    assert next_random_delay(100, 50, rng=rng3) == 100.0 * 60


def test_should_play_uses_probability():
    assert should_play(1.0, rng=FakeRng(random_value=0.99)) is True
    assert should_play(0.0, rng=FakeRng(random_value=0.0)) is False
    assert should_play(0.5, rng=FakeRng(random_value=0.49)) is True
    assert should_play(0.5, rng=FakeRng(random_value=0.5)) is False
    # 越界概率收敛
    assert should_play(1.5, rng=FakeRng(random_value=0.99)) is True
    assert should_play(-0.5, rng=FakeRng(random_value=0.0)) is False


def test_random_note_is_persona_driven_and_silent():
    note = build_random_play_note(share_only_interesting=True)
    for phrase in (
        "随机自由活动",
        "人格",
        "phone_list_apps",
        "phone_screen",
        "静默",
        "phone_share_to_user",
        "同一内容只发一次",
        "phone_clean_background",
        "长期记忆",
    ):
        assert phrase in note, (phrase, note)
    assert "固定条数" not in note


def test_random_note_without_share_only_interesting():
    note = build_random_play_note(share_only_interesting=False)
    # 两种模式都写入「看/分享两重判断 + 必须带链接或截图」规则
    assert "看和分享是两次独立判断" in note
    assert "phone_share_to_user" in note
    assert "必须带内容链接或截图" in note
    assert "只有发现真正有趣" not in note


def test_random_note_with_preferred_apps():
    note = build_random_play_note(preferred_apps="微信，小红书")
    assert "微信" in note and "小红书" in note
    note_list = build_random_play_note(preferred_apps=["抖音"])
    assert "抖音" in note_list


def test_next_random_delay_uses_module_rng_by_default():
    # 不传 rng 时使用模块 random，返回值落在合法区间内
    random.seed(2026)
    delay = next_random_delay(45, 120)
    assert 45 * 60 <= delay <= 120 * 60


def test_build_play_note_has_budget_and_memory_sources():
    from core.random_play import build_play_note

    note = build_play_note(30000)
    for phrase in (
        "30000 tokens",
        "预算完成",
        "phone_recent_chat",
        "phone_list_apps",
        "phone_share_to_user",
        "phone_clean_background",
        "人格",
        "长期记忆",
    ):
        assert phrase in note, (phrase, note)


def test_random_note_with_target_tokens():
    note = build_random_play_note(target_tokens=20000)
    assert "20000 tokens" in note
    assert "预算完成" in note
    assert "phone_recent_chat" in note


def test_interaction_hint_only_when_enabled():
    from core.random_play import build_play_note

    random_off = build_random_play_note()
    assert "phone_interaction_gate" not in random_off
    random_on = build_random_play_note(interactions_enabled=True)
    assert "phone_interaction_gate" in random_on
    assert "phone_interaction_record" in random_on
    assert "每轮最多 1 条" in random_on
    play_off = build_play_note(3000)
    assert "phone_interaction_gate" not in play_off
    play_on = build_play_note(3000, interactions_enabled=True)
    assert "phone_interaction_gate" in play_on
    assert "3000 tokens" in play_on


def test_random_play_and_play_notes_include_share_hint():
    from core.random_play import build_play_note

    hint = "\n分享：从 phone_recent_chat 里自己选目标，私聊只选最近有交流的用户。"
    note = build_random_play_note(share_hint=hint)
    assert "自己选目标" in note
    assert "自己选目标" in build_play_note(0, share_hint=hint)
    assert "自己选目标" not in build_random_play_note()


def test_random_play_and_play_notes_include_novelty_hint():
    """新鲜感状态注入随机玩/play 提示词；看/分享两重逻辑写进静态提示。"""
    from core.random_play import build_play_note

    hint = "【内容新鲜感】阈值 60/100：「CS2」疲劳 82/100（已看腻）。"
    assert "【内容新鲜感】" in build_random_play_note(novelty_hint=hint)
    assert "【内容新鲜感】" in build_play_note(0, novelty_hint=hint)
    assert "【内容新鲜感】" not in build_random_play_note(novelty_hint="  ")
    # 静态提示必须区分「看」和「分享」，并要求链接/截图、禁止纯文字
    for note in (build_random_play_note(), build_play_note(0)):
        assert "看和分享是两次独立判断" in note
        assert "禁止只发文字" in note or "禁止纯文字" in note
        assert "phone_chat_memory_checked" not in note
        assert "phone_recent_chat" in note
