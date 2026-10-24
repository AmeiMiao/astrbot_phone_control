"""core.interactions 单元测试：许可/限额/预约/记账（不含任何 App 流程）。"""

from __future__ import annotations


def test_autonomous_plan_reports_configured_permissions_and_separate_actions():
    from core.config import PhoneConfig
    from core.interactions import interaction_plan
    security = PhoneConfig.from_mapping({"security": {"allow_posts": True, "max_posts_per_day": 2, "allow_comments": False, "allow_passive_replies": True}}).security
    plan = interaction_plan(security)
    assert "post：已授权，每日上限2条" in plan
    assert "comment：未授权" in plan and "passive_reply：已授权" in plan
    assert "无需等待用户另外下任务" in plan
    assert "decide comment" in plan and "decide reply_comment" in plan

import time
from pathlib import Path

import pytest

from core.interactions import (
    KIND_ALLOW_KEYS,
    KIND_COMMENT,
    KIND_LABELS,
    KIND_PASSIVE_REPLY,
    KIND_POST,
    InteractionError,
    InteractionStore,
    evaluate_gate,
    normalize_kind,
    normalize_summary,
)


def _now() -> float:
    """用例基准时间：固定为当天本地 12:00，避免午夜附近跨日导致用例 flaky。"""
    local = time.localtime()
    return time.mktime(
        (local.tm_year, local.tm_mon, local.tm_mday, 12, 0, 0, 0, 0, -1)
    )


def _today_noon() -> float:
    """当天本地 12:00 的时间戳，避免用例在午夜附近跨日。"""
    local = time.localtime()
    return time.mktime(
        (local.tm_year, local.tm_mon, local.tm_mday, 12, 0, 0, 0, 0, -1)
    )


def test_normalize_kind_aliases_and_invalid():
    assert normalize_kind("comment") == KIND_COMMENT
    assert normalize_kind("回复") == KIND_COMMENT
    assert normalize_kind("post") == KIND_POST
    assert normalize_kind("发帖") == KIND_POST
    with pytest.raises(InteractionError):
        normalize_kind("unsupported")
    with pytest.raises(InteractionError):
        normalize_kind("")


def test_normalize_summary_rules():
    assert normalize_summary("  看完很有感触\n  说两句  ") == "看完很有感触 说两句"
    assert normalize_summary("a\nb\tc") == "a b c"
    with pytest.raises(InteractionError):
        normalize_summary("   ")
    with pytest.raises(InteractionError):
        normalize_summary("字" * 121)
    assert len(normalize_summary("字" * 120)) == 120


def test_gate_disabled_switch():
    store = InteractionStore("/tmp/unused-interactions-disabled")
    decision = evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=False,
        max_per_day=5,
        cooldown_seconds=120,
    )
    assert decision.allowed is False
    assert "未开启评论" in decision.message
    assert "security.allow_comments" in decision.message


def test_gate_minus_one_limit_means_unlimited(tmp_path):
    """每日上限 -1 表示不限次数。"""
    store = InteractionStore(tmp_path)
    decision = evaluate_gate(
        store,
        kind=KIND_POST,
        allow=True,
        max_per_day=-1,
        cooldown_seconds=0,
        session="u",
    )
    assert decision.allowed is True
    assert "不限次数" in decision.message


def test_gate_reserve_and_single_pending(tmp_path):
    store = InteractionStore(tmp_path)
    now = _now()
    first = evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=3,
        cooldown_seconds=0,
        session="s1",
        now=now,
    )
    assert first.allowed is True
    assert "已预留 1 次评论名额" in first.message
    assert "今日剩余 2 次" in first.message
    assert "phone_interaction_record" in first.message
    assert first.reservation is not None
    second = evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=3,
        cooldown_seconds=0,
        session="s1",
        now=now + 1,
    )
    assert second.allowed is False
    assert "已有待完成" in second.message


def test_record_success_counts_and_releases(tmp_path):
    store = InteractionStore(tmp_path)
    now = _now()
    gate = evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=5,
        cooldown_seconds=0,
        session="s1",
        app="bilibili",
        now=now,
    )
    assert gate.allowed is True
    entry = store.record_result(
        kind=KIND_COMMENT,
        success=True,
        summary="视频很有启发，认真评论了一句",
        session="s1",
        app="bilibili",
        now=now + 5,
    )
    assert entry["success"] is True
    assert entry["app"] == "bilibili"
    assert store.used_today(KIND_COMMENT) == 1
    assert store.get_reservation("s1", KIND_COMMENT) is None
    assert store.last_success_at() == now + 5


