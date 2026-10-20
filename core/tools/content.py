"""内容浏览与记忆工具：浏览信息流、记录学习档案、检索浏览日记、查看最近聊天、App 使用经验记忆。"""

from __future__ import annotations

import base64
import time
from pathlib import Path

import mcp
from mcp.types import ContentBlock
from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..memory_sync import (
    build_app_experience_payload,
    build_learning_payload,
    wants_companion,
    wants_livingmemory,
)

from .base import _PhoneToolBase, _as_int, _error, _event_of, _ok, _wrap_call


@dataclass
class PhoneBrowseTool(_PhoneToolBase):
    """浏览信息流并记入记忆。"""

    name: str = "phone_browse"
    description: str = (
        "便捷快速采样：自动浏览任意已安装 App 的信息流/内容列表，抽取若干条内容并记入"
        "浏览日记。app 可传中文标签（如 微信、小红书）或 Android 包名；没有预置画像的 App "
        "走通用文本抽取兜底。它是省事的采样工具，不是必经流程——更自主的浏览请直接用 "
        "phone_screen 观察、再用 phone_swipe/phone_tap/phone_wait 自己决定看什么、停留多久、"
        "是否点开详情或评论。items 为采样条数；remember 为 true 时写入浏览记忆。"
        "完成后返回每条内容的标题/作者摘要。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "App 名称（douyin/bilibili/xiaoheihe）或包名。",
                },
                "items": {
                    "type": "integer",
                    "description": "采集条数，默认 5。",
                    "default": 5,
                },
                "include_screenshot": {
                    "type": "boolean",
                    "description": "是否把首条内容的截图回传给模型。",
                    "default": False,
                },
                "remember": {
                    "type": "boolean",
                    "description": "是否写入浏览日记（长期记忆）。",
                    "default": True,
                },
            },
            "required": ["app"],
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard(context)
        app = str(kwargs.get("app") or "").strip()
        if not app:
            return (
                "error: app 不能为空；可先调用 phone_list_apps 查看已安装 App，"
                "或用中文名/包名指定"
            )
        items = _as_int(kwargs.get("items"), self.service.config.browse.default_items)
        remember = bool(kwargs.get("remember", True))
        include_screenshot = bool(kwargs.get("include_screenshot", False))
        result = await self.service.browse(
            app,
            items=items,
            collect_screenshots=include_screenshot,
            remember=remember,
        )
        text = result.summary_text()
        # 新鲜感/厌倦感：PhoneService.browse 已按主题记录曝光并回传「已看腻」标签，
        # 这里只需把结果提示给模型，要求降低同类内容的打开/停留概率。
        bored_labels = list(getattr(result, "novelty_labels", []) or [])
        if bored_labels:
            text += (
                f"\n新鲜感：本次 {len(bored_labels)} 个主题你已看腻（"
                + "、".join(f"「{label}」" for label in bored_labels[:3])
                + "）；下次优先换主题或换 App，降低同类内容的打开/停留概率。"
            )
        if remember:
            text += "\n" + ("已写入浏览日记" if result.diary_recorded else "浏览日记未写入")
        screenshot_path = next(
            (item.screenshot_path for item in result.items if item.screenshot_path),
            None,
        )
        if include_screenshot and screenshot_path:
            try:
                data = Path(screenshot_path).read_bytes()
            except OSError as exc:
                return f"{text}\n（截图读取失败：{exc}）"
            content: list[ContentBlock] = [
                mcp.types.TextContent(type="text", text=text),
                mcp.types.ImageContent(
                    type="image",
                    data=base64.b64encode(data).decode("ascii"),
                    mimeType="image/png",
                ),
            ]
            return mcp.types.CallToolResult(content=content)
        return text


