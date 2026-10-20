"""插件内共享的小工具：消息入口关键词、时间格式化与 LLM 回复文本提取。

供插件入口与 ``core/`` 内模块共用；本模块只依赖标准库，不依赖 astrbot。
"""

from __future__ import annotations

import time
from typing import Any


# 进入 App 后优先查看的消息入口关键词（按优先级从高到低加权）。
_INBOX_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("回复我的", 100),
    ("回复我", 96),
    ("互动消息", 92),
    ("评论我的", 90),
    ("@我的", 88),
    ("私信", 82),
    ("消息", 72),
    ("通知", 60),
    ("未读", 50),
)
_INBOX_EXCLUDE_WORDS = ("免打扰", "通知栏", "设置", "消息设置", "勿扰")


def _format_time(ts: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "未知时间"


def _llm_response_text(response: Any) -> str:
    """取 llm_generate 返回对象的文本（completion_text 优先，其次 result_chain）。"""
    text = str(getattr(response, "completion_text", "") or "").strip()
    if text:
        return text
    result_chain = getattr(response, "result_chain", None)
    if result_chain is None:
        return ""
    try:
        return str(result_chain.get_plain_text() or "").strip()
    except Exception:  # noqa: BLE001 - 结果链文本读取失败按空处理
        return ""
