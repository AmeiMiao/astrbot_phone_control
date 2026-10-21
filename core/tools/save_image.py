"""按兴趣保存当前图文任务的原图。"""

import json
from pydantic import Field
from pydantic.dataclasses import dataclass
from .base import _PhoneToolBase, _wrap_call
from ..saved_images import save_content_image, image_library


@dataclass
class PhoneSaveImageTool(_PhoneToolBase):
    name: str = "phone_save_image"
    description: str = "按兴趣保存当前图文原图/表情包；需开启保存图片。填写 description 图片内容与 reason，按配置存服务端/手机/两地，自动记录来源、图片编号和路径；可后续查询或传手机。"
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
                "description": {
                    "type": "string",
                    "description": "图片或表情包的实际内容描述，便于以后查找引用。",
                },
                "kind": {
                    "type": "string",
                    "enum": ["image", "sticker"],
                    "default": "image",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "图片主题/表情含义标签。",
                },
                "reason": {
                    "type": "string",
                    "description": "喜欢这张图片或表情包的具体理由。",
                },
            },
            "required": ["image_index", "description", "reason"],
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
            description=str(kwargs.get("description") or ""),
            kind=str(kwargs.get("kind") or "image"),
            tags=kwargs.get("tags") or [],
        )
        return json.dumps(result, ensure_ascii=False)


@dataclass
class PhoneSavedImagesTool(_PhoneToolBase):
    name: str = "phone_saved_images"
    description: str = "查询已存图片/表情包的描述、标签、来源和路径；action=to_phone 可用图片编号把服务端原图传到手机相册，哈希与索引核验。图片库独立于内容任务保存。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "to_phone"],
                    "default": "search",
                },
                "query": {
                    "type": "string",
                    "description": "图片描述/标签/来源/编号，空为最近图片。",
                },
                "image_id": {
                    "type": "string",
                    "description": "查询结果中的完整图片编号。",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 30,
                },
            },
            "required": [],
        }
    )

    @_wrap_call
    async def call(self, context, **kwargs):
        action = str(kwargs.get("action") or "search")
        if action == "search":
            await self._guard_light(context)
            result = image_library(self.service).find(
                kwargs.get("query") or "", kwargs.get("limit") or 10
            )
        elif action == "to_phone":
            await self._guard(context)
            result = await image_library(self.service).send_to_phone(
                str(kwargs.get("image_id") or "")
            )
        else:
            raise ValueError("action 需为 search 或 to_phone")
        return json.dumps(result, ensure_ascii=False)