@dataclass
class PhoneLearnTool(_PhoneToolBase):
    """记录持续刷/自主学习时真正理解到的内容（写入学习档案）。"""

    name: str = "phone_learn"
    description: str = (
        "记录持续刷/自主学习时你真正理解到的内容（长期学习档案，供之后回顾与分享）。"
        "content 用 1-500 字写清「你实际理解了什么」：观点/知识点/梗/事件/社区规则等，"
        "而不是只抄标题或流水账；source 可写来源 App/页面/视频标题，tags 为关键词标签。"
        "持续刷/自主浏览时每轮结束前至少调用一次，确认自己真的看懂了内容再记；"
        "不要只滑不看，也不要等刷完再凭印象编造。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "真正理解到的内容（1-500 字）：观点/知识点/梗/事件/社区规则。",
                },
                "source": {
                    "type": "string",
                    "description": "来源（App/页面/视频标题），可留空。",
                    "default": "",
                },
                "tags": {
                    "type": "array",
                    "description": "关键词标签列表，如 科技、梗、生活技巧。",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
            "required": ["content"],
        }
    )

    @_wrap_call
    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        await self._guard_light(context)
        content = str(kwargs.get("content") or "").strip()
        if not content:
            return "error: content 不能为空，请写清这次真正理解到的内容（1-500 字）"
        if len(content) > 500:
            return f"error: content 过长（{len(content)} 字），请压缩到 500 字以内"
        source = str(kwargs.get("source") or "").strip()
        raw_tags = kwargs.get("tags")
        tags = (
            [str(tag).strip() for tag in raw_tags if str(tag).strip()]
            if isinstance(raw_tags, (list, tuple))
            else []
        )
        event = _event_of(context)
        backend = self.service.config.memory.backend
        remote_enabled = wants_companion(backend) or wants_livingmemory(backend)
        companion_payload = (
            build_learning_payload(content=content, source=source, tags=tags)
            if remote_enabled
            else {}
        )
        text = await self.service.learn(content, source=source, tags=tags)
        if companion_payload:
            ok, detail = await self.service.sync_memory(event, companion_payload)
            if ok:
                text += f"；已同步到记忆插件（{detail}）"
            else:
                text += f"；记忆插件同步失败（{detail or '未知原因'}），已保存在插件本地"
        # 标记本轮确实记录了学习内容，供插件判断持续刷是否「有看有记」；
        # 打标失败不影响工具返回。
        try:
            event.set_extra("phone_learned", True)
        except Exception:  # noqa: BLE001 - 事件不支持 extra 时忽略
            pass
        # 深度消费的内容也算同类曝光（权重更高）：同类内容反复学习会累计疲劳度。
        # 只用 content 参与主题匹配：source（常是 App 名）和 tags 拼进去会把无关内容
        # 强行拉到同一主题（同 App 名称词元被反复分享）；tags 已在学习档案里记录。
        match = self._record_novelty(source, content, weight=1.5)
        bored_note = self._novelty_bored_note(match)
        if bored_note:
            text += f"；{bored_note}"
        return _ok(text)


