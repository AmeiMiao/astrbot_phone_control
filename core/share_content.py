"""分享形态纯逻辑：链接校验、抓包取链接与「禁止纯文字分享」决策矩阵。

``phone_share_to_user`` 工具需要遵守一条硬规则：**分享内容不能只有纯文字**。
本模块把这条规则从工具层剥离成可本地单测的纯逻辑（仅依赖标准库，可 import
``core.traffic`` 的纯函数）：

* :func:`validate_http_link`：只接受 host 非空的 http(s) 链接；
* :func:`pick_share_link`：从 ``TrafficCapture.recent()`` 的原始 flow 里按时间窗
  提取可分享链接——只认 URL 含 ``share`` 的分享接口，``app_hint`` 严格过滤当前 App
  （支持中英文别名），未命中宁可返回空、不附其他 App 的链接；
* :func:`plan_share`：把「显式链接 / 抓包链接 / 截图兜底」翻译成分享计划，
  保证无链接时一定要求附图；
* :func:`join_share_text`：统一把正文与链接拼成最终分享文本；
* :func:`is_probably_share_page`：按 URL 判断分享接口/分享页。

模块不依赖 astrbot、不做 IO、不读配置，便于单测与在工具层复用。
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .traffic import extract_share_links, infer_flow_hints

# 抓包链接只看最近多少秒内的 flow（分享链接通常由刚触发的分享接口返回）。
SHARE_LINK_WINDOW_SECONDS = 180
# 单条 flow 最多提取多少个候选分享链接。
DEFAULT_SHARE_LINK_LIMIT = 5
# 抓包链接的来源说明（工具层/提示词共用，避免多处硬编码）。
TRAFFIC_SHARE_SOURCE = "抓包（页面推断）"


@dataclass(frozen=True)
class SharePlan:
    """一次分享的形态决策结果（工具层据此决定发什么、是否补截图）。"""

    text: str
    link: str
    link_source: str  # "manual" | "traffic" | ""
    screenshot_required: bool  # True=必须附图（工具自动截图或使用 screenshot_path）
    use_existing_screenshot: bool  # True=用传入的 screenshot_path，无需再截
    notes: tuple[str, ...]
    error: str  # 非空=无法分享（保留给工具层截图失败等极端情况）


def validate_http_link(link: Any) -> str:
    """校验并归一化分享链接：去首尾空白、必须是 http(s) 且 host 非空。

    Args:
        link: 任意候选链接（模型参数/抓包提取值）。

    Returns:
        合法时返回去空白后的原文，否则返回空串。
    """
    text = str(link or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"}:
        return ""
    if not parts.netloc:
        return ""
    return text


def _flow_ts(flow: Any) -> float | None:
    """读取 flow 的 ``ts``；缺失/非法/非正数返回 None（视为不可用记录）。"""
    if not isinstance(flow, dict):
        return None
    try:
        ts = float(flow.get("ts") or 0.0)
    except (TypeError, ValueError):
        return None
    return ts if ts > 0 else None


def _flow_host(flow: dict[str, Any]) -> str:
    """提取 flow URL 的 host（小写）；解析失败返回空串。"""
    url = str(flow.get("url") or "")
    try:
        return (urlsplit(url).netloc or "").lower()
    except (TypeError, ValueError):
        return ""


def _matches_app_hint(flow: dict[str, Any], hint_keywords: tuple[str, ...]) -> bool:
    """flow 是否命中 App 关键词：host 含关键词，或页面推断文本含关键词。"""
    if not hint_keywords:
        return False
    host = _flow_host(flow)
    try:
        hints = infer_flow_hints(flow).lower()
    except Exception:  # noqa: BLE001 - 推断失败按未命中处理
        hints = ""
    for keyword in hint_keywords:
        if keyword and (keyword in host or keyword in hints):
            return True
    return False


# App 中文名/别名 → 抓包 host 或页面推断里的关键词；避免「哔哩哔哩」匹配不到
# 「bilibili」这类中英文错位。
_APP_HINT_ALIASES: dict[str, tuple[str, ...]] = {
    "哔哩哔哩": ("bilibili", "b站"),
    "b站": ("bilibili",),
    "抖音": ("douyin", "aweme"),
    "小黑盒": ("xiaoheihe", "xiaoheihei"),
    "微信": ("weixin", "wechat"),
    "小红书": ("xiaohongshu",),
    "微博": ("weibo",),
    "知乎": ("zhihu",),
    "快手": ("kuaishou",),
    "bilibili": ("bilibili",),
    "douyin": ("douyin", "aweme"),
}


def _app_hint_keywords(hint: Any) -> tuple[str, ...]:
    """把 App 名/别名展开成匹配关键词（含中英文别名），空 hint 返回空元组。"""
    text = str(hint or "").strip().lower()
    if not text:
        return ()
    keywords = {text}
    keywords.update(_APP_HINT_ALIASES.get(text, ()))
    return tuple(sorted(keywords, key=len, reverse=True))


def pick_share_link(
    flows: Iterable[dict],
    *,
    now: float | None = None,
    window_seconds: int = SHARE_LINK_WINDOW_SECONDS,
    app_hint: str = "",
) -> tuple[str, str]:
    """从最近抓包记录中提取可分享链接。

    规则（宁可拿不到链接退回截图，也不附错链接）：

    1. 只看 ``ts`` 落在 ``now - window_seconds`` 之后的 flow；
    2. 只认 **URL 含 ``share`` 的分享接口/分享页** flow；信息流/详情响应里的
       ``share_url`` 不对应当前内容，不用于自动分享；
    3. 提供 ``app_hint`` 时只保留命中当前 App 的 flow（支持中英文别名），
       未命中直接返回空——不回退到其他 App 的 flow；
    4. 多候选按 flow 时间从新到旧，用 :func:`core.traffic.extract_share_links`
       提取（已过滤图片/脚本等静态资源）；
    5. 返回 ``(link, "抓包（页面推断）")``；没有候选时返回 ``("", "")``。

    Args:
        flows: ``TrafficCapture.recent()`` 返回的原始 flow dict（含 ts/url/body）。
        now: 当前时间戳；缺省取 ``time.time()``。
        window_seconds: 时间窗（秒），默认 180。
        app_hint: App 名/别名或 host 关键词（如「哔哩哔哩」「B站」「douyin」）。

    Returns:
        ``(可分享链接, 来源说明)``。
    """
    current = float(now) if now is not None else time.time()
    try:
        window = max(0.0, float(window_seconds))
    except (TypeError, ValueError):
        window = float(SHARE_LINK_WINDOW_SECONDS)
    keywords = _app_hint_keywords(app_hint)

    candidates: list[tuple[float, dict[str, Any]]] = []
    for flow in flows or ():
        ts = _flow_ts(flow)
        if ts is None or current - ts > window:
            continue
        if not is_probably_share_page(flow):
            continue
        if keywords and not _matches_app_hint(flow, keywords):
            continue
        candidates.append((ts, flow))
    candidates.sort(key=lambda item: item[0], reverse=True)

    seen: set[str] = set()
    for _ts, flow in candidates:
        try:
            links = extract_share_links(flow, limit=DEFAULT_SHARE_LINK_LIMIT)
        except Exception:  # noqa: BLE001 - 单条 flow 解析失败不影响其他候选
            continue
        for link in links:
            normalized = validate_http_link(link)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            return normalized, TRAFFIC_SHARE_SOURCE
    return "", ""


def plan_share(
    *,
    text: str = "",
    link: str = "",
    include_screenshot: bool = False,
    screenshot_path: str = "",
    traffic_link: str = "",
    traffic_source: str = "",
) -> SharePlan:
    """生成分享计划，保证「绝不允许纯文字分享」。

    决策矩阵：

    1. 显式 ``link`` 合法 → ``link_source="manual"``；非法则忽略并记 note；
    2. 无显式链接时使用 ``traffic_link``（合法则 ``link_source="traffic"`` 并记 note）；
    3. 仍无链接 → ``screenshot_required=True``（即使 ``include_screenshot=False``）：
       传入 ``screenshot_path`` 时复用已有截图，否则提示工具自动截图；
    4. 已有链接且 ``include_screenshot=True`` → ``screenshot_required=True``（链接+截图）；
    5. ``error`` 默认留空——本设计中总能截图，字段保留给工具层截图失败时使用。

    ``use_existing_screenshot`` 在「需要截图且传入 ``screenshot_path``」时均为 True，
    避免集成层对已有截图重复抓取。

    Args:
        text: 分享正文（原样返回，不拼接链接）。
        link: 模型显式给出的链接。
        include_screenshot: 是否要求同时附图。
        screenshot_path: 已准备好的截图路径；非空表示可直接复用。
        traffic_link: 抓包层提取到的候选链接。
        traffic_source: 抓包来源说明（如 ``pick_share_link`` 的返回值）。

    Returns:
        :class:`SharePlan` 决策结果。
    """
    notes: list[str] = []
    body = str(text or "")
    explicit = validate_http_link(link)
    if str(link or "").strip() and not explicit:
        notes.append("显式链接不是有效的 http(s) 链接，已忽略")

    resolved = ""
    source = ""
    if explicit:
        resolved = explicit
        source = "manual"
    else:
        traffic = validate_http_link(traffic_link)
        if str(traffic_link or "").strip() and not traffic:
            notes.append("抓包链接无效，已忽略")
        if traffic:
            resolved = traffic
            source = "traffic"
            if traffic_source:
                notes.append(f"链接来自{traffic_source}")
            else:
                notes.append("链接来自抓包")

    has_existing = bool(str(screenshot_path or "").strip())
    if resolved:
        if include_screenshot:
            notes.append("已按请求同时附带截图")
            return SharePlan(
                text=body,
                link=resolved,
                link_source=source,
                screenshot_required=True,
                use_existing_screenshot=has_existing,
                notes=tuple(notes),
                error="",
            )
        return SharePlan(
            text=body,
            link=resolved,
            link_source=source,
            screenshot_required=False,
            use_existing_screenshot=False,
            notes=tuple(notes),
            error="",
        )

    # 没有可分享链接：必须附图，绝不允许只发文字。
    if has_existing:
        notes.append("未获取到链接，使用已有截图")
    else:
        notes.append("未获取到链接，已自动附截图兜底")
    return SharePlan(
        text=body,
        link="",
        link_source="",
        screenshot_required=True,
        use_existing_screenshot=has_existing,
        notes=tuple(notes),
        error="",
    )


def join_share_text(text: str, link: str) -> str:
    """把正文与链接拼成最终分享文本（链接非空时追加「链接：<link>」行）。"""
    body = str(text or "")
    target = str(link or "").strip()
    if not target:
        return body
    if not body:
        return f"链接：{target}"
    return f"{body}\n链接：{target}"


def is_probably_share_page(flow: dict[str, Any]) -> bool:
    """判断一条 flow 是否像分享接口/分享页。

    只认 URL 含 ``share`` 的请求（如 App 点「分享」后触发的 ``/x/share/...``、
    ``/aweme/v1/share/...``）：信息流/详情响应里虽然也可能带 ``share_url``，
    但无法对应当前正在看的内容，不能用于自动分享。
    """
    if not isinstance(flow, dict):
        return False
    url = str(flow.get("url") or "").lower()
    return "share" in url