def test_record_failure_releases_without_counting(tmp_path):
    store = InteractionStore(tmp_path)
    now = _now()
    evaluate_gate(
        store,
        kind=KIND_POST,
        allow=True,
        max_per_day=2,
        cooldown_seconds=0,
        session="s2",
        now=now,
    )
    entry = store.record_result(
        kind=KIND_POST,
        success=False,
        summary="遇到登录墙，停止发布",
        session="s2",
        now=now + 5,
    )
    assert entry["success"] is False
    assert store.used_today(KIND_POST) == 0
    assert store.get_reservation("s2", KIND_POST) is None
    records = store.recent(5)
    assert len(records) == 1 and records[0]["success"] is False


def test_record_without_reservation_is_rejected(tmp_path):
    store = InteractionStore(tmp_path)
    with pytest.raises(InteractionError):
        store.record_result(
            kind=KIND_COMMENT, success=True, summary="无预约", session="x"
        )
    with pytest.raises(InteractionError):
        store.record_result(
            kind=KIND_POST, success=False, summary="无预约", session="x"
        )


def test_daily_limit_checked_before_cooldown(tmp_path):
    store = InteractionStore(tmp_path)
    now = _now()
    for index in range(2):
        session = f"s{index}"
        gate = evaluate_gate(
            store,
            kind=KIND_COMMENT,
            allow=True,
            max_per_day=2,
            cooldown_seconds=0,
            session=session,
            now=now + index * 10,
        )
        assert gate.allowed is True
        store.record_result(
            kind=KIND_COMMENT,
            success=True,
            summary=f"第 {index + 1} 条评论",
            session=session,
            now=now + index * 10 + 1,
        )
    decision = evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=2,
        cooldown_seconds=120,
        session="s3",
        now=now + 20,
    )
    assert decision.allowed is False
    assert "已达上限" in decision.message


def test_cooldown_between_interactions(tmp_path):
    store = InteractionStore(tmp_path)
    now = _now()
    evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=5,
        cooldown_seconds=0,
        session="s1",
        now=now,
    )
    store.record_result(
        kind=KIND_COMMENT,
        success=True,
        summary="评论成功",
        session="s1",
        now=now,
    )
    blocked = evaluate_gate(
        store,
        kind=KIND_POST,
        allow=True,
        max_per_day=2,
        cooldown_seconds=120,
        session="s2",
        now=now + 30,
    )
    assert blocked.allowed is False
    assert "操作太快" in blocked.message
    assert "还需约 90 秒" in blocked.message
    ok = evaluate_gate(
        store,
        kind=KIND_POST,
        allow=True,
        max_per_day=2,
        cooldown_seconds=120,
        session="s2",
        now=now + 121,
    )
    assert ok.allowed is True


def test_yesterday_records_do_not_count(tmp_path):
    store = InteractionStore(tmp_path)
    now = _now()
    evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=1,
        cooldown_seconds=0,
        session="old",
        now=now - 86400,
    )
    store.record_result(
        kind=KIND_COMMENT,
        success=True,
        summary="昨天的评论",
        session="old",
        now=now - 86400 + 1,
    )
    assert store.used_today(KIND_COMMENT, now=now) == 0
    decision = evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=1,
        cooldown_seconds=0,
        session="new",
        now=now,
    )
    assert decision.allowed is True


def test_stale_pending_reservation_expires(tmp_path):
    store = InteractionStore(tmp_path)
    now = _now()
    evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=3,
        cooldown_seconds=0,
        session="s1",
        now=now - 3 * 3600,
    )
    decision = evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=3,
        cooldown_seconds=0,
        session="s1",
        now=now,
    )
    assert decision.allowed is True
    assert decision.message.startswith("已预留")


