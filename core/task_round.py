"""当前手机任务轮次的上下文标记。

浏览日志清理保护按「轮次任务」保留：同一轮 Agent 任务（或同一条 ``/phone browse``
命令）内产生的浏览日志带同一个轮次编号。插件入口在 Agent 开始/结束、命令执行期间
设置与重置本上下文；``PhoneService.browse`` 把轮次编号写入日记条目，清理时据此
保护最近 N 轮任务的日志。

使用 ``contextvars`` 而不是全局变量：同一进程内并发的用户命令/后台任务各自持有
自己的轮次，不会串号。本模块只依赖标准库，便于单测。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

_ROUND: ContextVar[tuple[str, str] | None] = ContextVar(
    "phone_task_round", default=None
)

# 轮次来源：用户会话工具调用 / 命令 / 自主任务（持续刷、随机、定时、后台任务）。
ORIGIN_USER = "user"
ORIGIN_COMMAND = "command"
ORIGIN_AUTONOMOUS = "autonomous"


def new_round_id() -> str:
    """生成新的轮次编号。"""
    return uuid.uuid4().hex


def set_round(round_id: str, origin: str = ORIGIN_USER) -> Token:
    """设置当前任务轮次，返回用于恢复的 token。"""
    return _ROUND.set((str(round_id or ""), str(origin or "")))


def reset_round(token: Token | None) -> None:
    """恢复进入轮次前的上下文；token 为空或重复恢复时忽略。"""
    if token is None:
        return
    try:
        _ROUND.reset(token)
    except (ValueError, RuntimeError):
        # 跨 asyncio 任务恢复 token 会失败：清空当前轮次即可。
        _ROUND.set(None)


def current_round() -> tuple[str, str] | None:
    """返回当前 ``(轮次编号, 来源)``；不在任务上下文中时返回 None。"""
    value = _ROUND.get()
    if not value or not value[0]:
        return None
    return value


def current_round_id() -> str:
    """当前轮次编号（无上下文时为空串）。"""
    value = current_round()
    return value[0] if value else ""
