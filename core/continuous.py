"""持续刷手机：轮次间隔、偏好 App 与每轮静默任务提示词的纯逻辑。

由 ``core/plugin/tasks.py`` 的持续刷循环调用：一轮主动任务结束后按间隔等待，再生成下一轮
提示词唤醒主 Agent。提示词要求每轮读懂内容并用 ``phone_learn`` 记录学习成果，
可把最近的学习档案（``recent_learning``）注入以避免刷完就忘。本模块不导入
astrbot，便于本地单测。
"""

from __future__ import annotations

import time

from .play_flow import LIVE_BROWSE_FLOW, render_flexibility, BREAK_RULE_FLOW, DIRECT_BROWSE_FLOW, break_rule_hint
from collections.abc import Iterable

DEFAULT_GAP_SECONDS = 60
# 轮次间隔只保留下限；不设上限（用户可配置任意频率）
MIN_GAP_SECONDS = 1
MAX_CONTINUOUS_APPS = 10

_CORE = (
    "这是连续自由活动的一轮：自选 App 浏览信息流、看视频或探索感兴趣的内容。"
    "按自己的兴趣看，不感兴趣直接退出；分享/互动结束后再判断。"
    "操作 App 前先调用 phone_app_memory 查一下使用经验，按记住的布局/入口操作；"
    "界面与经验不符说明 App 已改版，先自己重新探索，再用 phone_app_remember 更新记忆。"
    "每轮都不要无脑滑动：打开内容后读标题、正文和评论，理解它讲的是什么，"
    "再决定继续看还是换一个。"
    "先调用 phone_check_inbox 看看有没有新消息；遇到回复你的人，按被动回复规则判断是否要回"
    "（需要回复时先用 phone_interaction_gate 申请，kind=passive_reply，通过后再自己完成）。"
    "遇到滑块、顺序点击或识图验证，改用 phone_bypass_verify 完成验证。"
)

_LEARN_RULE = (
    "本轮结束前必须至少调用一次 phone_learn，记录 1–2 条你真正理解到的内容"
    "（观点、知识点、梗、事件、社区规则等，用自己的话归纳，不要照抄）；"
    "记不清就继续看，不要空刷；若环境有长期记忆工具也写一条。"
)

_LEARN_RULE_FALLBACK = (
    "本轮结束前把真正理解到的 1–2 条内容整理好（观点、知识点、梗、事件、社区规则等，"
    "用自己的话归纳）；记不清就继续看，不要空刷；若环境有长期记忆工具就写一条。"
)

_MEMORY_HEADER = "上几轮的学习档案（供参考，别忘也别重复；不要向用户复述）："

_SILENT_RULE = (
    "全程保持静默：不要向用户逐步汇报，也不要在工具调用之间输出中间解说；"
    "确实需要确认或无法继续时再如实说明。"
)

_TIME_RULE = "把这一轮控制在 10–30 分钟内收尾；遇到无法继续的情况如实记录即可。"


def normalize_gap_seconds(value: float | int | str | None) -> int:
    """归一化轮次间隔（秒）：非法回退默认 60，仅保留下限（不设上限）。"""
    try:
        seconds = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_GAP_SECONDS
    return max(MIN_GAP_SECONDS, seconds)


def normalize_continuous_apps(raw: str | Iterable[str] | None) -> list[str]:
    """清洗偏好 App：字符串按中英文逗号拆分，去空去重，最多保留 10 个。"""
    if raw is None:
        return []
    items: Iterable[object]
    if isinstance(raw, str):
        items = raw.replace("，", ",").split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = raw
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = " ".join(str(item or "").split())
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
        if len(result) >= MAX_CONTINUOUS_APPS:
            break
    return result


def next_run_delay(
    gap_seconds: float | int | str | None,
    last_finished: float | int | None,
    now: float | int | None = None,
) -> float:
    """返回距下一轮持续刷手机还需等待的秒数。

    Args:
        gap_seconds: 轮次间隔（秒），先经 :func:`normalize_gap_seconds` 收敛。
        last_finished: 上一轮结束时间戳；``None`` 表示还没刷过，立即开始。
        now: 当前时间戳，缺省取 ``time.time()``。

    Returns:
        剩余等待秒数；已到点或参数非法时返回 0。
    """
    gap = normalize_gap_seconds(gap_seconds)
    if last_finished is None:
        return 0.0
    try:
        finished = float(last_finished)
    except (TypeError, ValueError):
        return 0.0
    try:
        current = float(now) if now is not None else time.time()
    except (TypeError, ValueError):
        current = time.time()
    return max(0.0, float(gap) - (current - finished))


def build_continuous_note(
    *,
    target_tokens: int = 0,
    apps: str | Iterable[str] | None = None,
    learn_tool_available: bool = True,
    recent_learning: str = "",
    share_hint: str = "",
    novelty_hint: str = "",
    break_rule: bool = False,
    flexibility=None,
) -> str:
    """生成一轮「持续刷手机」的中文静默任务提示词。

    Args:
        target_tokens: 本轮 token 预算；<=0 表示不写预算提示。
        apps: 偏好 App（字符串或列表），非空时写入提示但不强制。
        learn_tool_available: 本轮是否可用 ``phone_learn`` 记录学习内容。
        recent_learning: 最近学习档案渲染文本（``LearningStore.render_for_prompt``
            的结果）；非空时注入提示词，帮助跨轮记住已学内容。
        share_hint: 分享策略说明（白天免打扰/目标由 AI 判断），非空时追加。
        novelty_hint: 内容新鲜感/厌倦感状态（已看腻主题与阈值），非空时追加，
            用于降低同类内容的打开/停留概率。

    Returns:
        直接作为主动任务 note 使用的中文提示词（简洁、面向本轮，不复述历史过程）。
    """
    try:
        budget = max(0, int(target_tokens))
    except (TypeError, ValueError):
        budget = 0
    note = (
        (("这是连续自由活动的一轮。" + BREAK_RULE_FLOW + DIRECT_BROWSE_FLOW) if break_rule else _CORE)
        + ("有值得记住的内容时按需记录真实体验。" if break_rule else (_LEARN_RULE if learn_tool_available else _LEARN_RULE_FALLBACK))
        + _SILENT_RULE
        + _TIME_RULE
        + (str(share_hint).strip() if str(share_hint or "").strip() else "")
    )
    preferred = normalize_continuous_apps(apps)
    if preferred:
        note += f"可以优先浏览这些 App（不强制）：{'、'.join(preferred)}。"
    if budget > 0:
        note += f"本轮预算 {budget} tokens，按需分配，接近预算时主动收尾。"
    memory = str(recent_learning or "").strip()
    if memory:
        note += f"\n{_MEMORY_HEADER}\n{memory}"
    novelty = str(novelty_hint or "").strip()
    if novelty:
        note += f"\n{novelty}"
    if break_rule:
        note = note.replace("按自己的兴趣看，不感兴趣直接退出；分享/互动结束后再判断。", BREAK_RULE_FLOW)
        note += break_rule_hint(True)
    return render_flexibility(note + LIVE_BROWSE_FLOW, flexibility)