def test_records_trimmed_to_max_entries(tmp_path):
    store = InteractionStore(tmp_path, max_entries=100)
    now = _now()
    for index in range(120):
        session = f"s{index}"
        gate = evaluate_gate(
            store,
            kind=KIND_COMMENT,
            allow=True,
            max_per_day=200,
            cooldown_seconds=0,
            session=session,
            now=now + index,
        )
        assert gate.allowed is True
        store.record_result(
            kind=KIND_COMMENT,
            success=True,
            summary=f"第 {index} 条",
            session=session,
            now=now + index,
        )
    assert len(store._read_records()) == 100
    assert store.used_today(KIND_COMMENT, now=now + 119) == 100


def test_status_text_contains_counts_and_recent(tmp_path):
    store = InteractionStore(tmp_path)
    now = _now()
    evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=5,
        cooldown_seconds=0,
        session="s1",
        app="douyin",
        now=now,
    )
    store.record_result(
        kind=KIND_COMMENT,
        success=True,
        summary="这条视频的评论有点意思，我也说了一句自己的看法",
        session="s1",
        app="douyin",
        now=now,
    )
    text = store.status_text(
        allow_comments=True,
        allow_posts=False,
        max_comments=5,
        max_posts=2,
        cooldown_seconds=120,
        limit=5,
    )
    assert "评论：已开启" in text
    assert "今日已用 1/5" in text
    assert "发帖：未开启" in text
    assert "冷却" in text and "120" in text
    assert "[douyin] 评论成功" in text
    assert "这条视频" in text


# ----------------------------------------------------------------------
# 被动回复（passive_reply）：别名、独立冷却、限额与状态文本
# ----------------------------------------------------------------------

_PASSIVE_ALIASES = ("passive_reply", "passive", "被动回复", "被动评论", "回评")


def test_passive_reply_alias_and_kind_metadata():
    assert KIND_PASSIVE_REPLY == "passive_reply"
    assert KIND_LABELS[KIND_PASSIVE_REPLY] == "被动回复"
    assert KIND_ALLOW_KEYS[KIND_PASSIVE_REPLY] == "security.allow_passive_replies"
    for alias in _PASSIVE_ALIASES + ("Passive_Reply", " 被动回复 "):
        assert normalize_kind(alias) == KIND_PASSIVE_REPLY
    # reply/回复 仍是主动评论（comment）的别名
    assert normalize_kind("reply") == KIND_COMMENT
    assert normalize_kind("回复") == KIND_COMMENT
    assert normalize_kind("comment") == KIND_COMMENT
    assert normalize_kind("评论") == KIND_COMMENT
    assert normalize_kind("主动评论") == KIND_COMMENT
    assert normalize_kind("post") == KIND_POST
    with pytest.raises(InteractionError):
        normalize_kind("passive_like")


def test_gate_passive_reply_disabled_switch(tmp_path):
    store = InteractionStore(tmp_path)
    decision = evaluate_gate(
        store,
        kind=KIND_PASSIVE_REPLY,
        allow=False,
        max_per_day=10,
        cooldown_seconds=0,
        cooldown_kind=KIND_PASSIVE_REPLY,
    )
    assert decision.allowed is False
    assert "未开启被动回复" in decision.message
    assert "security.allow_passive_replies" in decision.message


def test_passive_reply_reserve_record_and_failure_release(tmp_path):
    store = InteractionStore(tmp_path)
    now = _today_noon()
    gate = evaluate_gate(
        store,
        kind=KIND_PASSIVE_REPLY,
        allow=True,
        max_per_day=3,
        cooldown_seconds=0,
        cooldown_kind=KIND_PASSIVE_REPLY,
        session="p1",
        app="bilibili",
        now=now,
    )
    assert gate.allowed is True
    assert "已预留 1 次被动回复名额" in gate.message
    assert "今日剩余 2 次" in gate.message
    assert store.pending_count(KIND_PASSIVE_REPLY, now) == 1
    entry = store.record_result(
        kind=KIND_PASSIVE_REPLY,
        success=True,
        summary="在评论区回复了楼主的提问",
        session="p1",
        app="bilibili",
        now=now + 3,
    )
    assert entry["kind"] == KIND_PASSIVE_REPLY
    assert store.used_today(KIND_PASSIVE_REPLY, now=now + 3) == 1
    assert store.get_reservation("p1", KIND_PASSIVE_REPLY) is None
    # 失败同样释放预约且不计当日用量
    assert evaluate_gate(
        store,
        kind=KIND_PASSIVE_REPLY,
        allow=True,
        max_per_day=3,
        cooldown_seconds=0,
        cooldown_kind=KIND_PASSIVE_REPLY,
        session="p2",
        now=now + 10,
    ).allowed is True
    failed = store.record_result(
        kind=KIND_PASSIVE_REPLY,
        success=False,
        summary="回复框被限流，放弃",
        session="p2",
        now=now + 11,
    )
    assert failed["success"] is False
    assert store.used_today(KIND_PASSIVE_REPLY, now=now + 11) == 1
    assert store.get_reservation("p2", KIND_PASSIVE_REPLY) is None


