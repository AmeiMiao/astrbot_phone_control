"""互动流程记账工具：评论/被动回复/发帖的名额申请与结果记录。"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..config import UNCONFIGURED_MESSAGE
from ..errors import AdbNotConfiguredError
from ..interactions import (
    KIND_COMMENT,
    KIND_LABELS,
    KIND_PASSIVE_REPLY,
    LIGHT_KINDS,
    interaction_run,
    InteractionError,
    evaluate_gate,
    interaction_limits,
    normalize_kind,
    normalize_summary,
)

from .base import (
    _PermissionDenied,
    _PhoneToolBase,
    _error,
    _event_of,
    _wrap_call,
    check_admin_permission,
)


@dataclass
class PhoneInteractionGateTool(_PhoneToolBase):
    """评论/被动回复/发帖前申请一个名额（插件只做许可/限额/记账）。"""

    name: str = "phone_interaction_gate"
    description: str = (
        "点赞、收藏、评论、被动回复或发帖前先申请名额。like=点赞，favorite=收藏；"
        "两者由 security.allow_interactions 授权，合计受 max_likes_per_session 单轮上限约束。"
        "插件不编排固定 App 流程，只做许可、每日限额、"
        "冷却与记账：允许时返回「已预留 1 次名额」，之后请自己用 phone_status / "
        "phone_screen / phone_tap / phone_input_text / phone_key 等低层工具完成操作，完成"
        "后必须调用 phone_interaction_record 记录成功/失败。kind 语义：comment=主动评论"
        "（你刷帖/看视频时主动发的评论或回复）；passive_reply=被动回复（别人回复了机器人"
        "后，先 phone_check_inbox 看消息，再按自己的心情与内容判断要不要回、回什么）；"
        "post=论坛发帖。app 可选（如 bilibili/douyin/xiaoheihe）。三类互动分别受 "
        "security.allow_comments / allow_passive_replies / allow_posts 开关、各自每日上限"
        "与各自冷却限制；每轮最多 1 条，不刷屏。请注意：这些开关与限额是流程授权与配额"
        "记账，不是底层动作拦截；若跳过本工具直接操作，插件无法从技术上阻止实际发布。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["like", "favorite", "comment", "passive_reply", "post"],
                    "description": (
                        "comment=主动评论/回复（刷帖/看视频时主动发）；"
                        "passive_reply=被动回复（有人回复机器人后按心情与内容决定）；"
                        "post=发帖；like=点赞；favorite=收藏。"
                    ),
                },
                "app": {
                    "type": "string",
                    "description": "目标 App（中文名/别名/包名，可留空由你按当前页面判断）。",
                    "default": "",
                },
            },
            "required": ["kind"],
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = _event_of(context)
        config = self.service.config
        denied = check_admin_permission(event, config)
        if denied:
            raise _PermissionDenied(denied)
        if not config.adb.configured:
            raise AdbNotConfiguredError(UNCONFIGURED_MESSAGE)
        try:
            kind = normalize_kind(kwargs.get("kind"))
        except InteractionError as exc:
            return _error(exc)
        security = config.security
        cooldown_kind: str | None = None
        if kind == KIND_COMMENT and not config.flexibility.free_browse_active and hasattr(self.service, "_contents"):
            task = self.service.contents.active.get(self.service.contents.owner(event))
            if task is not None and not any(d["decision"] in {"comment", "reply_comment"} for d in task["decisions"]):
                return "error: 请先独立 decide comment 发表正文感想，或针对已读具体评论 decide reply_comment；阅读评论不等于回复"
        if kind in LIGHT_KINDS:
            allow = security.allow_interactions
            max_per_day = 0
            cooldown_seconds = 0
            allow_key = "security.allow_interactions"
            if not config.flexibility.free_browse_active and hasattr(self.service, "_contents"):
                task = self.service.contents.active.get(self.service.contents.owner(event))
                if task is not None and not any(d["decision"] == kind for d in task["decisions"]):
                    return f"error: 请先针对当前内容独立 decide {kind}"
        elif kind == KIND_COMMENT:
            allow = security.allow_comments
            max_per_day = security.max_comments_per_day
            cooldown_seconds = security.interaction_cooldown_seconds
            allow_key = "security.allow_comments"
        elif kind == KIND_PASSIVE_REPLY:
            allow = security.allow_passive_replies
            max_per_day = security.max_passive_replies_per_day
            cooldown_seconds = security.passive_reply_cooldown_seconds
            allow_key = "security.allow_passive_replies"
            cooldown_kind = KIND_PASSIVE_REPLY
        else:
            allow = security.allow_posts
            max_per_day = security.max_posts_per_day
            cooldown_seconds = security.interaction_cooldown_seconds
            allow_key = "security.allow_posts"
        if kind not in LIGHT_KINDS:
            max_per_day, cooldown_seconds = interaction_limits(security, config.flexibility, kind)
        label = KIND_LABELS[kind]
        if not allow:
            return f"error: 未开启{label}，需在插件配置 {allow_key} 打开"
        quota_error = self.service.quota_error_text()
        if quota_error:
            return quota_error
        budget = self.service.ensure_play_budget(event)
        if budget is not None and budget.completed:
            return "本轮玩手机预算已完成，请立即收尾汇报（可 phone_share_to_user 一次）"
        session = str(getattr(event, "unified_msg_origin", "") or "")
        run = interaction_run(event)
        if kind in LIGHT_KINDS:
            session = f"{session}|{run}"
        gate_kwargs: dict[str, Any] = {
            "kind": kind,
            "allow": True,
            "max_per_day": max_per_day,
            "cooldown_seconds": cooldown_seconds,
            "app": str(kwargs.get("app") or ""),
            "session": session,
            "run": run,
            "max_per_session": security.max_likes_per_session,
        }
        if cooldown_kind:
            gate_kwargs["cooldown_kind"] = cooldown_kind
        try:
            decision = evaluate_gate(self.service.interactions, **gate_kwargs)
        except InteractionError as exc:
            return _error(exc)
        if not decision.allowed:
            return f"error: {decision.message}"
        return decision.message


@dataclass
class PhoneInteractionRecordTool(_PhoneToolBase):
    """记录本轮评论/被动回复/发帖的结果（成功计次，失败释放名额）。"""

    name: str = "phone_interaction_record"
    description: str = (
        "记录本轮点赞、收藏、评论、被动回复或发帖的结果：success=true 表示操作成功（计一次当日用量），"
        "success=false 表示失败/放弃（释放名额、不计用量并写明原因）。summary 由你自己"
        "用不超过 120 字概括这次评论/被动回复/发帖的内容或失败原因（会写入插件记录，供"
        "/phone interactions 查看）。必须先调用 phone_interaction_gate 预约名额，"
        "未预约直接记录会报错。每次操作只调用一次，不要重复发送内容。请注意：本工具只做"
        "流程记账，不是底层动作拦截；跳过申请或记录时插件无法保证配额与统计准确。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["like", "favorite", "comment", "passive_reply", "post"],
                    "description": (
                        "comment=主动评论/回复；passive_reply=被动回复；post=发帖；like=点赞；favorite=收藏。"
                    ),
                },
                "success": {
                    "type": "boolean",
                    "description": "是否成功完成评论/被动回复/发帖。",
                },
                "summary": {
                    "type": "string",
                    "description": "不超过 120 字的说明：成功写内容简述，失败写明确原因。",
                },
                "app": {
                    "type": "string",
                    "description": "目标 App（中文名/别名/包名，可留空）。",
                    "default": "",
                },
            },
            "required": ["kind", "success", "summary"],
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = _event_of(context)
        config = self.service.config
        denied = check_admin_permission(event, config)
        if denied:
            raise _PermissionDenied(denied)
        if not config.adb.configured:
            raise AdbNotConfiguredError(UNCONFIGURED_MESSAGE)
        try:
            kind = normalize_kind(kwargs.get("kind"))
            summary = normalize_summary(kwargs.get("summary"))
        except InteractionError as exc:
            return _error(exc)
        success = kwargs.get("success")
        if not isinstance(success, bool):
            return "error: success 必须是布尔值 true/false"
        session = str(getattr(event, "unified_msg_origin", "") or "")
        run = interaction_run(event)
        if kind in LIGHT_KINDS:
            session = f"{session}|{run}"
        try:
            entry = self.service.interactions.record_result(
                kind=kind,
                success=success,
                summary=summary,
                app=str(kwargs.get("app") or ""),
                session=session,
                run=run if kind in LIGHT_KINDS else None,
            )
        except InteractionError as exc:
            return _error(exc)
        success = entry["success"]
        security = config.security
        label = KIND_LABELS[kind]
        if kind in LIGHT_KINDS:
            used, pending = self.service.interactions.run_usage(run)
            state = "成功" if success else "失败并释放名额"
            return (f"已记录{label}{state}（本轮点赞/收藏合计 {used}/{security.max_likes_per_session}，"
                    f"待完成 {pending}）：{entry['summary']}。请核对状态，不要重复操作。")
        if kind == KIND_COMMENT:
            limit = security.max_comments_per_day
        elif kind == KIND_PASSIVE_REPLY:
            limit = security.max_passive_replies_per_day
        else:
            limit = security.max_posts_per_day
        used = self.service.interactions.used_today(kind)
        used_text = f"今日 {used}，不限次数" if int(limit) <= 0 else f"今日 {used}/{limit}"
        if success:
            return (
                f"已记录 1 次{label}成功（{used_text}）：{entry['summary']}。"
                "请继续收尾，不要重复发送相同内容。"
            )
        return (
            f"已记录{label}失败并释放名额（{used_text}）：{entry['summary']}。"
            "请停止重试，把失败原因如实告诉用户。"
        )
