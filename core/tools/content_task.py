"""内容任务工具：有序理解与独立兴趣决策。"""

import json
from pydantic import Field
from pydantic.dataclasses import dataclass
from .base import _PhoneToolBase, _wrap_call


@dataclass
class PhoneContentTool(_PhoneToolBase):
    name: str = "phone_content"
    description: str = (
        "单帖/视频任务：图文在打开详情前 begin(kind=post,content_id=内容ID)再打开详情并 read；视频先打开并确认起点，再 begin(kind=video,content_id=内容ID)。视频先试看8秒，续看前 decide(decision=continue,reason=原因)，再 read；"
        "自主规则弹性的自由浏览开启时可直接打开应用、看屏幕或 phone_watch_media 连续观看，本工具按需使用；"
        "comment=理解正文后对原帖发表自己的评论；read_comments=看评论；reply_comment(target=具体已读评论)=回复评论，三者分开判断；点赞like/收藏favorite可选，决定不执行操作，评论与回复另申请互动名额。skip停止；finish结束。切换内容先结束。status查看任务。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["begin", "read", "decide", "finish", "status"],
                },
                "kind": {"type": "string", "enum": ["post", "video"]},
                "content_id": {"type": "string"},
                "task_id": {"type": "string"},
                "question": {"type": "string"},
                "seconds": {"type": "number"},
                "decision": {
                    "type": "string",
                    "enum": [
                        "continue",
                        "read_comments",
                        "reply_comment",
                        "comment",
                        "like",
                        "favorite",
                        "skip",
                    ],
                },
                "reason": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["action"],
        }
    )

    @_wrap_call
    async def call(self, context, **kwargs):
        event = await (
            self._guard_light(context)
            if kwargs.get("action") in {"finish", "status"}
            else self._guard(context)
        )
        manager = self.service.contents
        action = kwargs.get("action")
        task_id = str(kwargs.get("task_id") or "")
        if action == "begin":
            task = await manager.begin(
                event,
                kind=kwargs.get("kind"),
                content_id=kwargs.get("content_id"),
                question=kwargs.get("question") or "",
            )
            result = {
                "task_id": task["id"],
                "note": "素材采集已就绪；请打开匹配内容，read 理解正文；begin 不代表已看过",
            }
        elif action == "read":
            result = await manager.read(event, task_id, kwargs.get("seconds") or 0)
        elif action == "decide":
            result = await manager.decide(
                event,
                kwargs.get("decision"),
                kwargs.get("reason") or "",
                task_id,
                kwargs.get("target") or "",
            )
        elif action == "finish":
            manager.current(event, task_id)
            await manager.finish_owner(manager.owner(event), abnormal=False)
            result = {"status": "completed"}
        elif action == "status":
            result = manager.current(event, task_id)
            result = {
                k: result[k]
                for k in (
                    "id",
                    "kind",
                    "content_id",
                    "cursor",
                    "status",
                    "summaries",
                    "decisions",
                )
            }
        else:
            raise ValueError("未知内容任务 action")
        return json.dumps(result, ensure_ascii=False)