def test_passive_reply_cooldown_independent_from_comment(tmp_path):
    store = InteractionStore(tmp_path)
    now = _today_noon()
    # 主动评论成功一次
    comment_gate = evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=5,
        cooldown_seconds=0,
        session="c1",
        now=now,
    )
    assert comment_gate.allowed is True
    store.record_result(
        kind=KIND_COMMENT,
        success=True,
        summary="主动评论一句",
        session="c1",
        now=now,
    )
    # 主动评论的成功不阻塞被动回复：独立冷却只看被动回复自身的成功记录
    passive_gate = evaluate_gate(
        store,
        kind=KIND_PASSIVE_REPLY,
        allow=True,
        max_per_day=5,
        cooldown_seconds=120,
        cooldown_kind=KIND_PASSIVE_REPLY,
        session="p1",
        now=now + 30,
    )
    assert passive_gate.allowed is True
    store.record_result(
        kind=KIND_PASSIVE_REPLY,
        success=True,
        summary="回复了别人的评论",
        session="p1",
        now=now + 31,
    )
    # 被动回复自身成功后按独立冷却阻塞
    blocked = evaluate_gate(
        store,
        kind=KIND_PASSIVE_REPLY,
        allow=True,
        max_per_day=5,
        cooldown_seconds=120,
        cooldown_kind=KIND_PASSIVE_REPLY,
        session="p2",
        now=now + 60,
    )
    assert blocked.allowed is False
    assert "操作太快" in blocked.message
    assert "距上次成功被动回复" in blocked.message
    assert "还需约 91 秒" in blocked.message
    # 主动评论/发帖仍共用全局冷却：被动回复的成功同样计入全局最近成功
    comment_blocked = evaluate_gate(
        store,
        kind=KIND_COMMENT,
        allow=True,
        max_per_day=5,
        cooldown_seconds=120,
        session="c2",
        now=now + 60,
    )
    assert comment_blocked.allowed is False
    assert "距上次成功互动" in comment_blocked.message


def test_passive_reply_cooldown_ignores_post_success(tmp_path):
    store = InteractionStore(tmp_path)
    now = _today_noon()
    post_gate = evaluate_gate(
        store,
        kind=KIND_POST,
        allow=True,
        max_per_day=3,
        cooldown_seconds=0,
        session="post1",
        now=now,
    )
    assert post_gate.allowed is True
    store.record_result(
        kind=KIND_POST,
        success=True,
        summary="发了一条动态",
        session="post1",
        now=now,
    )
    decision = evaluate_gate(
        store,
        kind=KIND_PASSIVE_REPLY,
        allow=True,
        max_per_day=3,
        cooldown_seconds=600,
        cooldown_kind=KIND_PASSIVE_REPLY,
        session="reply1",
        now=now + 10,
    )
    assert decision.allowed is True


def test_passive_reply_daily_limit_and_pending(tmp_path):
    store = InteractionStore(tmp_path)
    now = _today_noon()
    first = evaluate_gate(
        store,
        kind=KIND_PASSIVE_REPLY,
        allow=True,
        max_per_day=1,
        cooldown_seconds=0,
        cooldown_kind=KIND_PASSIVE_REPLY,
        session="p1",
        now=now,
    )
    assert first.allowed is True
    # 预约占掉唯一名额
    second = evaluate_gate(
        store,
        kind=KIND_PASSIVE_REPLY,
        allow=True,
        max_per_day=1,
        cooldown_seconds=0,
        cooldown_kind=KIND_PASSIVE_REPLY,
        session="p2",
        now=now + 1,
    )
    assert second.allowed is False
    assert "名额已满" in second.message
    store.record_result(
        kind=KIND_PASSIVE_REPLY,
        success=True,
        summary="回复完成",
        session="p1",
        now=now + 2,
    )
    third = evaluate_gate(
        store,
        kind=KIND_PASSIVE_REPLY,
        allow=True,
        max_per_day=1,
        cooldown_seconds=0,
        cooldown_kind=KIND_PASSIVE_REPLY,
        session="p3",
        now=now + 3,
    )
    assert third.allowed is False
    assert "已达上限 1 次" in third.message


