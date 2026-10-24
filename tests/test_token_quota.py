"""core.token_quota 单元测试：估算、滚动窗口、额度状态与玩手机预算。"""

from __future__ import annotations

import time

import pytest

from core.token_quota import (
    PlayBudgetRegistry,
    TokenQuota,
    estimate_messages_tokens,
    estimate_tokens,
)


def test_estimate_tokens_cjk_and_ascii():
    assert estimate_tokens("") == 0
    # 纯 ASCII 4 字符 ≈ 1 token
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2
    # 中文 2 字 ≈ 3 token（1.5/字符）
    assert estimate_tokens("你好") == 3
    # 混合文本
    assert estimate_tokens("你好abcd") == 4
    assert estimate_tokens(None) == 0


def test_estimate_messages_tokens():
    assert estimate_messages_tokens([]) == 0
    assert estimate_messages_tokens(["abcd", "你好"]) == 1 + 3


def test_record_and_window(tmp_path):
    quota = TokenQuota(tmp_path)
    now = time.time()
    quota.record(100, session="s1", source="llm_request", ts=now)
    quota.record(50, session="s1", source="tool", ts=now - 10)
    # 窗口外的旧记录不计
    quota.record(999, session="s1", source="old", ts=now - 48 * 3600)
    assert quota.used(24) == 150
    assert quota.entries(24)[0]["tokens"] == 100
    assert quota.remaining(window_hours=24, max_tokens=1000) == 850


def test_status_text_contains_numbers(tmp_path):
    quota = TokenQuota(tmp_path)
    quota.record(900, session="s1", source="x")
    text = quota.status_text(window_hours=24, max_tokens=1000)
    assert "24" in text
    assert "900" in text
    assert "1000" in text
    assert "剩余 100" in text


def test_reset_clears_records(tmp_path):
    quota = TokenQuota(tmp_path)
    quota.record(10)
    quota.record(20)
    assert quota.reset() == 2
    assert quota.used(24) == 0


def test_max_entries_trimmed(tmp_path):
    quota = TokenQuota(tmp_path, max_entries=100)
    for index in range(120):
        quota.record(1, session=f"s{index}")
    entries = quota.entries(24 * 365)
    assert len(entries) == 100
    assert entries[-1]["session"] == "s119"


def test_play_budget_registry_state_machine():
    registry = PlayBudgetRegistry()
    budget = registry.begin("cron:OtherMessage:job1", 30000)
    assert budget.completed is False
    assert registry.add("cron:OtherMessage:job1", 2500).spent_tokens == 2500
    assert registry.add("cron:OtherMessage:job1", 27500).completed is True
    assert registry.get("cron:OtherMessage:job1").remaining_tokens == 0
    assert registry.finish("cron:OtherMessage:job1") is not None
    assert registry.get("cron:OtherMessage:job1") is None


def test_play_budget_registry_reset_and_unknown():
    registry = PlayBudgetRegistry()
    first = registry.begin("s", 100)
    registry.add("s", 30)
    assert registry.get("s").spent_tokens == 30
    second = registry.begin("s", 200)
    assert second.spent_tokens == 0
    assert registry.add("not-exist", 10) is None
    registry.clear()
    assert len(registry) == 0


def test_minute_series_uses_ledger_without_point_truncation(tmp_path):
    quota = TokenQuota(tmp_path, max_entries=2000)
    # 600 点以上仍应完整统计；同一分钟内主模型、媒体和对账差额相加。
    for _ in range(650):
        quota.record(2, ts=120)
    quota.record(30, source="internal:vision", ts=145, actual=True)
    quota.record(20, source="agent_usage", ts=151, actual=True)
    quota.record(999, ts=119)
    quota.record(888, ts=201)
    assert quota.minute_series(2, now=200) == [
        {"ts": 120, "tokens": 1350}, {"ts": 180, "tokens": 0},
    ]
