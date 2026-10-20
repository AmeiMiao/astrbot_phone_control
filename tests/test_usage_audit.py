"""token 精确对账数据层单元测试：usage 明细解析、provider_stats 汇总与额度条目。

覆盖 ``core.token_quota`` 的 usage 扩展（UsageBreakdown / record 新字段 /
actual_usage / estimated_usage）与 ``core.usage_audit`` 的统计行汇总、run 级查询
重试、窗口级查询。测试不依赖 AstrBot：``astrbot``/``sqlmodel`` 只在真实查询函数
内部延迟导入，这里全部注入假 query。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from core.token_quota import (
    IMAGE_TOKEN_ESTIMATE,
    TokenQuota,
    UsageBreakdown,
    estimate_fallback_usage,
    usage_breakdown,
    usage_is_empty,
)
from core.usage_audit import (
    AgentUsage,
    _filter_run_rows,
    fetch_agent_run_usage,
    fetch_window_usage,
    usage_from_rows,
)


def run(coro):
    """同步测试里运行协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# usage_from_rows：provider_stats 统计行汇总
# ---------------------------------------------------------------------------


def test_usage_from_rows_objects():
    rows = [
        SimpleNamespace(
            token_input_other=10,
            token_input_cached=100,
            token_output=20,
            provider_model="model-b",
        ),
        SimpleNamespace(
            token_input_other=5,
            token_input_cached=0,
            token_output=7,
            provider_model="model-a",
        ),
    ]
    usage = usage_from_rows(rows)
    assert isinstance(usage, AgentUsage)
    assert usage.input_other == 15
    assert usage.input_cached == 100
    assert usage.output == 27
    assert usage.total == 142
    assert usage.calls == 2
    assert usage.provider_models == ("model-a", "model-b")


def test_usage_from_rows_dicts_and_duplicate_models():
    rows = [
        {
            "token_input_other": 1,
            "token_input_cached": 2,
            "token_output": 3,
            "provider_model": "same-model",
        },
        {
            "token_input_other": 4,
            "token_input_cached": 5,
            "token_output": 6,
            "provider_model": "same-model",
        },
    ]
    usage = usage_from_rows(rows)
    assert usage.calls == 2
    assert usage.input_other == 5
    assert usage.input_cached == 7
    assert usage.output == 9
    assert usage.provider_models == ("same-model",)


def test_usage_from_rows_mixed_rows_and_empty_rows_ignored():
    rows = [
        None,
        {},
        SimpleNamespace(provider_model="only-model"),  # 没有 token 字段 → 空行忽略
        SimpleNamespace(
            token_input_other=3,
            token_input_cached=0,
            token_output=1,
            provider_model=None,
        ),
        {
            "token_input_other": 2,
            "token_input_cached": 1,
            "token_output": 0,
            "provider_model": "model-n",
        },
    ]
    usage = usage_from_rows(rows)
    assert usage.calls == 2
    assert usage.input_other == 5
    assert usage.input_cached == 1
    assert usage.output == 1
    assert usage.provider_models == ("model-n",)


def test_usage_from_rows_empty_and_null_values():
    assert usage_from_rows([]) == AgentUsage()
    assert usage_from_rows(()) == AgentUsage()
    assert usage_from_rows([None, {}]) == AgentUsage()
    usage = usage_from_rows(
        [
            {
                "token_input_other": None,
                "token_input_cached": "",
                "token_output": "12",
            }
        ]
    )
    assert usage.calls == 1
    assert usage.input_other == 0
    assert usage.output == 12


# ---------------------------------------------------------------------------
# _filter_run_rows：排除同一会话上一次 run 的统计行（5s 查询容差的保护）
# ---------------------------------------------------------------------------


def test_filter_run_rows_excludes_previous_run():
    started_wall = 1000.0
    previous_run = {
        "token_input_other": 99,
        "token_input_cached": 0,
        "token_output": 1,
        "end_time": 900.0,
    }
    current_run = SimpleNamespace(
        token_input_other=1,
        token_input_cached=2,
        token_output=3,
        end_time=1005.0,
    )
    unfinished_run = {
        # error/abort 等未正常收尾的 run：end_time=0 无法判断，保留
        "token_input_other": 4,
        "token_input_cached": 0,
        "token_output": 0,
        "end_time": 0,
    }
    selected = _filter_run_rows(
        [previous_run, current_run, unfinished_run], started_wall
    )
    assert selected == [current_run, unfinished_run]
    usage = usage_from_rows(selected)
    assert usage.calls == 2
    assert usage.total == 10