def test_last_success_at_filters_by_kind(tmp_path):
    store = InteractionStore(tmp_path)
    now = _today_noon()
    store.reserve(kind=KIND_COMMENT, session="c", now=now)
    store.record_result(
        kind=KIND_COMMENT, success=True, summary="评论", session="c", now=now + 10
    )
    store.reserve(kind=KIND_PASSIVE_REPLY, session="p", now=now + 20)
    store.record_result(
        kind=KIND_PASSIVE_REPLY,
        success=True,
        summary="被动回复",
        session="p",
        now=now + 30,
    )
    store.reserve(kind=KIND_POST, session="q", now=now + 40)
    store.record_result(
        kind=KIND_POST, success=False, summary="发帖失败", session="q", now=now + 50
    )
    assert store.last_success_at() == now + 30
    assert store.last_success_at(kind=KIND_COMMENT) == now + 10
    assert store.last_success_at(kind=KIND_PASSIVE_REPLY) == now + 30
    assert store.last_success_at(kind=KIND_POST) is None


def test_status_text_passive_line_and_backward_compatible_defaults(tmp_path):
    store = InteractionStore(tmp_path)
    now = _today_noon()
    # 只传原有参数的调用：新增字段用默认值，不报错
    legacy = store.status_text(
        allow_comments=True,
        allow_posts=True,
        max_comments=5,
        max_posts=2,
        cooldown_seconds=120,
        now=now,
    )
    assert "被动回复：未开启" in legacy
    assert "security.allow_passive_replies" in legacy
    assert "今日已用 0，不限次数" in legacy
    assert "待完成 0" in legacy
    assert "独立冷却 无冷却" in legacy
    # 打开被动回复并成功一次、再预约一次后，计数/待完成/独立冷却/最近记录都生效
    assert evaluate_gate(
        store,
        kind=KIND_PASSIVE_REPLY,
        allow=True,
        max_per_day=8,
        cooldown_seconds=0,
        cooldown_kind=KIND_PASSIVE_REPLY,
        session="p1",
        app="douyin",
        now=now,
    ).allowed is True
    store.record_result(
        kind=KIND_PASSIVE_REPLY,
        success=True,
        summary="回复了评论区的问题",
        session="p1",
        app="douyin",
        now=now + 2,
    )
    assert evaluate_gate(
        store,
        kind=KIND_PASSIVE_REPLY,
        allow=True,
        max_per_day=8,
        cooldown_seconds=0,
        cooldown_kind=KIND_PASSIVE_REPLY,
        session="p2",
        now=now + 3,
    ).allowed is True
    text = store.status_text(
        allow_comments=True,
        allow_posts=False,
        max_comments=5,
        max_posts=2,
        cooldown_seconds=120,
        allow_passive_replies=True,
        max_passive_replies=8,
        passive_cooldown_seconds=60,
        now=now + 3,
    )
    assert "被动回复：已开启" in text
    assert "今日已用 1/8，待完成 1；独立冷却 60 秒" in text
    assert "[douyin] 被动回复成功" in text
    # 原有评论/发帖/冷却/最近记录行不受影响
    assert "评论：已开启" in text
    assert "今日已用 0/5" in text
    assert "发帖：未开启" in text
    assert "冷却：两次成功互动间隔 120 秒" in text


def test_gate_unlimited_and_no_cooldown(tmp_path):
    """上限 -1、冷却 0：连续多次互动都可预约。"""
    store = InteractionStore(tmp_path)
    now = _now()
    for index in range(5):
        gate = evaluate_gate(
            store,
            kind=KIND_COMMENT,
            allow=True,
            max_per_day=-1,
            cooldown_seconds=0,
            session=f"unlimited-{index}",
            now=now + index,
        )
        assert gate.allowed is True
        store.record_result(
            kind=KIND_COMMENT,
            success=True,
            summary=f"第 {index + 1} 条",
            session=f"unlimited-{index}",
            now=now + index,
        )
    assert store.used_today(KIND_COMMENT) == 5


