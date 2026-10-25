"""抓包流量工具：查看当前页面/最近抓包内容。"""

from __future__ import annotations

from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..page_content import page_traffic_footer
from ..traffic import DEFAULT_LIMIT as TRAFFIC_DEFAULT_LIMIT, render_traffic

from .base import _PhoneToolBase, _as_int, _ok, _wrap_call


@dataclass
class PhoneTrafficTool(_PhoneToolBase):
    """查看手机流量抓包（ADB + mitmproxy 解密）识别页面内容。"""

    name: str = "phone_traffic"
    description: str = (
        "查看手机抓包（插件直接在手机上抓包并解密 HTTP(S)）。scope=page（默认）只返回"
        "当前前台 App、最近一次手机操作之后的接口 URL/状态/响应片段，并标注抓包新鲜度，"
        "用于识别当前页面；scope=recent 返回保留窗口内全部最近流量，用于排查抓包或找"
        "可分享链接。抓包看不到图文、视频画面、布局等视觉信息（这些用多模态截图），"
        "证书固定/加密导致抓不到包的 App 也回退截图。评论/回复不要只凭抓包记录直接调"
        "接口：一开始必须用 UI 找到回复对象（用户/楼层），熟悉流程并积累记录后才可逐步"
        "参考抓包请求格式用 API。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["page", "recent"],
                    "description": (
                        "page（默认）=只取当前前台 App、最近手机操作之后的流量；"
                        "recent=保留窗口内最近全部流量。"
                    ),
                    "default": "page",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回条数（默认 20，最大 100；page 模式按页面分组取最新）。",
                    "default": TRAFFIC_DEFAULT_LIMIT,
                },
                "since_seconds": {
                    "type": "integer",
                    "description": "recent 模式只看最近多少秒；0=用配置 traffic.keep_seconds。",
                    "default": 0,
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard_light(context)
        if self.service.config.adb.mode == "app":
            from ..app_bridge import APP_UNSUPPORTED
            return "error: " + APP_UNSUPPORTED
        capture = getattr(self.service, "traffic", None)
        if capture is None:
            return "error: 抓包未初始化（插件未启用流量抓包配置）"
        status = capture.status()
        if not status.get("running"):
            reason = str(status.get("error") or "").strip()
            suffix = f"（{reason}）" if reason else ""
            return (
                f"error: 抓包未开启或未就绪{suffix}；"
                "可先用 phone_screen(mode=ui) 文本或截图识别页面"
            )
        scope = str(kwargs.get("scope") or "page").strip().lower()
        if scope not in {"page", "recent"}:
            return f"error: 不支持的 scope {scope!r}，只能是 page / recent"
        config = self.service.config
        limit = max(1, min(_as_int(kwargs.get("limit"), TRAFFIC_DEFAULT_LIMIT), 100))
        since = _as_int(kwargs.get("since_seconds"), 0)
        if since <= 0:
            since = int(config.traffic.keep_seconds)
        since = max(0, min(since, 86400))

        if scope == "page":
            # 与 phone_screen 共用同一份选择逻辑：前台 App 匹配、动作后窗口、时效过滤。
            snapshot = await self.service.page_traffic(
                force_refresh=True, limit=limit
            )
            if snapshot.has_content:
                head = capture.status_text()
                if snapshot.foreground_label:
                    head += (
                        f"\n当前前台：{snapshot.foreground_label}"
                        f"（{snapshot.foreground_package}）"
                    )
                return _ok(
                    f"{head}\n【当前页面抓包内容】\n{snapshot.text}\n"
                    + page_traffic_footer(snapshot, detailed=True)
                )
            reason = snapshot.reason or "当前页面没有可用抓包内容"
            flows = capture.recent(limit=limit, since_seconds=since)
            body = render_traffic(
                flows, window_seconds=since, body_chars=config.traffic.body_chars
            )
            return _ok(
                f"{capture.status_text()}\n"
                f"（当前页面抓包：{reason}；以下为最近流量，供参考）\n{body}"
            )

        flows = capture.recent(limit=limit, since_seconds=since)
        body = render_traffic(
            flows, window_seconds=since, body_chars=config.traffic.body_chars
        )
        return _ok(f"{capture.status_text()}\n{body}")