def test_filter_run_rows_keeps_boundary_and_unknown_end_time():
    started_wall = 1000.0
    rows = [
        {"token_input_other": 1, "end_time": 1000.0},  # 恰等于起点：保留
        {"token_input_other": 2},  # 缺 end_time 字段：保留
        {"token_input_other": 3, "end_time": None},  # end_time=None：保留
        {"token_input_other": 4, "end_time": 999.9},  # 早于起点：排除
        None,  # 空行：忽略
    ]
    selected = _filter_run_rows(rows, started_wall)
    assert [row["token_input_other"] for row in selected] == [1, 2, 3]


# ---------------------------------------------------------------------------
# fetch_agent_run_usage：run 结束后的短重试查询
# ---------------------------------------------------------------------------


def test_fetch_agent_run_usage_retries_until_ready():
    calls: list[tuple[str, float]] = []
    sleeps: list[float] = []

    async def query(umo, started_wall):
        calls.append((umo, started_wall))
        if len(calls) < 3:
            return []
        return [
            SimpleNamespace(
                token_input_other=10,
                token_input_cached=20,
                token_output=5,
                provider_model="model-x",
            )
        ]

    async def sleep(seconds):
        sleeps.append(seconds)

    usage = run(
        fetch_agent_run_usage(
            "umo-1",
            1234.5,
            retries=5,
            delay=0.25,
            query=query,
            sleep=sleep,
        )
    )
    assert usage is not None
    assert usage.total == 35
    assert usage.calls == 1
    assert usage.provider_models == ("model-x",)
    # started_wall 原样透传给查询函数
    assert calls == [("umo-1", 1234.5), ("umo-1", 1234.5), ("umo-1", 1234.5)]
    assert sleeps == [0.25, 0.25]


def test_fetch_agent_run_usage_exhausted_returns_none():
    calls: list[str] = []
    sleeps: list[float] = []

    async def query(umo, started_wall):
        calls.append(umo)
        return []

    async def sleep(seconds):
        sleeps.append(seconds)

    usage = run(
        fetch_agent_run_usage("umo", 1.0, retries=3, query=query, sleep=sleep)
    )
    assert usage is None
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_fetch_agent_run_usage_query_error_then_success():
    calls: list[str] = []

    async def query(umo, started_wall):
        calls.append(umo)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return [{"token_input_other": 1, "token_input_cached": 0, "token_output": 1}]

    async def sleep(seconds):
        return None

    usage = run(
        fetch_agent_run_usage("umo", 9.0, retries=2, query=query, sleep=sleep)
    )
    assert usage is not None
    assert usage.total == 2
    assert len(calls) == 2


def test_fetch_agent_run_usage_query_always_error_returns_none():
    async def query(umo, started_wall):
        raise RuntimeError("no database")

    async def sleep(seconds):
        return None

    usage = run(
        fetch_agent_run_usage("umo", 1.0, retries=2, query=query, sleep=sleep)
    )
    assert usage is None


def test_fetch_agent_run_usage_zero_total_is_not_ready():
    calls: list[str] = []

    async def query(umo, started_wall):
        calls.append(umo)
        if len(calls) == 1:
            # 行已写入但累计 usage 仍为 0：视为未就绪，继续重试
            return [
                {
                    "token_input_other": 0,
                    "token_input_cached": 0,
                    "token_output": 0,
                }
            ]
        return [{"token_input_other": 2, "token_input_cached": 0, "token_output": 1}]

    async def sleep(seconds):
        return None

    usage = run(
        fetch_agent_run_usage("umo", 1.0, retries=3, query=query, sleep=sleep)
    )
    assert usage is not None
    assert usage.total == 3
    assert len(calls) == 2


def test_fetch_agent_run_usage_zero_delay_skips_sleep():
    async def query(umo, started_wall):
        return []

    async def sleep(seconds):
        raise AssertionError("delay=0 时不应等待")

    usage = run(
        fetch_agent_run_usage("umo", 1.0, retries=2, delay=0, query=query, sleep=sleep)
    )
    assert usage is None


# ---------------------------------------------------------------------------
# fetch_window_usage：/phone quota audit 窗口汇总
# ---------------------------------------------------------------------------


def test_fetch_window_usage_empty_umos_skips_query():
    called = False

    async def query(umos, started_wall):
        nonlocal called
        called = True
        return []

    assert run(fetch_window_usage([], 24, now=1000.0, query=query)) == AgentUsage()
    assert run(fetch_window_usage(["", "   "], 24, now=1000.0, query=query)) == AgentUsage()
    assert called is False