def test_light_quota_reservations_failure_success_and_restart(tmp_path):
    from core.interactions import KIND_LIKE, KIND_FAVORITE
    store = InteractionStore(tmp_path)
    def gate(kind, run="r1", limit=1):
        return evaluate_gate(store, kind=kind, allow=True, max_per_day=-1, cooldown_seconds=0,
                             run=run, session=run, max_per_session=limit)
    assert gate(KIND_LIKE).allowed
    assert not gate(KIND_FAVORITE).allowed
    store.record_result(kind=KIND_LIKE, success=False, summary="失败", session="r1", run="r1")
    assert gate(KIND_FAVORITE).allowed
    with pytest.raises(InteractionError, match="其他轮次"):
        store.record_result(kind=KIND_FAVORITE, success=True, summary="成功", session="r1", run="other")
    store.record_result(kind=KIND_FAVORITE, success=True, summary="成功", session="r1", run="r1")
    store = InteractionStore(tmp_path)
    assert store.run_usage("r1") == (1, 0)
    assert not gate(KIND_LIKE).allowed
    assert gate(KIND_LIKE, "r2").allowed
    store.finish_run("r2")
    assert not store.reservations()
    assert not gate(KIND_LIKE, "r3", 0).allowed


def test_light_success_does_not_trigger_comment_cooldown(tmp_path):
    from core.interactions import KIND_LIKE
    store = InteractionStore(tmp_path)
    store.reserve(kind=KIND_LIKE, session="s", run="r")
    store.record_result(kind=KIND_LIKE, success=True, summary="点赞成功", session="s", run="r")
    assert evaluate_gate(store, kind=KIND_COMMENT, allow=True, max_per_day=-1, cooldown_seconds=100).allowed


def test_reservation_save_failure_denies_and_record_failure_keeps_reservation(tmp_path, monkeypatch):
    from core.interactions import KIND_LIKE
    store = InteractionStore(tmp_path)
    def fail(*args, **kwargs):
        raise OSError("disk full")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", fail)
        decision = evaluate_gate(store, kind=KIND_LIKE, allow=True, max_per_day=-1, cooldown_seconds=0, run="r")
        assert not decision.allowed and "保存互动预约失败" in decision.message
    store.reserve(kind=KIND_LIKE, session="s", run="r")
    with monkeypatch.context() as patch:
        patch.setattr(store, "_write_records", lambda records: (_ for _ in ()).throw(InteractionError("保存互动记录失败")))
        with pytest.raises(InteractionError):
            store.record_result(kind=KIND_LIKE, success=True, summary="点赞", session="s", run="r")
    assert store.get_reservation("s", KIND_LIKE)
    assert store.used_today(KIND_LIKE) == 0
    with monkeypatch.context() as patch:
        patch.setattr(store, "_save_reservations", lambda records: (_ for _ in ()).throw(InteractionError("保存互动预约失败")))
        with pytest.raises(InteractionError):
            store.record_result(kind=KIND_LIKE, success=True, summary="点赞", session="s", run="r")
    store.record_result(kind=KIND_LIKE, success=True, summary="点赞", session="s", run="r")
    assert store.used_today(KIND_LIKE) == 1
    assert not store.reservations()


@pytest.mark.parametrize("kind", [KIND_COMMENT, KIND_POST, KIND_PASSIVE_REPLY])
def test_daily_minus_one_unlimited_and_zero_denied(tmp_path, kind):
    store = InteractionStore(tmp_path)
    zero = evaluate_gate(store, kind=kind, allow=True, max_per_day=0, cooldown_seconds=0)
    assert not zero.allowed
    unlimited = evaluate_gate(store, kind=kind, allow=True, max_per_day=-1, cooldown_seconds=0)
    assert unlimited.allowed and "不限次数" in unlimited.message
    denied = evaluate_gate(store, kind=kind, allow=False, max_per_day=-1, cooldown_seconds=0)
    assert not denied.allowed
