"""WireGuard 组网工具：查看状态、开关/重启 helper、输出导入用配置。"""

from __future__ import annotations

from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .base import _PhoneToolBase, _ok, _wrap_call


@dataclass
class PhoneWireguardTool(_PhoneToolBase):
    """管理插件内置 userspace WireGuard helper（client/server 组网）。"""

    name: str = "phone_wireguard"
    description: str = (
        "WireGuard 组网管理（插件内置 userspace helper，无需 NET_ADMIN/tun）："
        "status=运行状态/健康/对端握手/转发连接；start/stop/restart=开启、停止或重启 "
        "helper（幂等，会同步保存统一组网开关并选择 WireGuard）；config=导入用配置（client 模式"
        "显示服务器侧需添加的 Peer 片段，server 模式显示手机端完整 WireGuard 配置）。"
        "ADB over WG 是否自动切换由配置 network.wireguard.auto_adb 决定；修改 host/端口/密钥等"
        "配置项请让用户在插件配置页保存（会热应用重启 helper）。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "start", "stop", "restart", "config"],
                    "description": (
                        "status=查看状态；start/stop/restart=开关/重启 helper；"
                        "config=查看导入用 WireGuard 配置文本。"
                    ),
                    "default": "status",
                },
            },
        }
    )

    @_wrap_call
    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        await self._guard_light(context)
        manager = getattr(self.service, "wireguard", None)
        if manager is None:
            return "error: WireGuard 未初始化（插件未装配该能力）"
        action = str(kwargs.get("action") or "status").strip().lower()
        result = await manager.control(action)
        text = str(result.get("text") or "")
        if not result.get("ok") and text and not text.startswith("error:"):
            text = f"error: {text}"
        return _ok(text)