def test_fetch_window_usage_aggregates_multiple_umos():
    seen: dict[str, object] = {}

    async def query(umos, started_wall):
        seen["umos"] = list(umos)
        seen["started_wall"] = started_wall
        return [
            {
                "token_input_other": 10,
                "token_input_cached": 100,
                "token_output": 20,
                "provider_model": "model-b",
            },
            {
                "token_input_other": 1,
                "token_input_cached": 2,
                "token_output": 3,
                "provider_model": "model-a",
            },
            {
                "token_input_other": 4,
                "token_input_cached": 0,
                "token_output": 5,
                "provider_model": "model-b",
            },
        ]

    usage = run(
        fetch_window_usage(["umo-1", "umo-2", "umo-1"], 2.5, now=10000.0, query=query)
    )
    assert usage is not None
    assert seen["umos"] == ["umo-1", "umo-2"]
    assert float(seen["started_wall"]) == pytest.approx(10000.0 - 2.5 * 3600.0)
    assert usage.input_other == 15
    assert usage.input_cached == 102
    assert usage.output == 28
    assert usage.calls == 3
    assert usage.provider_models == ("model-a", "model-b")


def test_fetch_window_usage_query_error_returns_none():
    async def query(umos, started_wall):
        raise RuntimeError("database is locked")

    usage = run(fetch_window_usage(["umo-1"], 24, now=1.0, query=query))
    assert usage is None


def test_fetch_window_usage_empty_rows_returns_empty_usage():
    async def query(umos, started_wall):
        return []

    usage = run(fetch_window_usage(["umo-1"], 24, now=1.0, query=query))
    assert usage == AgentUsage()
    assert usage.calls == 0


# ---------------------------------------------------------------------------
# usage_breakdown：AstrBot TokenUsage / OpenAI usage 形态
# ---------------------------------------------------------------------------


def test_usage_breakdown_astrbot_token_usage_object():
    usage = SimpleNamespace(input_other=120, input_cached=30, output=45)
    breakdown = usage_breakdown(usage)
    assert breakdown == UsageBreakdown(input_other=120, input_cached=30, output=45)
    assert breakdown.total == 195
    assert breakdown.as_fields() == {
        "input_other": 120,
        "input_cached": 30,
        "output": 45,
    }


def test_usage_breakdown_dict_forms():
    assert usage_breakdown(
        {"input_other": 10, "input_cached": 20, "output": 30}
    ) == UsageBreakdown(10, 20, 30)
    assert usage_breakdown(
        {"prompt_tokens": 100, "completion_tokens": 20}
    ) == UsageBreakdown(100, 0, 20)
    assert usage_breakdown(
        {"total_tokens": 120, "completion_tokens": 20}
    ) == UsageBreakdown(100, 0, 20)
    assert usage_breakdown({"total_tokens": 100}) == UsageBreakdown(100, 0, 0)
    # 有明细时优先使用明细，不被 total_tokens 覆盖
    assert usage_breakdown(
        {
            "input_other": 7,
            "output": 3,
            "completion_tokens": 88,
            "total_tokens": 999,
        }
    ) == UsageBreakdown(7, 0, 3)


def test_usage_breakdown_missing_and_abnormal_usage():
    assert usage_breakdown(None) == UsageBreakdown()
    assert usage_breakdown({}) == UsageBreakdown()
    assert usage_breakdown("not-a-usage") == UsageBreakdown()

    class BrokenUsage:
        def __getattr__(self, name):
            raise RuntimeError("字段不可用")

    assert usage_breakdown(BrokenUsage()) == UsageBreakdown()


def test_usage_breakdown_clamps_and_coerces_values():
    assert usage_breakdown(
        {"input_other": -5, "input_cached": "3", "output": 2.6}
    ) == UsageBreakdown(0, 3, 3)
    assert usage_breakdown(
        {"input_other": float("nan"), "output": float("inf")}
    ) == UsageBreakdown()


# ---------------------------------------------------------------------------
# estimate_fallback_usage / usage_is_empty
# ---------------------------------------------------------------------------


def test_estimate_fallback_usage_text_and_images():
    assert estimate_fallback_usage() == UsageBreakdown()
    assert usage_is_empty(estimate_fallback_usage()) is True

    breakdown = estimate_fallback_usage(prompt_text="你好", completion_text="abcd")
    assert breakdown.input_other == 3
    assert breakdown.output == 1
    assert breakdown.input_cached == 0
    assert usage_is_empty(breakdown) is False

    with_images = estimate_fallback_usage(prompt_text="abcd", image_count=2)
    assert with_images.input_other == 1 + 2 * IMAGE_TOKEN_ESTIMATE
    assert with_images.output == 0


