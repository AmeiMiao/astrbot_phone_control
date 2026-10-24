"""手机 token 额度：滚动窗口用量记录、估算与「玩手机」预算状态机。

AstrBot 4.29 的工具循环拿不到每次模型调用的 usage 回调，运行中按可解释的
估算记账：CJK 约 1.5 token/字符、ASCII 约 1/4 token/字符；每次手机工具调用按
``quota.per_step_tokens`` 估算下一轮 LLM 开销。Agent run 结束后用 AstrBot
``provider_stats`` 的累计实际 usage 对账补记差额；插件内部 ``llm_generate`` 调用
按返回的 ``LLMResponse.usage`` 精确记账。条目中的 ``input_other/input_cached/
output`` 保存 usage 明细，``actual`` 标记该条是否为实际 usage。

记录落盘：``plugin_data/astrbot_phone_control/token_usage.jsonl``，滚动窗口按时间
过滤，文件最多保留 5000 行。本模块只依赖标准库，便于本地单测。
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOKEN_FILENAME = "token_usage.jsonl"
DEFAULT_MAX_ENTRIES = 5000
# usage 缺失时每张图片折算的输入 token 估算值（视觉模型常见计费量级）。
IMAGE_TOKEN_ESTIMATE = 1500
_CJK_START = 0x2E80
_CJK_RANGES = (
    (0x2E80, 0x9FFF),  # CJK 部首/统一表意文字
    (0x3000, 0x303F),  # CJK 标点
    (0x3040, 0x30FF),  # 日文假名
    (0xAC00, 0xD7AF),  # 韩文
    (0xF900, 0xFAFF),  # CJK 兼容表意
    (0xFF00, 0xFFEF),  # 全角字符
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """估算文本 token 数：CJK 约 1.5/字符，其余（ASCII 等）约 1/4 字符。

    Args:
        text: 待估算文本（可为空）。

    Returns:
        向上取整的 token 估算值。
    """
    content = str(text or "")
    if not content:
        return 0
    cjk = sum(1 for char in content if _is_cjk(char))
    other = len(content) - cjk
    return int(math.ceil(cjk * 1.5 + other / 4.0))


def estimate_messages_tokens(messages: list[Any]) -> int:
    """估算一段消息/上下文字符串列表的 token 总和。"""
    total = 0
    for message in messages or []:
        total += estimate_tokens(str(message))
    return total


def _to_non_negative_int(value: Any) -> int:
    """把 usage 字段/JSONL 值宽松转换为非负整数（异常、NaN、Inf 按 0）。"""
    if isinstance(value, bool):
        return int(value)
    if value is None:
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number):
        return 0
    return max(0, int(round(number)))


def _usage_field(usage: Any, name: str) -> Any:
    """读取 usage 字段：dict 按 key、对象按属性；缺失或异常返回 None。"""
    try:
        if isinstance(usage, Mapping):
            return usage.get(name)
        return getattr(usage, name, None)
    except Exception:  # noqa: BLE001 - 鸭子类型 usage 的字段异常按缺失处理
        return None


def _usage_int(usage: Any, name: str) -> int:
    return _to_non_negative_int(_usage_field(usage, name))


@dataclass(frozen=True)
class UsageBreakdown:
    """一次模型调用的 usage 明细：输入未缓存/输入缓存/输出 token 数。"""

    input_other: int = 0
    input_cached: int = 0
    output: int = 0

    @property
    def total(self) -> int:
        """三项之和。"""
        return int(self.input_other) + int(self.input_cached) + int(self.output)

    def as_fields(self) -> dict[str, int]:
        """转成 ``TokenQuota.record`` 需要的 usage 明细字段。"""
        return {
            "input_other": int(self.input_other),
            "input_cached": int(self.input_cached),
            "output": int(self.output),
        }


def usage_breakdown(usage: Any) -> UsageBreakdown:
    """解析 AstrBot ``TokenUsage`` 对象或 dict 形态的 usage。

    支持 ``input_other/input_cached/output`` 与 ``prompt_tokens/completion_tokens``；
    只有 ``total_tokens`` 时按 ``input_other = total_tokens - output`` 兜底。
    缺失、None、字段类型异常一律按 0 处理，保证记账路径不抛异常。
    """
    if usage is None:
        return UsageBreakdown()
    input_other = _usage_int(usage, "input_other")
    input_cached = _usage_int(usage, "input_cached")
    output = _usage_int(usage, "output")
    if output <= 0:
        output = _usage_int(usage, "completion_tokens")
    if input_other <= 0 and input_cached <= 0:
        input_other = _usage_int(usage, "prompt_tokens")
    if input_other <= 0 and input_cached <= 0:
        total = _usage_int(usage, "total_tokens")
        if total > 0:
            input_other = max(0, total - output)
    return UsageBreakdown(
        input_other=input_other,
        input_cached=input_cached,
        output=output,
    )


def estimate_fallback_usage(
    *,
    prompt_text: str = "",
    completion_text: str = "",
    image_count: int = 0,
) -> UsageBreakdown:
    """usage 缺失时估算一次模型调用。

    文本用 :func:`estimate_tokens` 估算，每张图片按
    :data:`IMAGE_TOKEN_ESTIMATE` 计入 ``input_other``。
    """
    images = _to_non_negative_int(image_count)
    return UsageBreakdown(
        input_other=estimate_tokens(str(prompt_text or ""))
        + images * IMAGE_TOKEN_ESTIMATE,
        output=estimate_tokens(str(completion_text or "")),
    )


def usage_is_empty(breakdown: UsageBreakdown | None) -> bool:
    """usage 明细是否为空（None 或三项全 0）。"""
    return breakdown is None or breakdown.total <= 0


@dataclass
class PlayBudget:
    """一次「玩手机」任务的 token 预算状态。"""

    session: str
    target_tokens: int
    spent_tokens: float = 0.0
    break_rule: bool = False

    @property
    def limit_tokens(self) -> int:
        """本次任务的执行上限；规则弹性提供向上取整的 20% 余量。"""
        target = max(0, int(self.target_tokens))
        return target + (target + 4) // 5 if self.break_rule else target

    @property
    def remaining_tokens(self) -> float:
        return max(0.0, float(self.limit_tokens) - self.spent_tokens)

    @property
    def completed(self) -> bool:
        return self.spent_tokens >= float(self.limit_tokens)


class PlayBudgetRegistry:
    """内存中的玩手机预算表（按会话 UMO 记录，插件重载即重置）。"""

    def __init__(self, *, max_sessions: int = 100) -> None:
        self.max_sessions = max(10, int(max_sessions))
        self._budgets: dict[str, PlayBudget] = {}

    def begin(self, session: str, target_tokens: int) -> PlayBudget:
        """开始（或重置）一个会话的预算。"""
        key = str(session or "").strip()
        budget = PlayBudget(
            session=key,
            target_tokens=max(0, int(target_tokens)),
        )
        self._budgets[key] = budget
        while len(self._budgets) > self.max_sessions:
            oldest = next(iter(self._budgets))
            if oldest == key:
                break
            self._budgets.pop(oldest, None)
        return budget

    def get(self, session: str) -> PlayBudget | None:
        return self._budgets.get(str(session or "").strip())

    def add(self, session: str, tokens: float) -> PlayBudget | None:
        budget = self.get(session)
        if budget is None:
            return None
        if tokens > 0:
            budget.spent_tokens += float(tokens)
        return budget

    def finish(self, session: str) -> PlayBudget | None:
        return self._budgets.pop(str(session or "").strip(), None)

    def clear(self) -> None:
        self._budgets.clear()

    def __len__(self) -> int:
        return len(self._budgets)


@dataclass
class TokenQuota:
    """滚动窗口 token 用量记录器。"""

    root: str | Path
    max_entries: int = DEFAULT_MAX_ENTRIES
    path: Path = field(init=False)

    def __post_init__(self) -> None:
        root_path = Path(self.root)
        self.path = root_path / TOKEN_FILENAME
        self.max_entries = max(100, int(self.max_entries))

    # ---- 读写 ---------------------------------------------------------

    def _read_entries_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        entries.append(record)
        except OSError:
            return []
        return entries

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                for record in entries:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---- 对外接口 -----------------------------------------------------

    def record(
        self,
        tokens: float,
        *,
        session: str = "",
        source: str = "",
        play: bool = False,
        ts: float | None = None,
        input_other: int = 0,
        input_cached: int = 0,
        output: int = 0,
        actual: bool = False,
    ) -> dict[str, Any]:
        """追加一条用量记录，返回写入的条目。

        Args:
            tokens: 本次记账的 token 数量（估算或实际差额）。
            session: 会话 UMO。
            source: 记账来源，如 ``llm_request``/``agent_usage``/``internal:vision``。
            play: 是否属于「玩手机」任务预算。
            ts: 记录时间（秒），默认当前时间。
            input_other: usage 明细中的输入未缓存 tokens。
            input_cached: usage 明细中的输入缓存 tokens。
            output: usage 明细中的输出 tokens。
            actual: 该条是否为实际 usage（provider_stats 对账或内部调用真实 usage）。
        """
        amount = float(tokens or 0)
        entry = {
            "ts": float(ts if ts is not None else time.time()),
            "tokens": round(max(0.0, amount), 2),
            "session": str(session or ""),
            "source": str(source or ""),
            "play": bool(play),
            "input_other": _to_non_negative_int(input_other),
            "input_cached": _to_non_negative_int(input_cached),
            "output": _to_non_negative_int(output),
            "actual": bool(actual),
        }
        entries = self._read_entries_unlocked()
        entries.append(entry)
        if len(entries) > self.max_entries:
            entries = entries[-self.max_entries :]
        self._write_entries(entries)
        return entry

    def entries(self, window_hours: float) -> list[dict[str, Any]]:
        """返回滚动窗口内的记录（按时间正序）。"""
        try:
            hours = max(0.0, float(window_hours))
        except (TypeError, ValueError):
            hours = 0.0
        cutoff = time.time() - hours * 3600.0
        return [
            entry
            for entry in self._read_entries_unlocked()
            if float(entry.get("ts") or 0) >= cutoff
        ]

    def minute_series(self, minutes: int = 30, *, now: float | None = None) -> list[dict[str, float]]:
        """按自然分钟汇总额度账本，包含估算与实际差额，空分钟补零。"""
        current = time.time() if now is None else float(now)
        count = max(1, min(int(minutes), 1440))
        end = int(current // 60) * 60
        start = end - (count - 1) * 60
        buckets = {slot: 0.0 for slot in range(start, end + 1, 60)}
        for entry in self._read_entries_unlocked():
            ts = float(entry.get("ts") or 0)
            slot = int(ts // 60) * 60
            if slot in buckets and ts <= current:
                buckets[slot] += float(entry.get("tokens") or 0)
        return [{"ts": slot, "tokens": round(tokens, 2)} for slot, tokens in buckets.items()]

    def used(self, window_hours: float) -> float:
        """滚动窗口内已用 token 数。"""
        return float(
            sum(float(entry.get("tokens") or 0) for entry in self.entries(window_hours))
        )

    def actual_usage(self, window_hours: float) -> dict[str, float | int]:
        """窗口内 ``actual=true`` 条目的 usage 明细汇总。

        Returns:
            含 ``input_other/input_cached/output/total/calls`` 的字典；窗口内没有
            实际 usage 条目时返回空字典。
        """
        input_other = 0
        input_cached = 0
        output = 0
        calls = 0
        for entry in self.entries(window_hours):
            if not bool(entry.get("actual")):
                continue
            calls += 1
            input_other += _to_non_negative_int(entry.get("input_other"))
            input_cached += _to_non_negative_int(entry.get("input_cached"))
            output += _to_non_negative_int(entry.get("output"))
        if calls <= 0:
            return {}
        return {
            "input_other": input_other,
            "input_cached": input_cached,
            "output": output,
            "total": input_other + input_cached + output,
            "calls": calls,
        }

    def estimated_usage(self, window_hours: float) -> float:
        """窗口内尚未经实际 usage 对账的估算 token 合计（用于状态展示）。"""
        return float(
            sum(
                float(entry.get("tokens") or 0)
                for entry in self.entries(window_hours)
                if not bool(entry.get("actual"))
            )
        )

    def remaining(self, *, window_hours: float, max_tokens: float) -> float:
        """窗口内剩余额度（不小于 0）。"""
        return max(0.0, float(max_tokens) - self.used(window_hours))

    def status_text(self, *, window_hours: float, max_tokens: float) -> str:
        """生成额度状态文本（/phone quota 与软告警共用）。"""
        used = self.used(window_hours)
        limit = max(1.0, float(max_tokens))
        percent = used / limit * 100.0
        remaining = max(0.0, limit - used)
        return (
            f"手机 token 额度：窗口 {float(window_hours):g} 小时，"
            f"已用 {used:.0f} / 上限 {float(max_tokens):.0f}（{percent:.1f}%，"
            f"剩余 {remaining:.0f}）。窗口按滚动时间统计，旧用量会自然滑出。"
        )

    def reset(self) -> int:
        """清空所有用量记录，返回清除条数。"""
        entries = self._read_entries_unlocked()
        self._write_entries([])
        return len(entries)
