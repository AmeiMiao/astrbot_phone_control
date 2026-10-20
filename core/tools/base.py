"""工具层公共基础设施：结果/异常辅助、权限校验、设备门禁与异常包裹。"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import mcp
from mcp.types import ContentBlock
from pydantic.dataclasses import dataclass
from astrbot.api import FunctionTool, logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..device_gate import ExecutionCancelled, GateError
from ..config import UNCONFIGURED_MESSAGE, PhoneConfig
from ..errors import AdbNotConfiguredError, PhoneControlError


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _result_text(result: Any) -> str:
    """把 ToolExecResult 转成可读文本（用于结果判定/追加提示）。"""
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts = []
        for item in content:
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
        if parts:
            return "\n".join(parts)
    return str(result)


def _error(exc: BaseException) -> str:
    """把异常转成模型可读的错误文本。"""
    message = str(exc).strip() or exc.__class__.__name__
    return f"error: {message}"


def _novelty_bored_note(match: Any) -> str:
    """把「已看腻」的新鲜感匹配结果转成给模型的简短提示；未到阈值返回空串。"""
    if match is None or not getattr(match, "bored", False):
        return ""
    entry = getattr(match, "entry", None)
    label = str(getattr(entry, "label", "") or "").strip()
    fatigue = float(getattr(match, "fatigue", 0.0) or 0.0)
    subject = f"「{label}」" if label else "这类内容"
    return (
        f"新鲜感提示：{subject}你已看腻（疲劳 {fatigue:.0f}/100）；"
        "之后降低同类内容的打开/停留概率，优先换主题或换 App。"
    )


def _event_of(context: ContextWrapper[AstrAgentContext]) -> Any:
    """从工具调用上下文里取出 AstrMessageEvent。"""
    agent_context = getattr(context, "context", None)
    event = getattr(agent_context, "event", None)
    if event is None:
        raise RuntimeError("工具调用缺少消息事件上下文")
    return event


def _is_cron_event(event: Any) -> bool:
    """判断是否为 cron 主动任务合成的消息事件。"""
    if type(event).__name__ == "CronMessageEvent":
        return True
    try:
        return str(event.get_platform_name() or "") == "cron"
    except Exception:  # noqa: BLE001 - 事件类型可能是鸭子类型
        return False


def _agent_run_active(event: Any) -> bool:
    """事件是否处于 AstrBot Agent run 中（由插件 on_agent_begin/on_llm_request 标记）。

    直接调用工具（测试/外部 API）没有该标记：工具作用域退出时立即释放设备租约，
    避免后续不同事件被上一次调用的租约挡住；Agent run 内则保持租约到 run 结束。
    """
    try:
        return bool(event.get_extra("phone_agent_run_active"))
    except Exception:  # noqa: BLE001 - 鸭子类型事件
        return False


def _sender_id_of(event: Any) -> str:
    try:
        return str(event.get_sender_id() or "").strip()
    except Exception:  # noqa: BLE001 - 某些平台事件不实现 get_sender_id
        return ""


def check_admin_permission(event: Any, config: PhoneConfig) -> str | None:
    """校验调用者是否有权使用手机能力。

    优先级：cron 主动任务放行 → ``security.owner_ids``（非空时只认主人，AstrBot
    管理员也不放行）→ 未启用 require_admin 放行 → admin/白名单放行。

    Args:
        event: 触发本次工具调用的消息事件。
        config: 当前配置快照。

    Returns:
        ``None`` 表示放行；否则返回以 ``error:`` 开头的拒绝文本。
    """
    security = config.security
    if _is_cron_event(event):
        return None
    sender_id = _sender_id_of(event)
    if security.owner_ids:
        if sender_id and sender_id in security.owner_ids:
            return None
        return (
            "error: 手机操控仅插件主人可用，请在插件配置 security.owner_ids 添加你的"
            f"用户 ID（你的 ID 是 {sender_id or '未知'}）"
        )
    if not security.require_admin:
        return None
    if str(getattr(event, "role", "member")) == "admin":
        return None
    if sender_id and sender_id in security.allowed_user_ids:
        return None
    return (
        "error: 权限不足，手机操控仅允许管理员或白名单用户使用。"
        f"你的用户 ID 是 {sender_id or '未知'}；"
        "可把该 ID 加入 AstrBot 管理员列表，或写入插件配置 security.allowed_user_ids"
        "（也可配置 security.owner_ids 指定插件主人）。"
    )


def _ok(payload: str) -> str:
    return payload if payload else "ok"


def _image_result(text: str, data: bytes | None, mime: str) -> ToolExecResult:
    """组装「文本 + 可选图片」工具结果；图片 MIME 以服务层返回为准。"""
    text = _ok(text)
    if not data:
        return text
    content: list[ContentBlock] = [
        mcp.types.TextContent(type="text", text=text),
        mcp.types.ImageContent(
            type="image",
            data=base64.b64encode(data).decode("ascii"),
            mimeType=str(mime or "").strip() or "image/png",
        ),
    ]
    return mcp.types.CallToolResult(content=content)


def _normalize_points(value: Any) -> list[dict[str, int]] | None:
    """把 points 归一化成 ``[{"x": int, "y": int}]``；空值返回 None。"""
    if not isinstance(value, (list, tuple)):
        return None
    points: list[dict[str, int]] = []
    for item in value:
        if isinstance(item, dict):
            points.append(
                {"x": _as_int(item.get("x"), 0), "y": _as_int(item.get("y"), 0)}
            )
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            points.append({"x": _as_int(item[0], 0), "y": _as_int(item[1], 0)})
    return points or None


@dataclass
class _PhoneToolBase(FunctionTool[AstrAgentContext]):
    """所有手机工具的公共基类。"""

    service: Any = None

    async def _guard(self, context: ContextWrapper[AstrAgentContext]) -> Any:
        """返回事件；依次校验权限、ADB 配置、token 额度与玩手机预算。"""
        event = _event_of(context)
        config = self.service.config
        denied = check_admin_permission(event, config)
        if denied:
            raise _PermissionDenied(denied)
        if not config.adb.configured:
            raise AdbNotConfiguredError(UNCONFIGURED_MESSAGE)
        quota_error = self.service.quota_error_text()
        if quota_error:
            raise _ToolNotice(quota_error)
        if self.service.should_yield_to_user(event):
            raise _ToolNotice(
                "用户任务优先：AI 自刷/随机/后台玩手机已暂停，先让用户任务使用手机；"
                "请立即收尾停下，用户任务结束后会自动恢复自刷。"
            )
        budget = self.service.ensure_play_budget(event)
        if budget is not None and budget.target_tokens > 0 and budget.completed:
            raise _ToolNotice(
                "本轮玩手机预算已完成，请立即收尾汇报（可 phone_share_to_user 一次）"
            )
        return event

    async def _guard_light(self, context: ContextWrapper[AstrAgentContext]) -> Any:
        """只校验权限与 ADB 配置（不拦额度/预算）。

        用于 phone_learn 这类「收尾时才调用」的元工具：预算完成时模型仍必须能
        记录本轮学到的内容，不能被额度/预算提示挡回去。
        """
        event = _event_of(context)
        config = self.service.config
        denied = check_admin_permission(event, config)
        if denied:
            raise _PermissionDenied(denied)
        if not config.adb.configured:
            raise AdbNotConfiguredError(UNCONFIGURED_MESSAGE)
        return event

    def _is_write_tool(self) -> bool:
        """该工具是否属于会改变设备状态的写操作（未知工具按写操作）。

        依据验证控制器登记的工具集合判断：验证工具与只读观察工具按读处理，
        其余按写处理，供统一设备门禁区分暂停/占用时的放行范围。
        """
        controller = getattr(self.service, "verification", None)
        if controller is None:
            return True
        verify_tools = getattr(controller, "_VERIFY_TOOLS", frozenset())
        read_only = getattr(controller, "_READ_ONLY_TOOLS", frozenset())
        return self.name not in verify_tools and self.name not in read_only

    # ---- 内容新鲜感/厌倦感 -------------------------------------------

    def _record_novelty(self, app: str, text: str, *, weight: float = 1.0) -> Any:
        """记录一次内容曝光；未启用/失败返回 None（不影响工具主流程）。"""
        record = getattr(self.service, "record_novelty", None)
        if not callable(record):
            return None
        try:
            return record(app or "", text or "", weight=weight)
        except Exception:  # noqa: BLE001 - 状态记录失败不影响工具结果
            return None

    def _novelty_bored_note(self, match: Any) -> str:
        """新鲜的「已看腻」提示文本。"""
        return _novelty_bored_note(match)


class _PermissionDenied(Exception):
    """内部信号：权限门禁拒绝（由 :func:`_wrap_call` 转成文本）。"""


class _ToolNotice(Exception):
    """内部信号：额度/预算等提示（原样返回文本，不加 error: 前缀）。"""


def _gate_error_text(message: str) -> str:
    """门禁拒绝文案统一成 ``error:`` 开头（门禁自身可能已带该前缀）。"""
    text = str(message or "").strip() or "当前不能操作手机"
    return text if text.startswith("error:") else f"error: {text}"


def _wrap_call(handler):
    """给工具实现统一套上设备执行作用域、权限与异常处理。

    设备作用域在进入时校验鉴权/占用/暂停/期限，退出只清理上下文；门禁不存在
    （离线单测未装配门禁）时跳过作用域，工具按原有逻辑执行。
    """

    async def wrapper(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        try:
            event = _event_of(context)
        except Exception:  # noqa: BLE001 - 缺少事件时交给 handler 统一报错
            event = None
        gate = getattr(self.service, "gate", None)
        try:
            if gate is not None and event is not None:
                with gate.scope(
                    event, purpose=self.name, write=self._is_write_tool()
                ):
                    result = await handler(self, context, **kwargs)
            else:
                result = await handler(self, context, **kwargs)
        except ExecutionCancelled as exc:
            # 暂停/取消/代次变化不是工具错误：要求模型立即收尾，不加 error: 前缀
            detail = str(getattr(exc, "message", "") or exc).strip()
            return f"已中止：{detail}" if detail else "已中止：手机执行已被取消"
        except GateError as exc:
            return _gate_error_text(getattr(exc, "message", "") or str(exc))
        except _ToolNotice as exc:
            return str(exc)
        except _PermissionDenied as exc:
            return str(exc)
        except PhoneControlError as exc:
            return _error(exc)
        except asyncio.TimeoutError:
            return "error: 操作超时，设备可能无响应"
        except Exception as exc:  # noqa: BLE001 - 工具不允许把异常抛给 Agent 循环
            logger.warning("手机工具 %s 执行失败：%s", self.name, exc)
            return _error(exc)
        finally:
            # Agent run 外的直接工具调用：作用域结束即释放租约，避免阻塞后续事件。
            if gate is not None and event is not None and not _agent_run_active(event):
                try:
                    gate.release_event(event)
                except Exception:  # noqa: BLE001 - 释放失败由 stale 回收兜底
                    pass
        # 额度接近上限时在结果尾部追加软告警（不改变工具结果本身）
        try:
            warning = self.service.quota_warning_text()
        except Exception:  # noqa: BLE001 - 告警失败不影响结果
            warning = None
        if warning:
            if isinstance(result, str):
                result = f"{result}\n\n{warning}"
            elif isinstance(result, mcp.types.CallToolResult):
                result.content.append(
                    mcp.types.TextContent(type="text", text=warning)
                )
        return result

    return wrapper
