"""按兴趣保存当前图文任务的原图。"""

import json
from pydantic import Field
from pydantic.dataclasses import dataclass
from .base import _PhoneToolBase, _wrap_call
from ..saved_images import save_content_image


@dataclass
class PhoneSaveImageTool(_PhoneToolBase):
    name: str = "phone_save_image"
    description: str = "喜欢当前图文的图片或表情包时保存原图到手机相册；需开启 browse.auto_save_images，先 phone_content read，再选 image_index 和 reason。保存不等于分享。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "当前图文任务编号。"},
                "image_index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "已关联原图编号，从 0 开始。",
                },
                "reason": {
                    "type": "string",
                    "description": "喜欢这张图片或表情包的具体理由。",
                },
            },
            "required": ["image_index", "reason"],
        }
    )

    @_wrap_call
    async def call(self, context, **kwargs):
        event = await self._guard(context)
        result = await save_content_image(
            self.service,
            event,
            task_id=str(kwargs.get("task_id") or ""),
            index=int(kwargs.get("image_index", 0)),
            reason=str(kwargs.get("reason") or ""),
        )
        return json.dumps(result, ensure_ascii=False)
