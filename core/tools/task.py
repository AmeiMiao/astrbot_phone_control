"""后台任务工具：把多步任务转入静默后台执行。"""

from __future__ import annotations

from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..config import UNCONFIGURED_MESSAGE
from ..errors import AdbNotConfiguredError
from ..scheduler import (
    BackgroundTaskError,
    create_background_task,
    validate_background_instruction,
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
class PhoneStartBackgroundTaskTool(_PhoneToolBase):
    """把多步手机任务转入静默后台执行，完成后主动汇报。"""

    name: str = "phone_start_background_task"
    description: str = (
        "把用户委托的多步手机任务（答题、找内容、发帖、评论、整理等）转入静默后台执行，"
        "避免在对话里逐步刷屏。instruction 用 1-500 字描述要做什么；share_target 可指定"
        "汇报会话 UMO（留空用当前会话）。创建 run_once 主动任务后立即返回，不等待完成；"
        "任务全程静默，完成后会用 phone_share_to_user 汇报。只有用户明确要求「现在/实时/"
        "看着你操作」时才在前台做；工具循环期间也不要输出中间解说文字。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "任务指令（1-500 字），例如「去 B 站看三个科技视频并总结」。",
                },
                "share_target": {
                    "type": "string",
                    "description": "完成后汇报的会话 UMO；留空用当前会话。",
                    "default": "",
                },
            },
            "required": ["instruction"],
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
            instruction = validate_background_instruction(
                kwargs.get("instruction")
            )
        except BackgroundTaskError as exc:
            return _error(exc)
        share_target = str(kwargs.get("share_target") or "").strip() or str(
            getattr(event, "unified_msg_origin", "") or ""
        )
        app_context = getattr(self.service, "app_context", None)
        cron_manager = getattr(app_context, "cron_manager", None)
        try:
            job = await create_background_task(
                cron_manager,
                instruction=instruction,
                share_target=share_target,
            )
        except BackgroundTaskError as exc:
            return _error(exc)
        try:
            await self.service.apply_task_provider(job)
        except Exception:  # noqa: BLE001 - 供应商应用失败不影响任务创建
            pass
        return (
            f"已转入后台执行（job {getattr(job, 'job_id', '?')}，静默运行），"
            "完成后会通过分享汇报到当前会话，无需等待；可用 /phone tasks 查看。"
        )
