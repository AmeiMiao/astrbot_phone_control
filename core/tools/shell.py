"""经配置授权的 Android 设备 shell 工具。"""

from __future__ import annotations

import asyncio
import math
from contextlib import suppress

from pydantic import Field
from pydantic.dataclasses import dataclass

from ..device_gate import current_scope
from ..errors import PhoneControlError
from .base import _PhoneToolBase, _json_text, _wrap_call


@dataclass
class PhoneShellTool(_PhoneToolBase):
    name: str = "phone_shell"
    description: str = (
        "执行当前 Android 设备的 shell 命令，需开启 security.allow_shell_tool。"
        "使用设备当前 ADB 身份权限；命令只执行一次，不自动重试，返回退出码和标准输出/错误。"
        "用于设备诊断和必要的系统操作；优先用现有专用工具。"
        "前台命令受设备租约、暂停、取消和超时约束，每路输出最多 8 KiB；"
        "不要启动后台或脱离会话的任务，执行结果不确定时先检查设备。"
    )
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Android shell 命令（最多 8192 字符）"},
            "timeout_seconds": {"type": "number", "description": "超时秒数，上限 adb.command_timeout；0 使用配置", "default": 0},
        },
        "required": ["command"],
    })

    @_wrap_call
    async def call(self, context, **kwargs):
        await self._guard(context)
        if not self.service.config.security.allow_shell_tool:
            return "error: 未开启设备 shell，需在插件配置 security.allow_shell_tool 打开"
        command = kwargs.get("command")
        if not isinstance(command, str) or not command.strip() or "\x00" in command or len(command) > 8192:
            return "error: command 必须是 1-8192 字符且不含 NUL 的命令"
        try:
            timeout = float(kwargs.get("timeout_seconds", 0))
        except (TypeError, ValueError):
            return "error: timeout_seconds 必须是有限的非负数"
        if not math.isfinite(timeout) or timeout < 0:
            return "error: timeout_seconds 必须是有限的非负数"
        maximum = self.service.config.adb.command_timeout
        timeout = min(timeout or maximum, maximum)
        scope = current_scope()

        async def execute():
            await self.service.adb.ensure_ready()
            if scope is not None:
                scope.checkpoint("phone_shell")
            try:
                return await self.service.adb.shell_result(command, timeout=timeout)
            finally:
                invalidate = getattr(self.service, "_invalidate_ui_cache", None)
                if callable(invalidate):
                    invalidate()

        task = asyncio.create_task(execute())
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                if asyncio.get_running_loop().time() >= deadline:
                    raise asyncio.TimeoutError
                if scope is not None:
                    scope.checkpoint("phone_shell")
                if not self.service.config.security.allow_shell_tool:
                    raise PhoneControlError("设备 shell 授权已关闭，命令已中止")
                done, _ = await asyncio.wait({task}, timeout=0.05)
                if done:
                    if scope is not None:
                        scope.checkpoint("phone_shell")
                    result = await task
                    return ("error: " if result["exit_code"] else "") + _json_text(result)
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
