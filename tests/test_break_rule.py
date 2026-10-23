"""规则弹性的预算热切换、实际记账与兴趣/新鲜感回归。"""

from types import SimpleNamespace

import pytest

from core.config import PhoneConfig
from core.config_apply import LIVE_KEYS
from core.continuous import build_continuous_note
from core.novelty import NoveltySettings, NoveltyStore
from core.play_flow import FLOW_RULE
from core.random_play import build_play_note, build_random_play_note
from core.service import PhoneService
from core.token_quota import PlayBudget


def service(tmp_path, *, enabled=False):
    return PhoneService(
        raw_config={"auto": {"break_rule": enabled}},
        adb=None, actions=None, diary=None, data_dir=tmp_path,
    )


def event(session="one", target=10000):
    return SimpleNamespace(
        unified_msg_origin=session,
        get_extra=lambda key: {"play_task": True, "target_tokens": target}
        if key == "cron_payload" else None,
    )


def test_default_and_live_config():
    assert not PhoneConfig.from_mapping({}).auto.break_rule
    assert PhoneConfig.from_mapping({"auto": {"break_rule": "true"}}).auto.break_rule
    assert not PhoneConfig.from_mapping({"auto": {"break_rule": "false"}}).auto.break_rule
    assert "auto.break_rule" in LIVE_KEYS


@pytest.mark.parametrize("target,limit", [(1, 2), (5, 6), (6, 8), (10000, 12000)])
def test_budget_has_finite_extension(target, limit):
    budget = PlayBudget("one", target, spent_tokens=target, break_rule=True)
    assert not budget.completed
    assert budget.limit_tokens == limit
    budget.spent_tokens = limit
    assert budget.completed and budget.remaining_tokens == 0


def test_live_toggle_preserves_spending_and_separate_sessions(tmp_path):
    s = service(tmp_path)
    e = event()
    budget = s.ensure_play_budget(e)
    s.record_tokens(10000, session="one", play=True)
    assert budget.completed
    s._raw_config["auto"]["break_rule"] = True
    assert s.ensure_play_budget(e) is budget
    assert not budget.completed and budget.spent_tokens == 10000
    s.record_tokens(1999, session="one", play=True)
    assert not s.ensure_play_budget(e).completed
    s.record_tokens(1, session="one", play=True)
    assert s.ensure_play_budget(e).completed
    assert s.quota.used(24) == 12000
    assert s.ensure_play_budget(event("two")).spent_tokens == 0
    s._raw_config["auto"]["break_rule"] = False
    assert s.ensure_play_budget(e) is budget
    assert budget.spent_tokens == 12000 and budget.limit_tokens == 10000


def test_global_quota_and_zero_task_budget(tmp_path):
    s = service(tmp_path, enabled=True)
    assert s.ensure_play_budget(event(target=0)) is None
    s._raw_config["quota"] = {"enabled": True, "max_tokens": 10000}
    s.record_tokens(10000, session="one", play=True)
    assert s.quota_error_text()


@pytest.mark.parametrize("builder", [
    lambda enabled: build_play_note(10000, break_rule=enabled),
    lambda enabled: build_random_play_note(target_tokens=10000, break_rule=enabled),
    lambda enabled: build_continuous_note(target_tokens=10000, break_rule=enabled),
])
def test_task_prompts_support_truthful_low_interest(builder):
    strict = builder(False)
    flexible = builder(True)
    assert "break_rule 已开启" not in strict
    assert "break_rule 已开启" in flexible
    assert "20%" in flexible and "无需编造兴趣" in flexible
    assert FLOW_RULE not in flexible
    assert "不感兴趣直接退出" not in flexible
    assert "感兴趣再decide continue" not in flexible


def test_novelty_remains_recorded_but_threshold_is_advisory(tmp_path):
    settings = NoveltySettings(threshold=10, gain=20)
    store = NoveltyStore(tmp_path)
    match = store.record("测试", "星际鲸鱼漫游宇宙", settings)
    assert match.bored
    assert "优先换主题" in store.render_for_prompt(settings)
    flexible = store.render_for_prompt(settings, break_rule=True)
    assert "可按真实意愿继续看" in flexible
    assert "优先换主题" not in flexible
    assert store.entries(settings)[0].hits == 1
