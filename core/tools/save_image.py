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
    description: str = "按语义查询已存图片/表情包，storage 可筛选服务端或手机本地。发帖/回复先判断是否需要配图，仅相关时 search 并阅读描述，申请 phone_interaction_gate 后 prepare（image_id、kind、reason）准备相册图片，再在 App 选图核对预览。prepare 不发布、不消耗预约；无合适图片用文字。to_phone 单独准备图片。需开启图片保存，原图与设备哈希核验。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "to_phone", "prepare"],
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
                "storage": {
                    "type": "string", "enum": ["auto", "all", "server", "phone"],
                    "default": "auto", "description": "server=服务器原图；phone=手机本地；auto/all=搜索全部、准备时自动选择。",
                },
                "kind": {"type": "string", "enum": ["post", "comment", "passive_reply"]},
                "reason": {"type": "string", "description": "prepare 必填：图片内容与本次发帖/回复语义的关系，无关时不要配图。"},
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
        storage = str(kwargs.get("storage") or "auto")
        if action == "search":
            await self._guard_light(context)
            result = image_library(self.service).find(
                kwargs.get("query") or "", kwargs.get("limit") or 10,
                "all" if storage == "auto" else storage,
            )
        elif action == "to_phone":
            await self._guard(context)
            result = await image_library(self.service).send_to_phone(
                str(kwargs.get("image_id") or ""), "auto" if storage == "all" else storage
            )
        elif action == "prepare":
            event = await self._guard(context)
            result = await image_library(self.service).prepare_for_interaction(
                event, str(kwargs.get("image_id") or ""),
                kind=str(kwargs.get("kind") or ""), reason=kwargs.get("reason") or "",
                storage="auto" if storage == "all" else storage,
            )
        else:
            raise ValueError("action 需为 search、to_phone 或 prepare")
        return json.dumps(result, ensure_ascii=False)
