"""手机 token 精确对账：从 AstrBot ``provider_stats`` 读取 Agent run 的累计 usage。

AstrBot 4.29 的 ``on_llm_response`` 每个 Agent run 只在结束时触发一次，只带最后
一次模型调用的 usage；工具循环中间调用、工具 schema 与图片等输入全部漏记。而
AstrBot 的 ``provider_stats`` 表按 Agent run 保存了该 run 内每次模型调用的累计
usage（``token_input_other/token_input_cached/token_output``），因此用它做实际
usage 对账源：

- :func:`fetch_agent_run_usage`：一次 Agent run 结束后按
  ``umo + run 起始墙钟`` 查询并汇总该 run 的实际 usage（数据库写入是异步的，
  需要短重试）。
- :func:`fetch_window_usage`：按会话 UMO 列表汇总最近时间窗口内的实际 usage，
  供 ``/phone quota audit`` 对照插件记账。

本模块只依赖标准库；``astrbot``/``sqlmodel`` 均在查询函数内部延迟导入，本地无
AstrBot 环境也能导入与单测。查询只读，不写数据库，失败不抛异常由调用方降级。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Agent run 起始墙钟查询下界容差：provider_stats.start_time 由 AgentRunner 在构造
# 时用 time.time() 写入，可能略早于插件 on_llm_request 记录的 started_wall；5s 容差
# 同时容忍时序抖动，超出的上一次 run 由 _filter_run_rows 按 end_time 排除。
START_TIME_TOLERANCE_SECONDS = 5.0

_MISSING = object()


@dataclass(frozen=True)
class AgentUsage:
    """一次 Agent run（或一组 run）在 AstrBot provider_stats 中的累计 usage。"""

    input_other: int = 0
    input_cached: int = 0
    output: int = 0
    calls: int = 0
    provider_models: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        """输入未缓存 + 输入缓存 + 输出。"""
        return int(self.input_other) + int(self.input_cached) + int(self.output)


def _to_non_negative_int(value: Any) -> int:
    """把数据库字段/字典值宽松转换为非负整数（异常、NaN、Inf 按 0）。"""
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


def _row_value(row: Any, name: str) -> Any:
    """读取统计行字段：dict 按 key、对象按属性；缺失或异常返回哨兵值。"""
    try:
        if isinstance(row, Mapping):
            return row.get(name, _MISSING)
        return getattr(row, name, _MISSING)
    except Exception:  # noqa: BLE001 - 鸭子类型统计行的字段异常按缺失处理
        return _MISSING


def _row_end_time(row: Any) -> float:
    """读取统计行 ``end_time``（墙钟秒）；缺失/None/非数/0 一律按 0（未知）。"""
    value = _row_value(row, "end_time")
    if value is _MISSING or value is None:
        return 0.0
    try:
        end_time = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(end_time) or end_time <= 0:
        return 0.0
    return end_time


def _filter_run_rows(rows: Iterable[Any], started_wall: float) -> list[Any]:
    """从查询结果中排除早于本次 run 的 provider_stats 统计行。

    ``provider_stats.start_time`` 是 AgentRunner 构造时刻，可能早于插件
    ``on_llm_request`` 记录的 ``started_wall``；查询下界带 5s 容差时，同一会话
    5 秒内上一次 run 的统计行也会被带回来。用 ``end_time`` 区分：早于本次 run
    起点就已结束的必是上一次 run。``end_time`` 缺失或为 0（error/abort 等未正常
    收尾的 run）无法判断，保留该行以免漏记实际 usage。
    """
    try:
        boundary = float(started_wall)
    except (TypeError, ValueError):
        boundary = 0.0
    selected: list[Any] = []
    for row in rows if rows is not None else ():
        if row is None:
            continue
        end_time = _row_end_time(row)
        if end_time > 0 and end_time < boundary:
            continue
        selected.append(row)
    return selected


def usage_from_rows(rows: Iterable[Any]) -> AgentUsage:
    """汇总 provider_stats 统计行。

    Args:
        rows: 统计行，支持对象（ProviderStat）或 dict；字段为
            ``token_input_other/token_input_cached/token_output/provider_model``。

    Returns:
        累计 usage；空行（None、无 token 字段的行）忽略，``calls`` 为有效行数，
        ``provider_models`` 去重后排序。
    """
    input_other = 0
    input_cached = 0
    output = 0
    calls = 0
    models: set[str] = set()
    for row in rows if rows is not None else ():
        if row is None:
            continue
        values: list[Any] = []
        present = False
        for name in ("token_input_other", "token_input_cached", "token_output"):
            value = _row_value(row, name)
            if value is not _MISSING:
                present = True
            values.append(value)
        if not present:
            continue
        calls += 1
        input_other += _to_non_negative_int(values[0])
        input_cached += _to_non_negative_int(values[1])
        output += _to_non_negative_int(values[2])
        model = _row_value(row, "provider_model")
        if model is not _MISSING and model is not None:
            text = str(model).strip()
            if text:
                models.add(text)
    return AgentUsage(
        input_other=input_other,
        input_cached=input_cached,
        output=output,
        calls=calls,
        provider_models=tuple(sorted(models)),
    )


async def fetch_agent_run_usage(
    umo: str,
    started_wall: float,
    *,
    retries: int = 6,
    delay: float = 0.5,
    query: Callable[[str, float], Awaitable[Iterable[Any]]] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> AgentUsage | None:
    """查询一次 Agent run 在 provider_stats 中的累计实际 usage。

    AstrBot 在 Agent run 结束后才异步写入统计行，因此需要短重试。每次查询结果
    ``calls > 0 且 total > 0`` 时立即返回；重试耗尽返回 ``None``（不抛异常），由
    调用方保留估算并记录 warning。单次查询抛异常视为本次尚未就绪，继续重试。

    Args:
        umo: 会话 unified_msg_origin。
        started_wall: run 起始墙钟（``time.time()`` 浮点秒）。
        retries: 最多尝试次数（至少 1 次）。
        delay: 两次尝试之间的等待秒数。
        query: 查询函数（测试注入），默认 :func:`_query_agent_run_rows`。
        sleep: 等待函数（测试注入），默认 ``asyncio.sleep``。
    """
    query_fn = query or _query_agent_run_rows
    sleep_fn = sleep or asyncio.sleep
    try:
        attempts = max(1, int(retries))
    except (TypeError, ValueError):
        attempts = 1
    try:
        delay_seconds = max(0.0, float(delay))
    except (TypeError, ValueError):
        delay_seconds = 0.0
    umo_text = str(umo or "")
    try:
        run_started = float(started_wall)
    except (TypeError, ValueError):
        run_started = 0.0
    for attempt in range(attempts):
        usage = AgentUsage()
        try:
            rows = await query_fn(umo_text, run_started)
            usage = usage_from_rows(rows or ())
        except Exception as exc:  # noqa: BLE001 - 查询失败按未就绪继续重试
            logger.debug("查询 provider_stats 失败（第 %d 次）：%s", attempt + 1, exc)
        if usage.calls > 0 and usage.total > 0:
            return usage
        if attempt < attempts - 1 and delay_seconds > 0:
            await sleep_fn(delay_seconds)
    return None


async def fetch_window_usage(
    umos: Iterable[str],
    window_hours: float,
    *,
    now: float | None = None,
    query: Callable[[Sequence[str], float], Awaitable[Iterable[Any]]] | None = None,
) -> AgentUsage | None:
    """汇总给定会话 UMO 列表最近 ``window_hours`` 内的实际 usage。

    Args:
        umos: 会话 unified_msg_origin 列表；空列表直接返回空汇总（不查库）。
        window_hours: 回看小时数。
        now: 窗口结束墙钟（默认 ``time.time()``，测试注入）。
        query: 查询函数（测试注入），默认 :func:`_query_window_rows`。

    Returns:
        累计 usage；查询失败返回 ``None``（不抛异常）。
    """
    unique_umos: list[str] = []
    seen: set[str] = set()
    for umo in umos if umos is not None else ():
        text = str(umo or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique_umos.append(text)
    if not unique_umos:
        return AgentUsage()
    try:
        hours = max(0.0, float(window_hours))
    except (TypeError, ValueError):
        hours = 0.0
    reference = time.time() if now is None else float(now)
    started_wall = reference - hours * 3600.0
    query_fn = query or _query_window_rows
    try:
        rows = await query_fn(unique_umos, started_wall)
    except Exception as exc:  # noqa: BLE001 - 查询失败由调用方降级展示
        logger.debug("查询 provider_stats 窗口用量失败：%s", exc)
        return None
    return usage_from_rows(rows or ())


async def _query_agent_run_rows(umo: str, started_wall: float) -> list[Any]:
    """只读查询一次 Agent run 的 provider_stats 统计行（延迟导入 AstrBot）。"""
    from astrbot.core import db_helper  # noqa: PLC0415 - 延迟导入便于本地单测
    from astrbot.core.db.po import ProviderStat  # noqa: PLC0415
    from sqlmodel import select  # noqa: PLC0415

    started = float(started_wall)
    async with db_helper.get_db() as session:
        result = await session.execute(
            select(ProviderStat).where(
                ProviderStat.umo == str(umo),
                ProviderStat.start_time
                >= started - START_TIME_TOLERANCE_SECONDS,
            )
        )
        rows = list(result.scalars().all())
    # start_time 是 AgentRunner 构造时刻，可能早于插件 on_llm_request 记录的
    # started_wall，5s 容差会带上同一会话上一次 run 的统计行；按 end_time 在
    # Python 侧过滤（end_time 为 0/缺失的 error/abort 行保留）。
    return _filter_run_rows(rows, started)


async def _query_window_rows(
    umos: Sequence[str], started_wall: float
) -> list[Any]:
    """只读查询多个会话在时间窗口内的 provider_stats 统计行。"""
    umo_list = [str(umo) for umo in umos]
    if not umo_list:
        return []
    from astrbot.core import db_helper  # noqa: PLC0415 - 延迟导入便于本地单测
    from astrbot.core.db.po import ProviderStat  # noqa: PLC0415
    from sqlmodel import select  # noqa: PLC0415

    async with db_helper.get_db() as session:
        result = await session.execute(
            select(ProviderStat).where(
                ProviderStat.umo.in_(umo_list),
                ProviderStat.start_time >= float(started_wall),
            )
        )
        return list(result.scalars().all())
