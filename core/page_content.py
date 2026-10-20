"""当前页面内容识别：把抓包流量与屏幕信息按前台 App 与时序聚合（纯逻辑）。

抓包（``traffic``/``traffic_capture``）与屏幕识别（UI 树/截图/OCR）原本是两个
独立来源，由模型自行决定先后；实际使用中存在三个问题：

* 同一页面的流量可能来自后台 App 或上一页面，模型不核对就当成当前内容；
* 抓包默认窗口很长（``traffic.keep_seconds``），一次返回大量历史请求，既费 token
  又难判断哪条对应当前页面；
* 模型需要分别调用 ``phone_traffic`` 与 ``phone_screen``，多一次往返，且两侧时间
  没有对齐。

本模块提供确定性的「当前页面流量快照」，供 ``phone_screen`` / ``phone_traffic``
共用：

* 以前台包名为准筛选流量：只保留域名/接口推断出的 App 与当前前台一致的记录；
* 以最近一次设备写动作时间为下界：动作前的流量属于旧页面，不作为当前内容；
* 过旧流量（默认 90 秒）丢弃，并在结果里标注最新流量的年龄供模型判断新鲜度；
* 同一页面（推断的 ``App·页面`` 或 URL 路径）只保留最新一条，避免同类接口刷屏；
* 渲染成紧凑文本并给出降级原因，屏幕侧据此决定是否还需要 OCR/截图。

本模块不依赖 astrbot 与设备，便于本地单测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

from .apps import KNOWN_APP_LABELS, app_label_for_package, resolve_app
from .traffic import extract_share_links, infer_flow_hints, mask_url

# 当前页面流量的默认选择参数
PAGE_FLOW_LIMIT = 6
PAGE_FLOW_MAX_AGE_SECONDS = 90.0
PAGE_ACTION_WINDOW_MAX_SECONDS = 180.0
PAGE_BODY_CHARS = 600
PAGE_COMPACT_BODY_CHARS = 400
PAGE_MAX_TEXT_CHARS = 1800
PAGE_COMPACT_MAX_TEXT_CHARS = 900
STALE_FLOW_WARN_SECONDS = 30.0

# 抓包推断出的 App 标签 → 包名（内置画像优先，其次常见 App 中文名映射）。
_LABEL_TO_PACKAGE: dict[str, str] = {
    label: package for package, label in KNOWN_APP_LABELS.items()
}


@dataclass(frozen=True)
class PageTrafficSnapshot:
    """当前页面相关的抓包内容快照（供工具层渲染）。"""

    running: bool = False
    foreground_package: str = ""
    foreground_label: str = ""
    activity: str = ""
    flows: tuple[dict[str, Any], ...] = ()
    text: str = ""
    newest_age_seconds: float | None = None
    matched: bool = False
    """是否有流量可确认属于当前前台 App。"""
    reason: str = ""
    notes: tuple[str, ...] = ()
    window_seconds: float = 0.0
    action_window: bool = False

    @property
    def has_content(self) -> bool:
        """是否拿到了可展示的当前页面流量。"""
        return bool(self.flows and self.text)


def app_label_for_hint(hint: str) -> str:
    """从 ``infer_flow_hints`` 的 ``App·页面`` 文本里取 App 标签；未知返回空串。"""
    text = str(hint or "").strip()
    if "·" not in text:
        return ""
    return text.split("·", 1)[0].strip()


def label_package(label: str) -> str:
    """把 App 标签解析成包名（B站/哔哩哔哩、抖音、小黑盒等），未知返回空串。"""
    text = str(label or "").strip()
    if not text:
        return ""
    profile = resolve_app(text)
    if profile is not None and profile.package:
        return profile.package
    return _LABEL_TO_PACKAGE.get(text, "")


def foreground_app(package: str) -> tuple[str, str]:
    """返回前台 ``(包名, 可读标签)``；标签未收录时用包名本身。"""
    text = str(package or "").strip()
    if not text:
        return "", ""
    label = app_label_for_package(text)
    if not label:
        profile = resolve_app(text)
        if profile is not None and profile.label and profile.label != text:
            label = profile.label
    return text, label or text


def flow_app(flow: dict[str, Any]) -> tuple[str, str]:
    """返回流量的 ``(App 标签, 包名)``；无法归属时返回两个空串。"""
    hint = infer_flow_hints(flow)
    label = app_label_for_hint(hint)
    if not label:
        return "", ""
    return label, label_package(label)


def flow_matches_foreground(flow: dict[str, Any], foreground_package: str) -> bool | None:
    """判断流量是否属于当前前台 App。

    Returns:
        True/False：可归属且匹配或不匹配；None：流量无法归属到已知 App。
    """
    expected = str(foreground_package or "").strip().lower()
    if not expected:
        return None
    _label, package = flow_app(flow)
    if not package:
        return None
    return package.strip().lower() == expected


def _flow_ts(flow: dict[str, Any]) -> float:
    try:
        return float(flow.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _group_key(flow: dict[str, Any]) -> str:
    """页面分组键：优先用 App·页面推断，其次 URL 路径。"""
    hint = infer_flow_hints(flow)
    if hint and "·" in hint:
        return hint
    url = str(flow.get("url") or "")
    try:
        path = urlsplit(url).path
    except Exception:  # noqa: BLE001 - 异常 URL 退回原文前缀
        path = ""
    return path or url.split("?", 1)[0]


@dataclass(frozen=True)
class PageFlowSelection:
    flows: tuple[dict[str, Any], ...] = ()
    matched: bool = False
    unknown_foreground: bool = False
    reason: str = ""
    newest_age_seconds: float | None = None
    action_window: bool = False
    notes: tuple[str, ...] = ()


def select_page_flows(
    flows: Iterable[dict[str, Any]],
    *,
    foreground_package: str = "",
    now: float | None = None,
    since_ts: float = 0.0,
    limit: int = PAGE_FLOW_LIMIT,
    max_age_seconds: float = PAGE_FLOW_MAX_AGE_SECONDS,
) -> PageFlowSelection:
    """从最近抓包中选出属于当前前台 App、动作之后且未过期的流量。

    Args:
        flows: ``TrafficCapture.recent()`` 返回的原始流量（时间正序）。
        foreground_package: 当前前台包名；为空表示未知，退化为最近流量。
        now: 当前墙钟秒；默认 ``time.time()``。
        since_ts: 最近一次设备写动作的墙钟秒；0 表示不做动作窗口过滤。
        limit: 最多保留多少个页面分组（同一页面只取最新一条）。
        max_age_seconds: 流量最大年龄；更旧的视为旧页面内容丢弃。

    Returns:
        ``PageFlowSelection``：选中流量、是否匹配前台、降级原因与提示。
    """
    current = float(now) if now is not None else time.time()
    count = max(1, int(limit))
    records = sorted(
        (flow for flow in flows if _flow_ts(flow) > 0),
        key=_flow_ts,
    )
    if not records:
        return PageFlowSelection(reason="最近没有抓到可识别的接口流量")

    action_window = False
    window_start = float(since_ts or 0)
    if window_start and current - window_start <= PAGE_ACTION_WINDOW_MAX_SECONDS:
        action_window = True
        records = [flow for flow in records if _flow_ts(flow) >= window_start]
        if not records:
            return PageFlowSelection(
                reason="最近一次设备动作之后还没有新的接口流量（页面可能刚切换或使用缓存）",
                action_window=True,
            )

    fresh: list[dict[str, Any]] = []
    for flow in records:
        age = current - _flow_ts(flow)
        if age <= max_age_seconds:
            fresh.append(flow)
    if not fresh:
        return PageFlowSelection(
            reason=f"最近流量已过期（最新一条在 {max_age_seconds:.0f} 秒前以上），页面可能已变化",
            newest_age_seconds=round(current - _flow_ts(records[-1]), 1),
            action_window=action_window,
        )
    records = fresh

    matched = False
    unknown_foreground = False
    expected = str(foreground_package or "").strip()
    if expected:
        attributable = [flow for flow in records if flow_app(flow)[1]]
        matched_flows = [
            flow
            for flow in records
            if flow_matches_foreground(flow, expected) is True
        ]
        if not matched_flows:
            reason = (
                "最近流量的域名未收录，无法确认是否属于当前前台 App"
                if not attributable
                else "最近流量不属于当前前台 App（可能来自后台请求或上一页面）"
            )
            return PageFlowSelection(
                reason=reason,
                newest_age_seconds=round(current - _flow_ts(records[-1]), 1),
                action_window=action_window,
            )
        records = matched_flows
        matched = True
    else:
        unknown_foreground = True

    # 同一页面只保留最新一条：信息流/详情页会连续请求多个同类接口，
    # 全量展示既费 token 又让模型分不清主次。
    groups: dict[str, dict[str, Any]] = {}
    for flow in sorted(records, key=_flow_ts, reverse=True):
        key = _group_key(flow)
        if key not in groups:
            groups[key] = flow
        if len(groups) >= count:
            break
    selected = sorted(groups.values(), key=_flow_ts)

    notes: list[str] = []
    if unknown_foreground:
        notes.append("无法确认当前前台 App，按最近流量展示")
    if not action_window:
        notes.append(
            "设备动作时间过旧，按流量新旧过滤"
            if since_ts
            else "未记录到设备动作时间，按流量新旧过滤"
        )
    newest_age = round(current - _flow_ts(selected[-1]), 1) if selected else None
    return PageFlowSelection(
        flows=tuple(selected),
        matched=matched,
        unknown_foreground=unknown_foreground,
        newest_age_seconds=newest_age,
        action_window=action_window,
        notes=tuple(notes),
    )


def render_page_flows(
    flows: Iterable[dict[str, Any]],
    *,
    now: float | None = None,
    body_chars: int = PAGE_BODY_CHARS,
    max_total_chars: int = PAGE_MAX_TEXT_CHARS,
) -> str:
    """把选中的页面流量渲染成紧凑文本（URL 打码、正文截断、总量封顶）。"""
    current = float(now) if now is not None else time.time()
    chars = max(80, int(body_chars))
    budget = max(200, int(max_total_chars))
    lines: list[str] = []
    used = 0
    records = list(flows)
    for index, flow in enumerate(records):
        ts = _flow_ts(flow)
        age = max(0, int(current - ts))
        head = (
            f"[{time.strftime('%H:%M:%S', time.localtime(ts))}，{age}s 前] "
            f"{flow.get('method') or 'GET'} {mask_url(flow.get('url'))}"
            f" → {flow.get('status')}"
        )
        ctype = str(flow.get("ctype") or "").split(";")[0].strip()
        if ctype:
            head += f" {ctype}"
        hint = infer_flow_hints(flow)
        if hint:
            head += f" | {hint}"
        body = str(flow.get("body") or "").strip().replace("\n", " ")
        req_body = str(flow.get("reqbody") or "").strip().replace("\n", " ")
        excerpt = ""
        if body:
            excerpt = body[:chars] + ("…" if len(body) > chars else "")
        elif req_body:
            excerpt = "请求：" + req_body[: max(60, chars // 2)]
        block = head
        if excerpt:
            block += f"\n  响应：{excerpt}" if body else f"\n  {excerpt}"
        links = extract_share_links(flow)
        if links:
            block += "\n  可分享链接：" + " | ".join(links[:3])
        if used + len(block) > budget:
            lines.append(f"…（其余 {len(records) - index} 条省略）")
            break
        lines.append(block)
        used += len(block)
    return "\n".join(lines)


def build_page_traffic(
    *,
    flows: Iterable[dict[str, Any]] = (),
    running: bool = False,
    error: str = "",
    foreground_package: str = "",
    activity: str = "",
    now: float | None = None,
    since_ts: float = 0.0,
    limit: int = PAGE_FLOW_LIMIT,
    body_chars: int = PAGE_BODY_CHARS,
    max_total_chars: int = PAGE_MAX_TEXT_CHARS,
    max_age_seconds: float = PAGE_FLOW_MAX_AGE_SECONDS,
) -> PageTrafficSnapshot:
    """构造当前页面流量快照（选择 + 渲染 + 降级说明）。"""
    current = float(now) if now is not None else time.time()
    package, label = foreground_app(foreground_package)
    if not running:
        return PageTrafficSnapshot(
            running=False,
            foreground_package=package,
            foreground_label=label,
            activity=str(activity or ""),
            reason=str(error or "抓包未开启"),
        )
    selection = select_page_flows(
        flows,
        foreground_package=package,
        now=current,
        since_ts=since_ts,
        limit=limit,
        max_age_seconds=max_age_seconds,
    )
    if not selection.flows:
        return PageTrafficSnapshot(
            running=True,
            foreground_package=package,
            foreground_label=label,
            activity=str(activity or ""),
            reason=selection.reason,
            notes=selection.notes,
            newest_age_seconds=selection.newest_age_seconds,
            action_window=selection.action_window,
        )
    text = render_page_flows(
        selection.flows,
        now=current,
        body_chars=body_chars,
        max_total_chars=max_total_chars,
    )
    notes = list(selection.notes)
    if (
        selection.newest_age_seconds is not None
        and selection.newest_age_seconds > STALE_FLOW_WARN_SECONDS
    ):
        notes.append(
            f"最新流量在 {selection.newest_age_seconds:.0f} 秒前，若刚切换页面可能尚未产生新请求"
        )
    return PageTrafficSnapshot(
        running=True,
        foreground_package=package,
        foreground_label=label,
        activity=str(activity or ""),
        flows=selection.flows,
        text=text,
        newest_age_seconds=selection.newest_age_seconds,
        matched=selection.matched,
        notes=tuple(notes),
        window_seconds=float(max_age_seconds),
        action_window=selection.action_window,
    )


def page_traffic_footer(snapshot: PageTrafficSnapshot, *, detailed: bool = False) -> str:
    """生成页面流量快照的来源/新鲜度说明（不含正文）。"""
    if not snapshot.flows:
        return "（抓包不可用，页面内容以屏幕/截图识别为准）"
    parts: list[str] = []
    if snapshot.newest_age_seconds is not None:
        parts.append(f"抓包最新一条在 {snapshot.newest_age_seconds:.0f} 秒前")
    if snapshot.action_window:
        parts.append("已按最近设备动作过滤旧页面流量")
    if detailed and snapshot.foreground_label:
        parts.append(
            f"当前前台：{snapshot.foreground_label}（{snapshot.foreground_package}）"
            + (f"/{snapshot.activity}" if snapshot.activity else "")
        )
    parts.extend(snapshot.notes)
    parts.append(
        "图文、视频画面、布局等视觉信息仍以多模态截图/OCR 为准"
        if detailed
        else "视觉信息仍以截图/OCR 为准"
    )
    return "（" + "；".join(parts) + "）"
