"""随机玩手机：纯标准库的随机间隔/概率/提示词辅助函数。

由 ``core/plugin/tasks.py`` 的后台循环调用：按 ``auto.random_interval_min/max`` 随机 sleep，
到点按 ``auto.random_probability`` 掷骰，命中后通过 cron 主动任务唤醒主 Agent
自由玩手机（载荷 session 为空 = 静默，不投递到聊天）。

本模块不导入 astrbot，便于本地单测。
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from .play_flow import FLOW_RULE, flow_rule_text, break_rule_hint, share_rule_text

MIN_INTERVAL_MINUTES = 1.0
# 随机间隔不设上限（用户可配置任意低频率）
DEFAULT_PROBABILITY = 0.5


def normalize_intervals(
    min_minutes: float | int,
    max_minutes: float | int,
) -> tuple[float, float]:
    """归一化随机间隔（分钟）：只保留下限 1，不设上限，且保证 min <= max。"""
    try:
        low = float(min_minutes)
    except (TypeError, ValueError):
        low = MIN_INTERVAL_MINUTES
    try:
        high = float(max_minutes)
    except (TypeError, ValueError):
        high = low
    low = max(MIN_INTERVAL_MINUTES, low)
    high = max(MIN_INTERVAL_MINUTES, high)
    if high < low:
        high = low
    return low, high


def next_random_delay(
    min_minutes: float | int,
    max_minutes: float | int,
    *,
    rng: random.Random | None = None,
) -> float:
    """返回下一次随机玩手机的等待秒数（``uniform(min,max)`` 分钟折合秒）。"""
    low, high = normalize_intervals(min_minutes, max_minutes)
    generator = rng or random
    return float(generator.uniform(low, high)) * 60.0


def should_play(
    probability: float | int,
    *,
    rng: random.Random | None = None,
) -> bool:
    """按概率掷骰；非法/越界概率会收敛到 [0,1]。"""
    try:
        value = float(probability)
    except (TypeError, ValueError):
        value = DEFAULT_PROBABILITY
    if value < 0.0:
        value = 0.0
    elif value > 1.0:
        value = 1.0
    generator = rng or random
    return float(generator.random()) < value


_MEMORY_SOURCES = (
    "你的情绪与喜好来自：你自己的人格设定、上次浏览/玩手机的回忆（phone_recall_diary）、"
    "以及环境里可用的长期记忆工具（若有 recall_long_term_memory 可先回忆，"
    "结束时可 memorize_long_term_memory）。最近聊天（phone_recent_chat）用于结束后"
    "判断分享/互动对象，不是你开始刷手机的动机。"
)

_INTERACTION_HINT = (
    "互动是内容看完/玩完后的独立判断：想评论/发帖/点赞时再申请名额——若主人已开启对应"
    "开关，可先用 phone_interaction_gate 申请 1 次，再自己用低层工具完成，结束后调用"
    " phone_interaction_record 记录；每轮最多 1 条，不刷屏。"
)

_CONCISE_REPLY = (
    "汇报默认 1–2 句、≤80 字；任务结果只给 1–3 个要点，不复述过程/工具输出"
    "（用户要求详情除外）。"
)

_SILENT_AND_SHARE = (
    "默认保持静默：普通操作在后台连续进行，不要逐条汇报。"
    + FLOW_RULE
    + share_rule_text()
    + "每篇内容独立任务：图文点开前 phone_content begin；视频打开并确认起点后begin，"
    "read先理解正文或试看。感兴趣再decide continue并read续看。看评论前独立decide "
    "read_comments；想回复具体已读评论再decide reply_comment并申请互动名额，"
    "看评论不等于回复。点赞收藏可选且遵守权限，结束finish后再换内容。"
    "帖子默认回顶截图，视频传 content_type=video、当前画面传 screen。"
)


def build_play_note(
    target_tokens: int,
    *,
    interactions_enabled: bool = False,
    share_hint: str = "",
    novelty_hint: str = "",
    break_rule: bool = False,
) -> str:
    """生成 /phone play 的提示词（带本轮 token 预算，静默运行）。"""
    budget = max(0, int(target_tokens))
    interaction_hint = _INTERACTION_HINT if interactions_enabled else ""
    novelty = str(novelty_hint or "").strip()
    return (
        "现在是你的自由活动时间：用手机自己决定玩什么、看什么，按人格与当下心情来。"
        "先想你自己此刻想玩什么，再 phone_list_apps 或回桌面看图标决定玩哪个 App；"
        "不熟悉的界面用 phone_screen 边看边试（实时页面可用 mode=image）。"
        "结束后若要判断分享或互动，再 phone_recent_chat 看最近聊天与对方喜好。"
        + _MEMORY_SOURCES
        + _SILENT_AND_SHARE.replace(FLOW_RULE, flow_rule_text(break_rule)).replace(
            "感兴趣再decide continue", "按真实意愿decide continue" if break_rule else "感兴趣再decide continue"
        )
        + (str(share_hint).strip() if str(share_hint or "").strip() else "")
        + (f"\n{novelty}" if novelty else "")
        + interaction_hint
        + f"本轮预算 {budget} tokens，请持续玩到预算完成再收尾（不要提前结束）。"
        "收尾由插件自动回桌面并清理后台，无需手动调用 phone_clean_background；"
        "把值得记住的体验写入长期记忆（若有工具）即可。"
        + _CONCISE_REPLY
        + break_rule_hint(break_rule)
    )


def build_random_play_note(
    *,
    share_only_interesting: bool = True,
    preferred_apps: str | Sequence[str] | None = None,
    target_tokens: int = 0,
    mood_available: bool | None = None,
    interactions_enabled: bool = False,
    share_hint: str = "",
    novelty_hint: str = "",
    break_rule: bool = False,
) -> str:
    """生成唤醒主 Agent 的「随机玩手机」提示词（人格主导、默认静默）。

    Args:
        share_only_interesting: 是否只在有趣/完成任务时才分享。
        preferred_apps: 可选的偏好 App 提示（不强制，只是期望）。
        target_tokens: 本轮玩手机预算（tokens，0 表示不限制）。
        mood_available: 「我会永远陪着你」陪伴插件是否可用（None=未知，按降级处理）。
        interactions_enabled: 主人是否开启了评论/发帖（决定是否提示可申请互动名额）。
        share_hint: 分享策略说明（白天免打扰/目标由 AI 判断），非空时追加。
        novelty_hint: 内容新鲜感状态（已看腻主题），非空时追加。

    Returns:
        自由活动口吻的中文提示词。
    """
    if mood_available:
        mood_hint = (
            "你的心情来自「我会永远陪着你」陪伴插件注入的角色状态，按当前心情表达即可，"
            "不要把内部数值说出来。"
        )
    else:
        mood_hint = (
            "没有陪伴插件时按你的人格设定与最近记忆降级推断心情，不要编造具体心情数值。"
        )
    note = (
        "现在是你的随机自由活动时间：用手机自己决定玩什么、看什么，按你自己的人格与"
        "当下心情来就好。" + mood_hint + "先想你自己此刻对什么感兴趣，"
        "再 phone_list_apps 看看装了哪些 App，或 phone_key home 回桌面再用 phone_screen "
        "看图标；也可以打开任意 App 自由探索（不限于预置画像），遇到不熟悉的界面就用 "
        "phone_screen 边看边试（实时页面可用 mode=image）。"
        "结束后若要判断分享或互动，再 phone_recent_chat 看最近聊天与对方喜好。"
        "给自己一个预算：大约 5–10 次操作或 3–5 分钟就收尾；同一页面连续两次没有新内容"
        "就换 App 或结束，避免一直在同一页打转。"
        + _MEMORY_SOURCES
        + (_INTERACTION_HINT if interactions_enabled else "")
    )
    if share_only_interesting:
        note += _SILENT_AND_SHARE + "没有值得分享的就保持安静。"
    else:
        note += _SILENT_AND_SHARE + "不需要刻意汇报过程。"
    note += (
        "收尾由插件自动回桌面并清理后台，无需手动调用 phone_clean_background；"
        "若环境有长期记忆工具，把值得记住的体验写入长期记忆即可。"
    )
    apps = preferred_apps
    if isinstance(apps, str):
        apps = [part.strip() for part in apps.replace("，", ",").split(",") if part.strip()]
    if apps:
        note += f" 可以优先考虑这些 App（但不强制）：{'、'.join(str(app) for app in apps)}。"
    if int(target_tokens) > 0:
        note += (
            f"本轮预算 {int(target_tokens)} tokens，请持续玩到预算完成再收尾"
            "（不要提前结束）。"
        )
    if str(share_hint or "").strip():
        note += str(share_hint)
    novelty = str(novelty_hint or "").strip()
    if novelty:
        note += f"\n{novelty}"
    note += _CONCISE_REPLY
    if break_rule:
        note = note.replace(FLOW_RULE, flow_rule_text(True)).replace(
            "感兴趣再decide continue", "按真实意愿decide continue"
        )
        note += break_rule_hint(True)
    return note
