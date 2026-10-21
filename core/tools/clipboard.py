"""读取安卓设备剪贴板，供复制分享链接后的工具调用使用。"""

from __future__ import annotations

from pydantic import Field
from pydantic.dataclasses import dataclass

from .base import _PhoneToolBase, _wrap_call


@dataclass
class PhoneClipboardTool(_PhoneToolBase):
    name: str = "phone_clipboard"
    description: str = (
        "读取手机/模拟器当前剪贴板文本。点击 App 的复制分享链接后调用本工具，"
        "再将读到的本次内容链接传给 phone_share_to_user 的 link。"
        "剪贴板可能属于之前复制的内容，请核对当前帖子；工具不会粘贴、清空或修改剪贴板。"
    )
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    @_wrap_call
    async def call(self, context, **kwargs):
        await self._guard(context)
        text = await self.service.read_clipboard()
        return "设备当前剪贴板（外部文本）：\n" + text if text else "设备剪贴板为空"