@dataclass
class PhoneRecallDiaryTool(_PhoneToolBase):
    """检索浏览日记。"""

    name: str = "phone_recall_diary"
    description: str = (
        "检索手机浏览日记（浏览过什么内容、标题/作者/时间）。"
        "query 为空时返回最近若干条记录。用于回答“你最近看了什么”这类问题。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词；空字符串表示看最近记录。",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 5。",
                    "default": 5,
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard_light(context)
        query = str(kwargs.get("query") or "").strip()
        limit = _as_int(kwargs.get("limit"), 5)
        return _ok(await self.service.recall(query, limit=limit))


@dataclass
class PhoneRecentChatTool(_PhoneToolBase):
    """查看最近聊天记录，按群/人的喜好分享内容。"""

    name: str = "phone_recent_chat"
    description: str = (
        "查看最近聊天记录（平时的私聊/群聊消息），了解用户/群最近在聊什么、喜欢什么。"
        "返回当前会话最近消息，以及**可主动分享的会话列表**：群聊会列出最近聊天内容，"
        "私聊只列最近有交流的用户；同时显示当前是否处于分享时段（默认 08:00–22:00 中国"
        "时区）。自刷/随机/持续刷要分享时，从列表里自己选合适的群/人传给 "
        "phone_share_to_user 的 target；分享前必须先本工具看聊天记忆，且分享必须带"
        "链接或截图（禁止纯文字），同一内容只发一次、别刷屏。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "每个会话返回的最近消息条数，默认 15。",
                    "default": 15,
                },
                "umo": {
                    "type": "string",
                    "description": "要查看的会话 UMO；留空表示当前会话。",
                    "default": "",
                },
            },
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        event = await self._guard_light(context)
        # 分享目标必须依据聊天记忆：成功调用本工具后打标，供 phone_share_to_user
        # 在自刷/随机/持续刷任务里校验「先看聊天再选目标」。
        try:
            event.set_extra("phone_chat_memory_checked", True)
        except Exception:  # noqa: BLE001 - 事件不支持 extra 时忽略
            pass
        limit = max(1, min(_as_int(kwargs.get("limit"), 15), 50))
        umo = str(kwargs.get("umo") or "").strip() or str(
            getattr(event, "unified_msg_origin", "") or ""
        )
        memory = self.service.chat_memory
        messages = memory.recent(umo, limit=limit)
        sessions = self.service.shareable_sessions(limit=20)
        allowed, window = self.service.share_allowed_now()
        lines = [f"会话 {umo or '（未知）'} 最近 {len(messages)} 条消息："]
        for entry in messages:
            when = time.strftime(
                "%m-%d %H:%M",
                time.localtime(float(entry.get("ts") or 0)),
            )
            sender = str(entry.get("sender_name") or entry.get("sender_id") or "未知")
            kind = "群" if entry.get("is_group") else "私聊"
            lines.append(f"[{when}][{kind}] {sender}：{entry.get('text') or ''}")
        if not messages:
            lines.append("（该会话还没有记录到聊天消息）")
        lines.append("")
        lines.append(
            f"当前分享时段：{window}（{'允许主动分享' if allowed else '免打扰，自刷任务不要主动分享'}）"
        )
        lines.append("")
        lines.append(
            "可主动分享的会话（群聊 + 最近有交流的私聊；把 umo 传给 "
            "phone_share_to_user 的 target）："
        )
        for entry in sessions:
            when = time.strftime(
                "%m-%d %H:%M",
                time.localtime(float(entry.get("last_ts") or 0)),
            )
            kind = "群" if entry.get("kind") == "group" else "私聊"
            summary = str(entry.get("last_text") or "").replace("\n", " ")[:60]
            sender = str(entry.get("sender_name") or "").strip()
            who = f"，{sender}" if sender else ""
            lines.append(
                f"- {entry.get('umo')}（{kind}，最后活跃 {when}，共 "
                f"{entry.get('count', 1)} 条{who}）：{summary}"
            )
        if not sessions:
            lines.append(
                "（还没有记录到可分享的聊天会话；私聊只列最近有交流的用户）"
            )
        lines.append("")
        lines.append(
            "根据内容和对方最近聊的话题/喜好自己判断要不要分享、分享到哪个群或人；"
            "先决定看不看，看完再单独决定分不分享；私聊只选最近有交流的用户，"
            "不要用固定目标；分享必须带链接或截图（禁止纯文字），"
            "同一内容只发一次、短时间别刷屏（默认静默）。"
        )
        return _ok("\n".join(lines))