def test_estimate_fallback_usage_ignores_negative_images():
    assert estimate_fallback_usage(image_count=-3) == UsageBreakdown()
    assert estimate_fallback_usage(image_count="2").input_other == 2 * IMAGE_TOKEN_ESTIMATE


def test_usage_is_empty_accepts_none_and_non_empty():
    assert usage_is_empty(None) is True
    assert usage_is_empty(usage_breakdown(None)) is True
    assert usage_is_empty(UsageBreakdown()) is True
    assert usage_is_empty(UsageBreakdown(output=1)) is False
    assert usage_is_empty(UsageBreakdown(input_cached=1)) is False


# ---------------------------------------------------------------------------
# TokenQuota：record 新字段 / 旧条目兼容 / 实际 usage 窗口汇总
# ---------------------------------------------------------------------------


def test_record_writes_usage_fields_and_backward_compat(tmp_path):
    quota = TokenQuota(tmp_path)
    legacy_style = quota.record(100, session="s1", source="llm_request")
    # 旧调用不传 usage 字段时条目仍带键，写 0/false
    assert legacy_style["input_other"] == 0
    assert legacy_style["input_cached"] == 0
    assert legacy_style["output"] == 0
    assert legacy_style["actual"] is False

    actual_entry = quota.record(
        50,
        session="s1",
        source="agent_usage",
        input_other=10,
        input_cached=5,
        output=2,
        actual=True,
    )
    assert actual_entry["input_other"] == 10
    assert actual_entry["input_cached"] == 5
    assert actual_entry["output"] == 2
    assert actual_entry["actual"] is True

    entries = [
        json.loads(line)
        for line in quota.path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(entries) == 2
    assert entries[0]["actual"] is False
    assert entries[0]["input_other"] == 0
    assert entries[1]["actual"] is True
    assert entries[1]["input_cached"] == 5
    # 旧 tokens 语义保持不变
    assert quota.used(24) == 150


def test_record_accepts_breakdown_as_fields(tmp_path):
    quota = TokenQuota(tmp_path)
    breakdown = usage_breakdown(
        {"input_other": 3, "input_cached": 4, "output": 5}
    )
    entry = quota.record(breakdown.total, session="s", actual=True, **breakdown.as_fields())
    assert entry["tokens"] == 12
    assert entry["input_other"] == 3
    assert entry["input_cached"] == 4
    assert entry["output"] == 5


def test_quota_reads_legacy_jsonl_entries(tmp_path):
    quota = TokenQuota(tmp_path)
    legacy = {
        "ts": time.time(),
        "tokens": 42,
        "session": "s",
        "source": "old",
        "play": False,
    }
    quota.path.parent.mkdir(parents=True, exist_ok=True)
    quota.path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    assert quota.used(24) == 42
    assert quota.entries(24)[0]["tokens"] == 42
    assert quota.actual_usage(24) == {}
    assert quota.estimated_usage(24) == 42


def test_actual_usage_window_filter_and_summary(tmp_path):
    quota = TokenQuota(tmp_path)
    now = time.time()
    # 1 条实际 usage（窗口内）
    quota.record(
        0,
        session="s",
        source="agent_usage",
        ts=now,
        input_other=10,
        input_cached=20,
        output=5,
        actual=True,
    )
    # 1 条实际 usage（2 小时前，24h 窗口内、1h 窗口外）
    quota.record(
        7,
        session="s",
        source="agent_usage",
        ts=now - 7200,
        input_other=1,
        input_cached=2,
        output=3,
        actual=True,
    )
    # 运行中估算（非 actual）
    quota.record(5, session="s", source="llm_request", ts=now)
    # 窗口外的实际 usage
    quota.record(
        99,
        session="s",
        source="agent_usage",
        ts=now - 48 * 3600,
        input_other=50,
        actual=True,
    )

    summary = quota.actual_usage(24)
    assert summary == {
        "input_other": 11,
        "input_cached": 22,
        "output": 8,
        "total": 41,
        "calls": 2,
    }
    assert quota.actual_usage(1) == {
        "input_other": 10,
        "input_cached": 20,
        "output": 5,
        "total": 35,
        "calls": 1,
    }
    assert quota.estimated_usage(24) == 5
    assert quota.estimated_usage(1) == 5
    # tokens 语义不变：估算 + 实际差额一起计入已用
    assert quota.used(24) == 12


def test_actual_usage_returns_empty_dict_without_actual_entries(tmp_path):
    quota = TokenQuota(tmp_path)
    quota.record(10, session="s", source="llm_request")
    assert quota.actual_usage(24) == {}
    assert quota.estimated_usage(24) == 10
