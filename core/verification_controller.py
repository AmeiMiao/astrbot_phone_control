"""验证会话与人工协助控制器。

状态机与文案在 ``core/verification.py``；本模块负责把状态机接入插件：
发送求助通知、驱动监控协程、在验证期间暂停手机自动操作。
"""

from __future__ import annotations

from .identity import PLUGIN_NAME

import asyncio
from contextlib import suppress
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.platform.message_session import MessageSession

from . import verification as verification_mod


class PhoneVerification:
    """验证会话与人工协助控制器。

    - 尝试次数/总时限/人工等待由 ``verification`` 配置控制；
    - 状态持久化到插件数据目录 ``verification.json``，插件重载/重启不丢；
    - 到限进入 ``awaiting_user``：暂停手机自动操作、保留页面、发一次求助通知；
    - 用户通过 ``/phone verify status|resume|cancel <id>`` 处理。
    """

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.store = verification_mod.VerificationStore(plugin.data_dir)
        loaded = self.store.load_result()
        self.state: dict[str, Any] = loaded.state
        self._lock = asyncio.Lock()
        self._notify_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._log = getattr(plugin, "_device_log", logger.info)
        # 持久化故障：写盘失败时 fail-closed，所有设备操作拒绝直到恢复。
        self._persist_error = ""
        # 读取故障：状态文件无法可靠读取时 fail-closed，需人工显式处置后才解除；
        # 处置前禁止写盘，避免静默覆盖损坏文件丢掉证据。
        self._load_error = loaded.reason
        if loaded.fault:
            logger.error(
                "验证状态加载失败（%s，%s）：%s",
                self.store.path,
                loaded.kind,
                self._load_error,
            )
        # 在途求解取消信号：暂停/过期/取消时 set，新会话/恢复时换新 Event。
        self._cancel_event: asyncio.Event | None = None

    # ---- 基础状态 -----------------------------------------------------

    @property
    def config(self) -> Any:
        try:
            return self.plugin.service.config.verification
        except Exception:  # noqa: BLE001
            return None

    @property
    def active(self) -> bool:
        return verification_mod.is_active(self.state)

    @property
    def paused(self) -> bool:
        """手机自动操作是否暂停（等待用户协助/待确认/已过期未处置）。"""
        status = str((self.state or {}).get("status") or "")
        return status in verification_mod.PAUSED_STATUSES or status == verification_mod.STATUS_EXPIRED

    def _limits(self) -> dict[str, int]:
        config = self.config
        current = {}
        if config is not None:
            current = {
                "max_attempts": config.max_attempts,
                "total_timeout_seconds": config.total_timeout_seconds,
            }
        return verification_mod.effective_limits(self.state or {}, current)

    def _limits_for(self, state: dict[str, Any]) -> dict[str, int]:
        config = self.config
        current = {}
        if config is not None:
            current = {
                "max_attempts": config.max_attempts,
                "total_timeout_seconds": config.total_timeout_seconds,
            }
        return verification_mod.effective_limits(state, current)

    def _save(self) -> bool:
        if self._load_error:
            # 读取故障处置前不覆盖原文件：保留证据，等待用户显式处置
            return False
        ok = self.store.save(self.state if self.state else {})
        if ok:
            if self._persist_error:
                self._persist_error = ""
                self._log("验证：状态持久化已恢复，解除自动操作阻断")
                # 故障期间 set 的取消信号不能留给后续求解，换新 Event
                self._rotate_cancel()
            return True
        self._persist_error = (
            f"验证状态无法写入 {self.store.path}（磁盘/权限异常），"
            "已暂停手机自动操作；恢复写入后自动解除"
        )
        logger.warning("验证状态持久化失败（%s），已阻断手机自动操作", self.store.path)
        self._signal_cancel()
        return False

    # ---- 持久化 / 取消信号 ---------------------------------------------

    def health_reason(self) -> str:
        """状态读写故障导致的执行阻断文案；空串表示健康。"""
        return self._load_error or self._persist_error

    @property
    def persistence_ok(self) -> bool:
        """状态是否能正常持久化（失败时自动调度停止）。"""
        return not (self._load_error or self._persist_error)

    def load_fault(self) -> bool:
        """状态文件读取故障是否尚未处置（处置前禁止写盘与设备操作）。"""
        return bool(self._load_error)

    def _cancel_signal(self) -> asyncio.Event:
        event = self._cancel_event
        if event is None:
            event = asyncio.Event()
            self._cancel_event = event
        return event

    def cancelled(self) -> asyncio.Event:
        """在途求解取消信号：暂停/过期/取消时 set。"""
        return self._cancel_signal()

    def _rotate_cancel(self) -> None:
        """先触发旧取消信号再换新 Event：在途求解按旧信号退出，新执行从干净状态开始。"""
        self._signal_cancel()
        self._cancel_event = asyncio.Event()

    def _signal_cancel(self) -> None:
        self._cancel_signal().set()

    @property
    def cancel_generation(self) -> int:
        """当前验证会话的取消代次（暂停/取消/交还代次时递增）。"""
        return int((self.state or {}).get("cancel_generation") or 0)

    @property
    def session_id(self) -> str:
        """当前验证会话 ID（空串表示没有会话）。"""
        return str((self.state or {}).get("id") or "")

    @property
    def attempt_id(self) -> int:
        """当前已预占的尝试序号（每次 ``reserve_attempt`` 递增）。"""
        return verification_mod.normalize_int(
            (self.state or {}).get("attempts"), 0, minimum=0
        )

    def reservation_identity(self) -> verification_mod.AttemptIdentity:
        """本次预占产生的执行身份（供求解工具随结果携带）。"""
        return verification_mod.current_identity(self.state)

    def attempt_remaining_seconds(self) -> float:
        """当前单次尝试的剩余秒数；未处于尝试中返回 ``inf``。"""
        if not self.active:
            return float("inf")
        if str((self.state or {}).get("status") or "") != verification_mod.STATUS_ATTEMPTING:
            return float("inf")
        try:
            return float(
                verification_mod.remaining_attempt_seconds(
                    self.state, limits=self._limits()
                )
            )
        except Exception:  # noqa: BLE001 - 期限计算失败时不限制
            return float("inf")

    def execution_deadline_seconds(self) -> float:
        """验证类执行的剩余期限：会话剩余与单次尝试剩余的较小值。"""
        if not self.active:
            return float("inf")
        try:
            return float(
                verification_mod.execution_deadline_seconds(
                    self.state, limits=self._limits()
                )
            )
        except Exception:  # noqa: BLE001 - 期限计算失败时不限制
            return float("inf")

    def status_text(self) -> str:
        text = verification_mod.status_text(self.state)
        if self._load_error:
            text += (
                f"\n- 状态故障：{self._load_error}"
                "\n- 恢复：确认不需要保留原验证状态后发送 `/phone verify cancel <任意编号>`，"
                "插件会把损坏文件改名保留并解除阻断"
            )
        elif self._persist_error:
            text += "\n- 状态持久化：失败（已阻断手机自动操作，恢复写入后自动解除）"
        return text

    # ---- 门禁 ---------------------------------------------------------

    _VERIFY_TOOLS = frozenset({"phone_solve_verification", "phone_bypass_verify"})
    # 只读观察工具：只读取设备/本地状态，不切换 App、不点击。
    # 会导航或产生设备写副作用的工具（如 phone_check_inbox 可能先打开 App 再点击
    # 消息入口）必须按写操作处理，不得列入本表。
    _READ_ONLY_TOOLS = frozenset(
        {
            "phone_status",
            "phone_screen",
            "phone_screenshot",
            "phone_clipboard",
            "phone_read_screen",
            "phone_live_screen",
            "phone_wait",
            "phone_traffic",
            "phone_recent_chat",
            "phone_learn",
            "phone_recall_diary",
            "phone_app_memory",
            "phone_skill",
            "phone_list_apps",
        }
    )

    def guard_tool(self, tool_name: str, *, write: bool) -> str:
        """工具执行前的验证门禁；返回空串表示放行。"""
        health = self.health_reason()
        if health:
            return health
        if not self.state:
            return ""
        status = str(self.state.get("status") or "")
        if status in verification_mod.CLOSED_STATUSES:
            return ""
        if status == verification_mod.STATUS_EXPIRED:
            return (
                "error: 人工等待已到期，手机自动操作仍保持暂停；请由用户发送 "
                f"`/phone verify cancel {self.state.get('id')}` 释放后再继续"
            )
        if status in verification_mod.PAUSED_STATUSES:
            if tool_name in self._VERIFY_TOOLS and status == verification_mod.STATUS_AWAITING_USER:
                return (
                    "error: 验证已暂停等待人工协助，不能继续自动尝试；"
                    f"请用户发送 `/phone verify resume {self.state.get('id')}` 或 "
                    f"`/phone verify cancel {self.state.get('id')}`"
                )
            return (
                "error: 手机自动操作已暂停（等待用户在设备上完成验证）；"
                f"用户确认后发送 `/phone verify resume {self.state.get('id')}`，"
                f"或 `/phone verify cancel {self.state.get('id')}` 结束等待。不要继续操作手机。"
            )
        # observing / attempting：只允许验证工具与只读观察，其他写操作交给验证控制器
        if tool_name in self._VERIFY_TOOLS or tool_name in self._READ_ONLY_TOOLS:
            return ""
        if write:
            return (
                "error: 当前有进行中的验证会话，普通写操作已统一交给验证控制器；"
                "请使用 phone_solve_verification / phone_bypass_verify 处理，"
                f"或等待会话结束（id={self.state.get('id')}）"
            )
        return ""

    # 暂停/过期时仍允许执行的只读命令（不触碰写操作）。
    _PAUSED_ALLOWED_COMMANDS = frozenset(
        {"status", "traffic", "quota", "learn", "novelty", "diary"}
    )

    def guard_command(self, sub: str = "") -> str:
        """命令层门禁：暂停/过期/持久化故障时拒绝写类命令，只读命令放行。"""
        health = self.health_reason()
        if health:
            return health
        if not self.state:
            return ""
        status = str(self.state.get("status") or "")
        if status in verification_mod.CLOSED_STATUSES:
            return ""
        if status == verification_mod.STATUS_EXPIRED:
            return (
                "error: 人工等待已到期，手机自动操作仍保持暂停；请发送 "
                f"`/phone verify cancel {self.state.get('id')}` 释放后再继续"
            )
        if status in verification_mod.PAUSED_STATUSES:
            if str(sub or "").strip().lower() in self._PAUSED_ALLOWED_COMMANDS:
                return ""
            return "手机自动操作已暂停（等待人工完成验证）。\n" + self.status_text()
        return ""

    # ---- 会话生命周期 -------------------------------------------------

    async def _start_session(self, event: Any) -> None:
        service = self.plugin.service
        app = ""
        activity = ""
        with suppress(Exception):
            app, activity = await service.actions.foreground()
        try:
            autonomous = service.is_autonomous_play(event) or service.is_task_session(
                event
            )
        except Exception:  # noqa: BLE001
            autonomous = False
        origin = "auto" if autonomous else "user"
        initiator = "" if autonomous else str(
            getattr(event, "unified_msg_origin", "") or ""
        )
        config = self.config
        notify_target = ""
        if config is not None:
            notify_target = str(config.notify_target or "").strip()
        notify_umo = notify_target or initiator
        if not notify_umo:
            with suppress(Exception):
                notify_umo = str(service.last_session() or "")
        self.state = verification_mod.new_session(
            device=str(getattr(service.adb, "serial", "") or ""),
            app=app,
            activity=activity,
            origin=origin,
            initiator_umo=initiator,
            notify_umo=notify_umo,
            max_attempts=config.max_attempts if config else 3,
            total_timeout_seconds=config.total_timeout_seconds if config else 120,
            attempt_timeout_seconds=config.attempt_timeout_seconds if config else 30,
            manual_wait_timeout_seconds=config.manual_wait_timeout_seconds
            if config
            else 600,
        )
        self._rotate_cancel()
        if not self._save():
            return
        self._log(
            f"验证：建立会话 {self.state.get('id')}（App={app or '未知'}，"
            f"来源={origin}，通知={notify_umo or '无'}）"
        )

    async def reserve_attempt(self, event: Any, *, kind: str = "solver") -> tuple[bool, str]:
        """预占一次自动尝试（持久化）；写盘失败或状态故障时回滚并拒绝。"""
        async with self._lock:
            health = self.health_reason()
            if health:
                return False, health
            status = str((self.state or {}).get("status") or "")
            if status == verification_mod.STATUS_EXPIRED:
                return False, self.guard_tool("phone_solve_verification", write=False)
            if not self.active:
                if self.state and verification_mod.is_closed(self.state):
                    self.state = {}
                if self.state:
                    # 仍处于暂停（等待人工/待确认）时不自动开始新会话。
                    return False, self.guard_tool(
                        "phone_solve_verification", write=False
                    )
                await self._start_session(event)
                health = self.health_reason()
                if health:
                    return False, health
            previous = {
                key: self.state.get(key)
                for key in (
                    "attempts",
                    "status",
                    "attempt_started_at",
                    "last_reason",
                    "failure_count",
                    "quiz_answered",
                    "cancel_generation",
                )
            }
            limits = self._limits()
            ok, reason = verification_mod.reserve_attempt(self.state, limits=limits)
            if ok:
                if not self._save():
                    for key, value in previous.items():
                        self.state[key] = value
                    return False, self.health_reason()
                self._log(
                    f"验证：预占尝试 {self.state.get('attempts')}/"
                    f"{limits.get('max_attempts')}（{kind}，id={self.state.get('id')}）"
                )
                await self._stop_live_screen()
            else:
                if not self._save():
                    return False, self.health_reason()
                await self._after_state_change()
            return ok, reason

    async def finish_attempt(
        self,
        event: Any,
        *,
        passed: bool = False,
        reason: str = "",
    ) -> None:
        """结束一次尝试并持久化；耗尽时进入人工协助。"""
        async with self._lock:
            if not self.active:
                return
            limits = self._limits()
            verification_mod.finish_attempt(
                self.state, passed=passed, reason=reason, limits=limits
            )
            if not self._save():
                return
            if passed:
                self._log(
                    f"验证：确认通过，关闭会话 {self.state.get('id')}"
                    f"（用时 {verification_mod.elapsed_ms(self.state) / 1000:.1f}s）"
                )
                await self._stop_live_screen()
            else:
                self._log(
                    f"验证：本次尝试未通过（失败 {self.state.get('failure_count')}/"
                    f"{limits.get('max_attempts')}，id={self.state.get('id')}）"
                )
                await self._after_state_change()

    async def submit_outcome(
        self, event: Any, outcome: "verification_mod.SolveOutcome"
    ) -> str:
        """提交结构化求解结果；执行身份不匹配或已失效时丢弃本次结果。

        会话 ID、尝试序号与取消代次全部精确比较（代次 0 也参与），并在持锁后
        复核会话仍进行中、未暂停、本次尝试未到期，避免旧执行结果写入新会话。
        """
        async with self._lock:
            if not self.active:
                self._log(
                    "验证：丢弃已结束会话的求解结果"
                    f"（会话 {getattr(outcome, 'session_id', '') or '未知'}）"
                )
                return ""
            limits = self._limits()
            mismatch = verification_mod.submission_mismatch_reason(
                self.state, outcome, limits=limits
            )
            if mismatch:
                self._log(
                    f"验证：丢弃失效求解结果（{mismatch}，"
                    f"id={self.session_id}）"
                )
                return ""
            verification_mod.apply_outcome(self.state, outcome, limits=limits)
            if not self._save():
                return self.health_reason()
            status = str(self.state.get("status") or "")
            evidence = str(getattr(outcome, "evidence", "") or "")
            if status == verification_mod.STATUS_PASSED:
                self._log(
                    "验证：挑战已解除/答题完成，关闭会话 "
                    f"{self.state.get('id')}（{evidence or outcome.outcome}）"
                )
                await self._stop_live_screen()
            elif self.paused:
                self._log(
                    f"验证：{evidence or outcome.outcome}，暂停并请求人工协助"
                    f"（失败 {self.state.get('failure_count')}/"
                    f"{limits.get('max_attempts')}，id={self.state.get('id')}）"
                )
                await self._after_state_change()
            else:
                self._log(
                    f"验证：结果 {outcome.outcome}"
                    f"（答题进度 {self.state.get('quiz_answered', 0)}，"
                    f"失败 {self.state.get('failure_count')}/{limits.get('max_attempts')}，"
                    f"id={self.state.get('id')}）"
                )
            return self.status_text()

    async def request_manual(self, reason: str = "unsupported") -> None:
        async with self._lock:
            if not self.active:
                return
            verification_mod.apply_awaiting(
                self.state, reason=reason, limits=self._limits()
            )
            self._save()
            await self._after_state_change()

    async def _stop_live_screen(self) -> None:
        with suppress(Exception):
            await self.plugin._stop_live_screen("等待人工验证")

    async def _after_state_change(self) -> None:
        """状态变化后的副作用：暂停时取消在途执行、停采集并安排求助通知。"""
        if self.paused:
            # 暂停优先于任何在途动作：取消信号让求解器竞速退出，
            # 设备租约强制释放使动作批次的 checkpoint 立即失败。
            self._signal_cancel()
            gate = getattr(self.plugin, "device_gate", None)
            if gate is not None:
                gate.force_release("verification_paused")
            await self._stop_live_screen()
            self._schedule_notify()

    def _schedule_notify(self) -> None:
        if not verification_mod.should_notify(self.state):
            return
        task = self._notify_task
        if task is not None and not task.done():
            return
        try:
            self._notify_task = asyncio.create_task(
                self._notify_loop(),
                name=f"{PLUGIN_NAME}_verify_notify",
            )
        except RuntimeError:
            self._notify_task = None

    async def _notify_loop(self) -> None:
        """确定性地发送求助通知；失败有界重试（5s/30s/60s）。"""
        delays = [0.0, 5.0, 30.0, 60.0]
        index = 0
        while verification_mod.should_notify(self.state) and index < len(delays):
            delay = delays[index]
            index += 1
            if delay:
                await asyncio.sleep(delay)
            if not verification_mod.should_notify(self.state):
                return
            target = str(self.state.get("notify_umo") or "").strip()
            if not target:
                verification_mod.mark_notify(
                    self.state, state_name=verification_mod.NOTIFY_FAILED, error="缺少通知目标"
                )
                self._save()
                self._log("验证：缺少求助通知目标，保持暂停（notification_failed）")
                return
            message = verification_mod.build_awaiting_message(
                self.state, limits=self._limits()
            )
            context = getattr(self.plugin, "context", None)
            try:
                session = MessageSession.from_str(target)
                sent = await context.send_message(
                    session, MessageChain().message(message)
                )
            except Exception as exc:  # noqa: BLE001 - 通知失败保持暂停
                verification_mod.mark_notify(
                    self.state, state_name=verification_mod.NOTIFY_FAILED, error=str(exc)
                )
                self._save()
                self._log(f"验证：求助通知发送失败（{exc}），稍后重试")
                continue
            if sent:
                verification_mod.mark_notify(
                    self.state, state_name=verification_mod.NOTIFY_SENT
                )
                self._save()
                self._log(
                    f"验证：已向 {target} 发送求助通知（id={self.state.get('id')}）"
                )
                return
            verification_mod.mark_notify(
                self.state, state_name=verification_mod.NOTIFY_FAILED, error="send_message 返回 False"
            )
            self._save()
            self._log("验证：求助通知未送达（send_message=False），稍后重试")

    # ---- 恢复 / 取消 / 过期 ------------------------------------------

    def _match_id(self, session_id: str) -> bool:
        wanted = str(session_id or "").strip()
        current = str((self.state or {}).get("id") or "")
        if not wanted or not current:
            return False
        return current.startswith(wanted)

    async def resume(self, session_id: str, event: Any) -> str:
        health = self.health_reason()
        if health:
            return health
        async with self._lock:
            if not self.active:
                if self.state and self.state.get("status") == verification_mod.STATUS_EXPIRED:
                    return (
                        "人工等待已到期（任务已结束），不能恢复原任务；"
                        f"请发送 `/phone verify cancel {self.state.get('id')}` 释放设备暂停后重新发起任务"
                    )
                return "当前没有进行中的验证会话"
            if not self._match_id(session_id):
                return f"验证编号不匹配：当前会话是 {self.state.get('id')}"
            if not self.paused:
                return "当前验证会话不在等待人工协助状态，无需恢复"
            verification_mod.enter_checking(self.state)
            if not self._save():
                return self.health_reason()
            self._rotate_cancel()
            self._log(f"验证：用户请求恢复，开始有界结果观察（{self.state.get('id')}）")
        present = True
        observed = False
        try:
            text, _snapshot = await asyncio.wait_for(
                self.plugin.service.screen(max_nodes=80), timeout=12.0
            )
            observed = True
            present = verification_mod.challenge_present(text)
        except Exception as exc:  # noqa: BLE001 - 观察失败按未知处理，保持等待
            self._log(f"验证：恢复前观察失败（{exc}），保持等待")
        async with self._lock:
            if not self.active:
                return "验证会话已结束"
            if not observed:
                verification_mod.finish_checking(
                    self.state, passed=False, reason="observe_failed"
                )
                if not self._save():
                    return self.health_reason()
                await self._after_state_change()
                return (
                    "无法确认验证是否已通过（观察失败）；手机仍保持暂停，"
                    f"请再试 `/phone verify resume {self.state.get('id')}` 或 "
                    f"`/phone verify cancel {self.state.get('id')}`"
                )
            if present:
                verification_mod.finish_checking(
                    self.state, passed=False, reason="still_present"
                )
                if not self._save():
                    return self.health_reason()
                await self._after_state_change()
                return (
                    "仍检测到验证页面；手机保持暂停，请在设备上完成后再发送 "
                    f"`/phone verify resume {self.state.get('id')}`"
                )
            verification_mod.finish_checking(
                self.state, passed=True, reason="user_confirmed"
            )
            if not self._save():
                return self.health_reason()
            self._log(f"验证：用户确认后观察通过，解除暂停（{self.state.get('id')}）")
            await self._stop_live_screen()
            peer_gate = getattr(self.plugin, "device_gate", None)
            if peer_gate is not None:
                peer_gate.force_release("verify_resume")
            await self._resume_schedulers()
            return (
                "验证已确认通过，手机自动操作已恢复。原任务已结束，"
                "可以重新发起玩手机/手机任务。"
            )

    async def _resume_schedulers(self) -> None:
        """解除暂停后恢复随机/持续刷调度（插件启动时可能因暂停未启动循环）。"""
        try:
            self.plugin._start_random_play_loop()
            self.plugin._start_screenshot_cleanup_loop()
            if bool(getattr(self.plugin, "_continuous_enabled", False)):
                self.plugin._continuous_last_finished = 0.0
                self.plugin._start_continuous_loop()
            self._log("验证：已解除暂停，手机调度器恢复")
        except Exception as exc:  # noqa: BLE001 - 恢复失败由后续配置/重载兜底
            logger.warning("验证解除暂停后恢复调度失败：%s", exc)

    async def _dispose_load_fault(self) -> str:
        """显式处置状态读取故障：原文件改名保留、清空内存状态并解除阻断。

        只由用户命令触发（``/phone verify cancel``）；``/phone stop`` 不会静默处置，
        避免把故障状态当正常收尾放过去。
        """
        backup = self.store.quarantine()
        if backup is None:
            self._log("验证：状态文件损坏且无法备份，保持设备阻断")
            return (
                "error: 验证状态文件损坏且无法保留备份"
                f"（{self.store.path}）；请检查插件数据目录权限后重试 "
                "`/phone verify cancel`"
            )
        self._load_error = ""
        self._persist_error = ""
        self.state = {}
        self._rotate_cancel()
        self._log(f"验证：状态文件损坏，已保留原文件为 {backup.name} 并解除设备阻断")
        await self._release_device_lease("verify_state_disposed")
        await self._resume_schedulers()
        return (
            f"验证状态文件损坏，原文件已保留为 {backup.name}；设备阻断已解除，"
            "原验证会话无法恢复，可重新发起任务。"
        )

    async def cancel(
        self,
        session_id: str,
        event: Any = None,
        *,
        restart_schedulers: bool = True,
        dispose_load_fault: bool = True,
    ) -> str:
        async with self._lock:
            if self._load_error:
                if not dispose_load_fault:
                    return self.health_reason()
                return await self._dispose_load_fault()
            if not self.active:
                if self.state and self.state.get("status") == verification_mod.STATUS_EXPIRED:
                    # 先触发旧执行的取消信号，再递增代次/换新 Event
                    self._signal_cancel()
                    verification_mod.mark_cancelled(self.state, reason="user_cancel")
                    if not self._save():
                        return self.health_reason()
                    self._rotate_cancel()
                    self._log(f"验证：用户取消已到期等待（{self.state.get('id')}），解除暂停")
                    await self._release_device_lease("verify_cancel")
                    if restart_schedulers:
                        await self._resume_schedulers()
                    return (
                        f"已释放到期的人工等待（{self.state.get('id')}），设备暂停已解除；"
                        "可重新发起任务。"
                    )
                return "当前没有进行中的验证会话"
            if session_id and not self._match_id(session_id):
                return f"验证编号不匹配：当前会话是 {self.state.get('id')}"
            # 先触发旧执行的取消信号，再递增代次/换新 Event：在途求解竞速退出，
            # 旧结果即使返回也会因会话/尝试/代次不匹配被 submit_outcome 拒绝。
            self._signal_cancel()
            verification_mod.mark_cancelled(self.state, reason="user_cancel")
            if not self._save():
                return self.health_reason()
            self._rotate_cancel()
            self._log(f"验证：用户取消等待（{self.state.get('id')}），解除暂停")
            await self._stop_live_screen()
            await self._release_device_lease("verify_cancel")
            if restart_schedulers:
                await self._resume_schedulers()
            return (
                f"已取消验证等待（{self.state.get('id')}），设备暂停已解除；"
                "原任务已终止，可重新发起任务。"
            )

    async def _release_device_lease(self, reason: str) -> None:
        """用户显式取消/恢复后清空设备占用，避免旧任务租约残留。"""
        gate = getattr(self.plugin, "device_gate", None)
        if gate is not None:
            gate.force_release(reason)

    async def cancel_for_stop(self) -> str:
        if self._load_error:
            # 状态文件故障只由显式的 /phone verify cancel 处置，停止命令不静默放行
            return (
                "验证状态文件损坏（手机自动操作仍阻断）；请执行 "
                "`/phone verify cancel <任意编号>` 处置"
            )
        if not self.active:
            if self.state and self.state.get("status") == verification_mod.STATUS_EXPIRED:
                return await self.cancel(
                    "", restart_schedulers=False, dispose_load_fault=False
                )
            return ""
        return await self.cancel(
            "", restart_schedulers=False, dispose_load_fault=False
        )

    # ---- 看门狗/启动恢复 ---------------------------------------------

    async def startup(self) -> None:
        """插件启动时恢复验证状态：不自动继续旧验证，先保持暂停。"""
        if self._load_error:
            logger.warning(
                "验证：状态文件损坏（%s），手机自动操作保持阻断；"
                "请执行 /phone verify cancel <任意编号> 显式处置",
                self.store.path,
            )
            return
        if not self.state:
            return
        if verification_mod.is_closed(self.state):
            self.state = {}
            self._save()
            return
        self._rotate_cancel()
        if self.state.get("status") in {
            verification_mod.STATUS_OBSERVING,
            verification_mod.STATUS_ATTEMPTING,
            verification_mod.STATUS_CHECKING,
        }:
            verification_mod.apply_awaiting(
                self.state, reason="restart", limits=self._limits()
            )
            if not self._save():
                logger.warning("验证：重启后状态保存失败，保持阻断")
        if self.paused:
            self._log(
                f"验证：启动恢复待处理会话 {self.state.get('id')}，手机自动操作保持暂停"
            )
            self._schedule_notify()

    def start_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        try:
            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(),
                name=f"{PLUGIN_NAME}_verify_watchdog",
            )
        except RuntimeError:
            self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(5)
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 看门狗失败只记录
                logger.warning("验证看门狗异常：%s", exc)

    async def _tick(self) -> None:
        # 持久化故障时先尝试补写：成功即恢复，失败则继续阻断自动操作。
        if self._persist_error:
            async with self._lock:
                if self._persist_error:
                    self._save()
            if self._persist_error:
                logger.warning("验证：状态持久化仍失败，手机自动操作保持阻断")
                return
        if not self.active:
            return
        limits = self._limits()
        if self.paused:
            if verification_mod.manual_wait_expired(
                self.state, limits=limits
            ):
                async with self._lock:
                    if not self.paused:
                        return
                    verification_mod.mark_expired(self.state, reason="manual_wait_timeout")
                    if not self._save():
                        return
                    self._signal_cancel()
                    self._log(
                        f"验证：人工等待到期（{self.state.get('id')}），"
                        "设备保持暂停，等待用户显式取消"
                    )
            return
        if verification_mod.session_timeout_reached(self.state, limits=limits):
            async with self._lock:
                if not self.active or self.paused:
                    return
                verification_mod.apply_awaiting(
                    self.state, reason="timeout", limits=limits
                )
                if not self._save():
                    return
                self._log(
                    f"验证：总时限 {limits.get('total_timeout_seconds')}s 已到，"
                    f"取消在途求解并请求人工协助（{self.state.get('id')}）"
                )
                await self._after_state_change()
        if verification_mod.attempt_deadline_reached(self.state, limits=limits):
            async with self._lock:
                if not self.active or self.paused:
                    return
                verification_mod.apply_awaiting(
                    self.state, reason="attempt_timeout", limits=limits
                )
                if not self._save():
                    return
                self._log(
                    f"验证：单次尝试时限 {limits.get('attempt_timeout_seconds')}s 已到，"
                    f"取消在途求解并请求人工协助（{self.state.get('id')}）"
                )
                await self._after_state_change()
            return

    async def shutdown(self) -> None:
        """插件停用：保存状态并取消后台任务（不解除设备暂停）。"""
        for task_attr in ("_notify_task", "_watchdog_task"):
            task = getattr(self, task_attr, None)
            setattr(self, task_attr, None)
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._signal_cancel()
        self._save()
