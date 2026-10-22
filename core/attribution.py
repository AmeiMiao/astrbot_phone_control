"""学习内容的归属判定：自行唤醒学习 / 用户指定使用手机。

用户要求：自行唤醒（AI 自主持续刷、随机玩手机、定时自主浏览）产生的学习记录
不计入用户记忆；用户指定使用手机（普通会话里的手机工具、``/phone browse``、
用户发起的后台任务/玩手机任务）产生的学习记录计入用户信息。

本模块只做纯逻辑判断，便于单测；不依赖 astrbot。
"""

from __future__ import annotations

from .identity import PLUGIN_NAME

# 归属类型
KIND_AUTONOMOUS = "autonomous"
KIND_USER = "user"
KIND_UNKNOWN = "unknown"

# 任务 payload 的 origin 取值（见 core/scheduler.py）。
PLAY_ORIGIN = "phone_play"
RANDOM_PLAY_ORIGIN = "phone_random_play"
CONTINUOUS_ORIGIN = "phone_continuous"
BACKGROUND_ORIGIN = "phone_background_task"
AUTO_BROWSE_ORIGIN = PLUGIN_NAME

# 自行唤醒：随机玩手机、持续刷手机、定时自主浏览。
AUTONOMOUS_ORIGINS = frozenset(
    {RANDOM_PLAY_ORIGIN, CONTINUOUS_ORIGIN, AUTO_BROWSE_ORIGIN}
)
# 用户指定使用手机：普通会话（空 origin）、/phone play、用户发起的后台任务。
USER_ORIGINS = frozenset({"", PLAY_ORIGIN, BACKGROUND_ORIGIN})

KIND_LABELS = {
    KIND_AUTONOMOUS: "自行唤醒",
    KIND_USER: "用户指定",
    KIND_UNKNOWN: "未标注",
}


def origin_kind(origin: str) -> str:
    """把任务 origin 归类为自行唤醒 / 用户指定 / 未知。"""
    text = str(origin or "").strip()
    if text in AUTONOMOUS_ORIGINS:
        return KIND_AUTONOMOUS
    if text in USER_ORIGINS:
        return KIND_USER
    return KIND_UNKNOWN


def counts_user_memory(kind: str) -> bool:
    """该归属的学习内容是否计入用户记忆（MemoryCompanion/LivingMemory 用户侧）。"""
    return str(kind or "") == KIND_USER


def is_autonomous(kind: str) -> bool:
    """是否自行唤醒学习。"""
    return str(kind or "") == KIND_AUTONOMOUS


def kind_label(kind: str) -> str:
    """归属类型的中文短标签。"""
    return KIND_LABELS.get(str(kind or ""), KIND_LABELS[KIND_UNKNOWN])
