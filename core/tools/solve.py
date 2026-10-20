"""验证码/做题自动处理工具：两阶段识图 + 自动执行。"""

from __future__ import annotations

import asyncio
import contextlib
import math
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from .. import verification as verification_mod
from ..device_gate import GateError
from ..errors import PhoneControlError

from .base import _PhoneToolBase, _as_int, _event_of, _image_result, _wrap_call
from .perception import _resolve_ocr_provider
from .verification_tools import (
    _SOLVE_FIRST_PROMPT,
    _SolveRun,
    _VERIFY_MODES,
    _append_text,
    _as_bool,
    _attempt_remaining,
    _capture_mime,
    _clean_solve_text,
    _normalize_captcha_type,
    _normalize_confirm,
    _normalize_verify_ops,
    _outcome_summary,
    _solve_second_prompt,
    _verify_timeout_seconds,
    _vision_json,
)


@dataclass
class PhoneSolveVerificationTool(_PhoneToolBase):
    """两阶段识图解决验证码/做题页面并自动执行。"""

    name: str = "phone_solve_verification"
    description: str = (
        "自动处理验证码/人机验证/做题测试页面：一次调用内部两次视觉模型——第一次快速截图"
        "识别图片内容并判断是否人机验证（滑块 slider/顺序点击 sequence/识图点选 "
        "image_click/拼图 puzzle）或答题测试（quiz）；第二次重新截图让视觉模型给出操作"
        "顺序（tap/drag）与最终确认点 confirm，然后自动执行并再截图返回结果。适合滑块、"
        "顺序点击、识图/拼图验证与答题/考试/测试页。执行后会读一次界面做结构化判定"
        "（已解除/答题进度/未通过/无法确认）并给出尝试与失败计数；按钮点击后页面还在变化"
        "时先 phone_wait/phone_screen 观察，不要连续重复求解。未检测到验证/答题时返回内容"
        "摘要；模型失败或需要手动精细控制时回退 phone_bypass_verify（capture 后 "
        "drag/tap_points）。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["auto", "captcha", "quiz"],
                    "description": (
                        "auto（默认，按识图结果自动判断）/ captcha（强制按人机验证处理）/ "
                        "quiz（强制按答题/测试处理）。"
                    ),
                    "default": "auto",
                },
                "include_image": {
                    "type": "boolean",
                    "description": "是否把执行后的新截图回传给模型，默认 true。",
                    "default": True,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        "每次视觉模型调用的超时秒数；0 表示取配置 "
                        "perception.verify_timeout_seconds（再回退 ocr_timeout_seconds，"
                        "默认 30），并会被单次尝试剩余时间进一步压缩。"
                    ),
                    "default": 0,
                },
            },
        }
    )

    @_wrap_call
    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        # 预算完成/额度临界时也要能救场把验证码解决掉，所以用轻量门禁。
        event = await self._guard_light(context)
        controller = getattr(self.service, "verification", None)
        if controller is None:
            run = await self._solve_impl(context, **kwargs)
            return run.result
        allowed, reason = await controller.reserve_attempt(event, kind="solver")
        if not allowed:
            return reason
        generation = _as_int(getattr(controller, "cancel_generation", 0), 0)
        run = await self._solve_racing_cancel(context, controller, generation, **kwargs)
        if run is None:
            return "验证暂停，已中止在途求解（本次结果未提交，请等待用户处理）"
        result = run.result
        if run.outcome is not None:
            notice = await self._submit_outcome(event, controller, run.outcome)
            result = _append_text(result, notice)
            result = _append_text(result, self._health_reason(controller))
            result = _append_text(result, _outcome_summary(controller, run.outcome))
            if getattr(controller, "paused", False):
                result = _append_text(result, self._pause_status(controller))
        return result

    async def _submit_outcome(
        self, event: Any, controller: Any, outcome: Any
    ) -> str:
        """提交结构化判定；仅在提交本身异常时返回可读提示。"""
        try:
            await controller.submit_outcome(event, outcome)
        except Exception as exc:  # noqa: BLE001 - 工具层不向上抛状态提交异常
            logger.warning("验证判定提交失败：%s", exc)
            return f"验证判定提交失败：{exc}"
        return ""

    @staticmethod
    def _health_reason(controller: Any) -> str:
        """控制器持久化故障文案；健康时返回空串。"""
        health_fn = getattr(controller, "health_reason", None)
        if not callable(health_fn):
            return ""
        try:
            return str(health_fn() or "").strip()
        except Exception:  # noqa: BLE001 - 健康状态读取失败不影响结果
            return ""

    @staticmethod
    def _pause_status(controller: Any) -> str:
        """暂停后的会话状态文案；控制器不提供时返回空串。"""
        status_fn = getattr(controller, "status_text", None)
        if not callable(status_fn):
            return ""
        try:
            return str(status_fn() or "").strip()
        except Exception:  # noqa: BLE001 - 状态文案失败不影响工具结果
            return ""

    def _failed_run(
        self,
        result: ToolExecResult,
        evidence: str,
        generation: int,
        structured: bool,
    ) -> _SolveRun:
        """动作执行前失败：按「无法确认」交给控制器累计失败次数。"""
        outcome = None
        if structured:
            outcome = verification_mod.judge_solve_outcome(
                kind=verification_mod.SOLVE_KIND_QUIZ,
                pre_text="",
                post_text=_clean_solve_text(evidence, 120),
                observation_ok=False,
                generation=generation,
            )
        return _SolveRun(result=result, outcome=outcome)

    def _vision_timeout(
        self, controller: Any, requested: Any, structured: bool
    ) -> tuple[float, float]:
        """本次视觉调用超时：配置值与尝试剩余时间取小；返回（超时秒, 剩余秒）。"""
        base = _verify_timeout_seconds(self.service, requested)
        remaining = _attempt_remaining(controller) if structured else math.inf
        return min(base, remaining), remaining

    async def _solve_racing_cancel(
        self,
        context: ContextWrapper[AstrAgentContext],
        controller: Any,
        generation: int,
        **kwargs,
    ) -> _SolveRun | None:
        """求解与「验证暂停/取消」事件竞速；暂停先到则取消求解并返回 None。"""
        solve_task = asyncio.create_task(
            self._solve_impl(context, generation=generation, **kwargs)
        )
        cancel_task: asyncio.Task | None = None
        cancelled_fn = getattr(controller, "cancelled", None)
        if callable(cancelled_fn):
            try:
                cancelled_event = cancelled_fn()
                if hasattr(cancelled_event, "wait"):
                    cancel_task = asyncio.create_task(cancelled_event.wait())
            except Exception:  # noqa: BLE001 - 取消事件不可用时退化为串行等待
                logger.warning("验证取消事件不可用，求解改为串行等待")
                cancel_task = None
        if cancel_task is None:
            return await solve_task
        try:
            done, _pending = await asyncio.wait(
                {solve_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            solve_task.cancel()
            cancel_task.cancel()
            raise
        if solve_task in done:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
            return await solve_task
        # 暂停/取消先到：中止在途求解，不提交本次判定（代次已变化）
        solve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await solve_task
        return None

    async def _solve_impl(
        self,
        context: ContextWrapper[AstrAgentContext],
        *,
        generation: int = 0,
        **kwargs,
    ) -> _SolveRun:
        event = _event_of(context)
        controller = getattr(self.service, "verification", None)
        structured = controller is not None
        mode = str(kwargs.get("mode") or "auto").strip().lower()
        if mode not in _VERIFY_MODES:
            return self._failed_run(
                f"error: 不支持的 mode {mode!r}，只能是 auto / captcha / quiz",
                "mode 参数非法",
                generation,
                structured,
            )
        include_image = bool(kwargs.get("include_image", True))
        timeout_s, remaining_s = self._vision_timeout(
            controller, kwargs.get("timeout_seconds"), structured
        )
        if structured and remaining_s <= 1.0:
            return self._failed_run(
                "error: 本次尝试剩余时间不足，已中止求解且未执行任何操作；"
                "请等待验证控制器进入人工协助",
                "尝试剩余时间不足",
                generation,
                structured,
            )
        app_context = getattr(self.service, "app_context", None)
        if app_context is None:
            return self._failed_run(
                "error: 缺少 AstrBot 上下文，无法调用视觉模型；可改用 "
                "phone_bypass_verify capture 后手动 drag/tap_points，或在 AstrBot 配置"
                "「图片描述 Provider」/插件 perception.ocr_provider_id",
                "缺少 AstrBot 上下文",
                generation,
                structured,
            )
        provider_id, _source = _resolve_ocr_provider(self.service, app_context, event)
        if not provider_id:
            return self._failed_run(
                "error: 未找到可用的视觉模型 Provider：请在插件 "
                "perception.ocr_provider_id 或 AstrBot「图片描述 Provider」中配置；"
                "也可用 phone_bypass_verify capture 后手动 drag/tap_points",
                "缺少视觉模型 Provider",
                generation,
                structured,
            )

        # 第一阶段：快速截图 + 识图（内容摘要 / 是否验证码 / 是否答题）
        try:
            data, path, meta = await self.service.capture(
                fast=True, quiet=True, record_metric=False, persist=False
            )
        except PhoneControlError as exc:
            return self._failed_run(
                f"error: 快速截图失败：{exc}；可改用 phone_bypass_verify capture 后手动操作",
                "快速截图失败",
                generation,
                structured,
            )
        try:
            first, first_text = await _vision_json(
                app_context,
                provider_id,
                _SOLVE_FIRST_PROMPT,
                path,
                timeout_s,
                service=self.service,
                event=event,
                source="internal:solve_verification",
            )
        except asyncio.TimeoutError:
            return self._failed_run(
                f"error: 识图超时（>{timeout_s:.0f}s，provider={provider_id}）；建议用 "
                "phone_bypass_verify capture 看验证形式后手动 drag/tap_points，或调大 "
                "perception.verify_timeout_seconds / 检查视觉 Provider 是否支持图片输入",
                "识图超时",
                generation,
                structured,
            )
        except Exception as exc:  # noqa: BLE001 - 视觉调用失败给可操作建议
            return self._failed_run(
                f"error: 识图调用失败（provider={provider_id}）：{exc}；建议用 "
                "phone_bypass_verify capture 后手动 drag/tap_points，或检查 AstrBot"
                "「图片描述 Provider」/插件 perception.ocr_provider_id 配置",
                "识图调用失败",
                generation,
                structured,
            )
        if first is None:
            preview = _clean_solve_text(first_text, 120) or "（空响应）"
            return self._failed_run(
                f"error: 识图结果不是可用 JSON（provider={provider_id}）：{preview}；"
                "建议改用 phone_bypass_verify capture 看验证形式后手动 drag/tap_points",
                "识图结果无法解析",
                generation,
                structured,
            )

        content = _clean_solve_text(first.get("content"), 120)
        is_captcha = _as_bool(first.get("is_captcha"))
        is_quiz = _as_bool(first.get("is_quiz"))
        captcha_type = _normalize_captcha_type(first.get("captcha_type"))
        if mode == "captcha":
            is_captcha, is_quiz = True, False
        elif mode == "quiz":
            is_captcha, is_quiz = False, True
        if is_captcha and captcha_type == "none":
            captcha_type = "sequence"
        mime = _capture_mime(meta)

        if not is_captcha and not is_quiz:
            lines = [
                f"屏幕内容：{content or '（模型未给出摘要）'}",
                "未检测到验证码/答题页面。",
            ]
            outcome = None
            if structured:
                outcome = verification_mod.SolveOutcome(
                    outcome=verification_mod.OUTCOME_NOT_CHALLENGE,
                    kind=verification_mod.SOLVE_KIND_QUIZ,
                    evidence=content,
                    generation=generation,
                )
            return _SolveRun(
                result=_image_result(
                    "\n".join(lines), data if include_image else None, mime
                ),
                outcome=outcome,
            )

        # 第二阶段：重新截图 + 让视觉模型给出操作序列与最终确认点
        kind = "quiz" if is_quiz else "captcha"
        timeout_s, remaining_s = self._vision_timeout(
            controller, kwargs.get("timeout_seconds"), structured
        )
        if structured and remaining_s <= 1.0:
            return self._failed_run(
                "error: 本次尝试剩余时间不足，已中止求解且未执行任何操作；"
                "请等待验证控制器进入人工协助",
                "尝试剩余时间不足",
                generation,
                structured,
            )
        try:
            _data2, path2, _meta2 = await self.service.capture(
                fast=True, quiet=True, record_metric=False, persist=False
            )
        except PhoneControlError as exc:
            return self._failed_run(
                f"error: 解答前重新截图失败：{exc}；第一段识图：{content or '无摘要'}；"
                "未执行任何操作；可改用 phone_bypass_verify 手动处理",
                "解答前重新截图失败",
                generation,
                structured,
            )
        try:
            second, second_text = await _vision_json(
                app_context,
                provider_id,
                _solve_second_prompt(kind, captcha_type),
                path2,
                timeout_s,
                service=self.service,
                event=event,
                source="internal:solve_verification",
            )
        except asyncio.TimeoutError:
            return self._failed_run(
                f"error: 求解识图超时（>{timeout_s:.0f}s，provider={provider_id}），"
                f"已中止且未执行任何操作；第一段识图：{content or '无摘要'}；"
                "建议用 phone_bypass_verify capture 后手动 drag/tap_points，或调大 "
                "perception.verify_timeout_seconds",
                "求解识图超时",
                generation,
                structured,
            )
        except Exception as exc:  # noqa: BLE001 - 求解识图失败给可操作建议
            return self._failed_run(
                f"error: 求解识图调用失败（provider={provider_id}）：{exc}；已中止且"
                f"未执行任何操作；第一段识图：{content or '无摘要'}；可改用 "
                "phone_bypass_verify 手动处理",
                "求解识图调用失败",
                generation,
                structured,
            )
        if second is None:
            preview = _clean_solve_text(second_text, 120) or "（空响应）"
            return self._failed_run(
                f"error: 求解结果不是可用 JSON（provider={provider_id}）：{preview}；"
                f"已中止且未执行任何操作；第一段识图：{content or '无摘要'}；可改用 "
                "phone_bypass_verify 手动处理",
                "求解结果无法解析",
                generation,
                structured,
            )
        ops = _normalize_verify_ops(second.get("ops"))
        confirm = _normalize_confirm(second.get("confirm"))
        if not ops:
            return self._failed_run(
                "error: 视觉模型没有给出可执行的 ops 坐标；已中止且未执行任何操作；"
                f"第一段识图：{content or '无摘要'}；可改用 phone_bypass_verify capture "
                "后手动 drag/tap_points",
                "求解结果缺少可执行操作",
                generation,
                structured,
            )
        note = _clean_solve_text(second.get("note"), 80)
        answer = _clean_solve_text(second.get("answer"), 80)
        reason = _clean_solve_text(second.get("reason"), 80)

        # 执行前记录当前界面文本，作为结构化判定的对比基线
        pre_text = ""
        try:
            pre_text, _pre_snapshot = await self.service.screen(max_nodes=0)
        except Exception as exc:  # noqa: BLE001 - 观察失败不阻断执行，判定降级
            logger.warning("求解前界面观察失败：%s", exc)
        pre_text = pre_text or content

        try:
            summary = await self.service.exec_verify_ops(
                ops, confirm=confirm, wait_seconds=1.0
            )
        except GateError:
            raise
        except PhoneControlError as exc:
            return self._failed_run(
                f"error: 自动执行验证操作失败：{exc}；第一段识图："
                f"{content or '无摘要'}；可改用 phone_bypass_verify 手动处理",
                "执行验证操作失败",
                generation,
                structured,
            )

        # 执行后再读一次界面，形成结构化判定（视觉模型不判断最终结果）
        observation_ok = False
        post_text = ""
        try:
            post_text, _post_snapshot = await self.service.screen(max_nodes=0)
            observation_ok = True
        except Exception as exc:  # noqa: BLE001 - 观察失败按无法确认处理
            logger.warning("求解后界面观察失败：%s", exc)
        outcome = None
        if structured:
            outcome = verification_mod.judge_solve_outcome(
                kind=(
                    verification_mod.SOLVE_KIND_QUIZ
                    if is_quiz
                    else verification_mod.SOLVE_KIND_CAPTCHA
                ),
                pre_text=pre_text,
                post_text=post_text,
                observation_ok=observation_ok,
                generation=generation,
            )

        # 执行后再快速截图，供模型确认结果
        image: bytes | None = None
        if include_image:
            try:
                image, _post_path, post_meta = await self.service.capture(
                    fast=True, quiet=True, record_metric=False, persist=False
                )
                mime = _capture_mime(post_meta)
            except PhoneControlError:
                image = None

        lines = [
            f"验证码/做题自动处理：类型={'答题/测试' if is_quiz else '人机验证'}"
            + (f"（{captcha_type}）" if is_captcha else ""),
            f"图片内容：{content or '（无摘要）'}",
        ]
        if answer:
            lines.append(f"答案：{answer}")
        if reason:
            lines.append(f"理由：{reason}")
        if note:
            lines.append(f"操作说明：{note}")
        lines.append(f"执行结果：{summary}")
        if not observation_ok:
            lines.append("（执行后界面观察失败，结果判定降级为「无法确认」）")
        if include_image:
            lines.append(
                "已重新截图，可查看图片确认结果。"
                if image is not None
                else "（执行后重新截图失败，未附图片）"
            )
        else:
            lines.append("（include_image=false，未附执行后截图）")
        return _SolveRun(
            result=_image_result("\n".join(lines), image, mime),
            outcome=outcome,
        )