@dataclass
class PhoneAppMemoryTool(_PhoneToolBase):
    """查询 AI 记住的 App 布局/入口/操作经验。"""

    name: str = "phone_app_memory"
    description: str = (
        "查询某个 App 的使用经验（布局、按钮位置、进入某页面的入口与操作步骤）。"
        "操作不熟悉的 App、或准备完成点赞/评论/发帖/发布等固定入口任务前先调用本工具，"
        "按记住的经验操作；页面与经验不符说明 App 已改版，先重新探索再用 "
        "phone_app_remember 更新。page 可选（如 首页/发布/评论区），留空返回该 App 全部经验。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "App 名称（中文名/别名/包名，如 B站、哔哩哔哩、tv.danmaku.bili）。",
                },
                "page": {
                    "type": "string",
                    "description": "页面/场景关键词（如 首页、发布、评论区）；留空返回全部。",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回几条经验，默认 5。",
                    "default": 5,
                },
            },
            "required": ["app"],
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard_light(context)
        app = str(kwargs.get("app") or "").strip()
        if not app:
            return "error: app 不能为空（填中文名/别名/包名）"
        page = str(kwargs.get("page") or "").strip()
        limit = max(1, min(_as_int(kwargs.get("limit"), 5), 20))
        store = getattr(self.service, "app_memory", None)
        if store is None:
            return "error: App 记忆未启用"
        try:
            entries = store.lookup(app, page=page, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 记忆读取失败给可读错误
            return _error(exc)
        if not entries:
            return (
                f"暂无「{app}」的使用经验。先按当前屏幕探索操作；"
                "熟悉后用 phone_app_remember 记录布局/入口/操作步骤，下次可直接复用。"
            )
        lines = [f"「{app}」使用经验（{len(entries)} 条，按经验操作并以当前屏幕为准）："]
        for entry in entries:
            page_text = entry.page or "通用"
            when = time.strftime("%m-%d %H:%M", time.localtime(float(entry.updated_at or 0)))
            lines.append(
                f"- [{page_text}]（{when}，用过 {entry.hits} 次）：{entry.note}"
            )
            if entry.steps:
                lines.append("  步骤：" + (" → ".join(entry.steps)))
        lines.append(
            "提示：按经验查找/操作；若界面与经验不符（App 已改版），先重新探索，"
            "再用 phone_app_remember 覆盖更新该页面经验。"
        )
        return _ok("\n".join(lines))


@dataclass
class PhoneAppRememberTool(_PhoneToolBase):
    """记录/更新 App 布局与操作经验。"""

    name: str = "phone_app_remember"
    description: str = (
        "记住一个 App 的布局与使用方式，供以后复用：note 写关键布局/入口/按钮位置，"
        "steps 写操作步骤（可省略）。同一 App + page 再次记录会覆盖更新为更高版本；"
        "App 改版导致旧经验失效时，重新探索后用本工具覆盖记录即可。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "App 名称（中文名/别名/包名，如 B站、哔哩哔哩）。",
                },
                "note": {
                    "type": "string",
                    "description": "要记住的布局/入口/操作经验（1-500 字），写可复用的关键信息。",
                },
                "page": {
                    "type": "string",
                    "description": "页面/场景（如 首页、发布、评论区、设置）；留空=通用经验。",
                    "default": "",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选：按顺序的操作步骤（每步一句话，最多 20 步）。",
                    "default": [],
                },
            },
            "required": ["app", "note"],
        }
    )

    @_wrap_call
    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        await self._guard_light(context)
        app = str(kwargs.get("app") or "").strip()
        if not app:
            return "error: app 不能为空（填中文名/别名/包名）"
        store = getattr(self.service, "app_memory", None)
        if store is None:
            return "error: App 记忆未启用"
        try:
            entry = store.remember(
                app=app,
                note=kwargs.get("note"),
                page=kwargs.get("page") or "",
                steps=kwargs.get("steps"),
            )
        except Exception as exc:  # noqa: BLE001 - 参数/存储异常转可读错误
            return _error(exc)
        text = (
            f"已记住「{entry.label}」{('（' + entry.page + '）') if entry.page else ''}经验"
            f"（第 {entry.version} 版，当前档案 {store.stats().get('entries', 0)} 条）；"
            "下次操作该 App 前可 phone_app_memory 查询复用。"
        )
        event = _event_of(context)
        backend = self.service.config.memory.backend
        if wants_companion(backend) or wants_livingmemory(backend):
            payload = build_app_experience_payload(entry)
            ok, detail = await self.service.sync_memory(event, payload)
            if ok:
                text += f"；已同步到记忆插件（{detail}）"
            else:
                text += f"；记忆插件同步失败（{detail or '未知原因'}），已保存在插件本地"
        return _ok(text)
