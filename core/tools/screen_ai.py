"""实时屏幕与内容识别工具：看实时帧、OCR 读屏、流媒体内容识别。"""

from __future__ import annotations


import asyncio
import base64

import mcp
from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .base import _PhoneToolBase, _as_float, _as_int, _ok, _wrap_call
from .perception import (
    DEFAULT_OCR_PROMPT,
    _frame_mime,
    _live_frame,
    _record_internal_usage,
    _resolve_ocr_provider,
)


@dataclass
class PhoneLiveScreenTool(_PhoneToolBase):
    """返回实时屏幕最新帧（可回传图片给模型）。"""

    name: str = "phone_live_screen"
    description: str = (
        "查看手机实时屏幕最新一帧：返回帧年龄、前台 App/Activity 与截图方式，"
        "include_image=true（默认）时把画面以图片回传给模型。用于需要看当前画面、"
        "验证码/时效页面、确认操作结果等场景；页面内容识别优先 phone_screen（抓包可用时"
        "会附带当前页面接口内容）。wait_fresh_ms>0 时确保返回不超过该年龄的帧"
        "（过期会立即抓一张）。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "wait_fresh_ms": {
                    "type": "integer",
                    "description": "要求帧年龄不超过该毫秒数；0 表示取缓存最新帧，过期则立即抓一张。",
                    "default": 0,
                },
                "include_image": {
                    "type": "boolean",
                    "description": "是否把最新帧作为图片回传给模型，默认 true。",
                    "default": True,
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard(context)
        wait_ms = max(0, _as_int(kwargs.get("wait_fresh_ms"), 0))
        include_image = bool(kwargs.get("include_image", True))
        frame = await _live_frame(self.service, wait_ms)
        text = (
            f"实时屏幕：age={frame.age_ms()}ms，"
            f"前台={frame.foreground or '未知'}/"
            f"{frame.activity or '未知'}，截图 method={frame.method}"
        )
        if not include_image:
            return _ok(text)
        encoded = base64.b64encode(frame.data).decode("ascii")
        return mcp.types.CallToolResult(
            content=[
                mcp.types.TextContent(type="text", text=text),
                mcp.types.ImageContent(
                    type="image", data=encoded, mimeType=_frame_mime(frame)
                ),
            ]
        )


@dataclass
class PhoneReadScreenTool(_PhoneToolBase):
    """用 OCR/图片理解 Provider 识别实时屏幕文字。"""

    name: str = "phone_read_screen"
    description: str = (
        "识别手机实时屏幕上的文字/数字/验证码：取最新帧后交给 OCR/图片理解 Provider，"
        "返回识别文本、帧年龄与前台页面。prompt 可自定义识别要求（默认「逐字识别文字/"
        "数字/验证码，只输出结果」）。适合验证码、时效页面：先本工具或 phone_live_screen"
        " 拿最新结果，立即用 phone_input_text 输入。页面内容识别优先 phone_screen"
        "（抓包可用时会附带当前页面接口内容），本工具只读屏上文字。provider 未配置/"
        "调用失败会返回明确 error 与建议。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "识别要求；留空用默认 OCR 提示词。",
                    "default": "",
                },
                "wait_fresh_ms": {
                    "type": "integer",
                    "description": "要求帧年龄不超过该毫秒数（0 表示最新帧，过期立即抓一张）。",
                    "default": 0,
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = await self._guard(context)
        wait_ms = max(0, _as_int(kwargs.get("wait_fresh_ms"), 0))
        prompt = str(kwargs.get("prompt") or "").strip() or DEFAULT_OCR_PROMPT
        frame = await _live_frame(self.service, wait_ms)
        app_context = getattr(self.service, "app_context", None)
        if app_context is None:
            return (
                "error: 缺少 AstrBot 上下文，无法调用 OCR Provider；可改用 "
                "phone_live_screen 把最新帧回传给主模型识别，或在插件配置 "
                "perception.ocr_provider_id / AstrBot「图片描述 Provider」中配置"
            )
        provider_id, _source = _resolve_ocr_provider(
            self.service, app_context, event
        )
        if not provider_id:
            return (
                "error: 未找到可用的 OCR/图片理解 Provider：请在 AstrBot 配置"
                "「图片描述 Provider」（provider_settings.default_image_caption_"
                "provider_id）或插件 perception.ocr_provider_id 中指定；也可以改用 "
                "phone_live_screen 把最新帧回传给主模型看"
            )
        timeout_s = getattr(
            getattr(self.service.config, "perception", None),
            "ocr_timeout_seconds",
            30,
        )
        try:
            response = await asyncio.wait_for(
                app_context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    image_urls=[str(frame.path)],
                ),
                timeout=max(5.0, float(timeout_s)),
            )
        except asyncio.TimeoutError:
            return (
                f"error: 屏幕识别超时（>{timeout_s}s，provider={provider_id}）；"
                "可改用 phone_live_screen 把最新帧回传给主模型识别，或调大插件配置 "
                "perception.ocr_timeout_seconds / 检查 OCR Provider 是否支持图片输入"
            )
        except Exception as exc:  # noqa: BLE001 - OCR 失败给可操作建议
            return (
                f"error: 屏幕识别调用失败（provider={provider_id}）：{exc}；"
                "可改用 phone_live_screen 把最新帧回传给主模型识别，或检查 AstrBot"
                "「图片描述 Provider」/插件 perception.ocr_provider_id 配置是否可用"
            )
        _record_text = str(getattr(response, "completion_text", "") or "").strip()
        _record_internal_usage(
            self.service,
            response,
            event,
            "internal:read_screen",
            prompt,
            _record_text,
            1,
        )
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            result_chain = getattr(response, "result_chain", None)
            if result_chain is not None:
                try:
                    text = str(result_chain.get_plain_text() or "").strip()
                except Exception:  # noqa: BLE001
                    text = ""
        if not text:
            return (
                f"error: 屏幕识别没有返回文本（provider={provider_id}）；"
                "可用 phone_live_screen 回传图片给主模型，或检查 Provider 是否支持图片输入"
            )
        return _ok(
            f"屏幕识别结果（provider={provider_id}，age={frame.age_ms()}ms，"
            f"前台={frame.foreground or '未知'}/{frame.activity or '未知'}）：\n{text}"
        )


# 流媒体内容识别（设备播放声音 + 关键画面）
@dataclass
class PhoneWatchMediaTool(_PhoneToolBase):
    """识别手机当前正在播放的视频/直播/音乐内容。"""

    name: str = "phone_watch_media"
    description: str = (
        "识别手机当前正在播放的视频/直播/音乐内容：配置视频模型时回传带原音轨的分段视频；其余方式录制声音并转写为文字，"
        "同时在录制期间抓关键画面交给视觉模型描述，返回『语音转写 + 画面描述』。"
        "标题/评论/弹幕不足以判断内容，或刷视频、看直播需要真正理解时使用；"
        "首次试看取 media.preview_seconds，后续默认需 phone_content decide continue；auto.break_rule 开启时可直接重复调用连续分段观看；可传 seconds。"
        "只录设备播放声音（output），无音频/DRM 内容时只返回画面描述。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": (
                        "录制时长秒；0 或不传时取配置 media.seconds（默认 20），"
                        "上限为 media.max_seconds"
                    ),
                    "default": 0,
                },
                "question": {
                    "type": "string",
                    "description": (
                        "想重点关注的问题（可选），如『讲了什么』『画面里有什么人物动作』"
                    ),
                    "default": "",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "audio", "frames", "both"],
                    "description": (
                        "识别方式：auto=按配置（默认声音+画面）；audio=只理解声音；"
                        "frames=只看画面；both=两者都做"
                    ),
                    "default": "auto",
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = await self._guard(context)
        seconds = _as_float(kwargs.get("seconds"), 0)
        question = str(kwargs.get("question") or "").strip()
        mode = str(kwargs.get("mode") or "auto").strip()
        return _ok(await self.service.watch_media(event,seconds=seconds,question=question,mode=mode))
