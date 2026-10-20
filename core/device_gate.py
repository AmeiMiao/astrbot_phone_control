"""统一设备执行入口：授权、设备占用、暂停、取消代次与剩余期限。

命令、LLM 工具与定时任务在接触设备前都经过本模块，避免不同任务交错执行
「观察 → 决策 → 操作」，也避免暂停/取消只停住某一个执行路径：

* 设备租约（lease）：同一时刻最多一个任务持有；同一事件内可重入。用户任务
  优先时由 :meth:`DeviceExecutionGate.preempt` 撤销自主任务租约；任务收尾清理
  使用可让路租约，新任务需要设备时直接接管（旧收尾在下一次检查处停止）；
* 授权与暂停状态由插件回调注入（本模块不导入 astrbot）；
* 取消代次（epoch）：撤销租约、进入暂停、用户取消或停止时递增；动作批次在
  每次设备操作前调用 :meth:`GateScope.checkpoint`，代次变化即中止；
* 期限：验证类执行取「会话剩余时间」与「单次尝试剩余时间」的较小值，到期
  抛出 :class:`ExecutionCancelled`；
* 后台基础设施（实时屏幕、抓包、媒体采集、scrcpy 后端）不持有租约，也不经
  contextvar 作用域，因此不受本门禁限制。

仅依赖标准库，可离线单测。
"""

from __future__ import annotations

import contextlib
import contextvars
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

# 事件里保存的 run key：同一次 Agent run / 命令处理内的多次设备访问共用一个租约。
RUN_KEY_EXTRA = "phone_device_run_key"

KIND_USER = "user"
KIND_AUTONOMOUS = "autonomous"
KIND_COMMAND = "command"

_KIND_LABELS = {
    KIND_USER: "用户任务",
    KIND_AUTONOMOUS: "自主任务",
    KIND_COMMAND: "命令",
}


class GateError(Exception):
    """设备执行被拒绝（权限、占用、暂停、期限或持久化故障）。"""

    def __init__(self, message: str, *, code: str = "denied") -> None:
        self.message = str(message or "当前不能操作手机")
        self.code = str(code or "denied")
        super().__init__(self.message)


class ExecutionCancelled(GateError):
    """执行中途被取消（暂停、代次变化、期限到达或租约被收回）。"""

    def __init__(self, message: str, *, code: str = "cancelled") -> None:
        super().__init__(message, code=code)


@dataclass(frozen=True)
class GateScope:
    """一次设备执行的作用域（仅代表租约与代次快照，不持久化）。"""

    gate: "DeviceExecutionGate" = field(repr=False, compare=False)
    event: Any = field(repr=False, compare=False)
    key: str = ""
    kind: str = KIND_USER
    purpose: str = ""
    write: bool = True
    owner_umo: str = ""
    epoch: int = 0
    deadline: float = math.inf

    def checkpoint(self, op: str = "") -> None:
        """设备操作前的统一检查；失败抛 :class:`ExecutionCancelled`。"""
        self.gate.checkpoint(op)


@dataclass
class _Lease:
    key: str
    kind: str
    owner_umo: str
    purpose: str
    since: float
    # 可让路租约（任务收尾清理）：新任务需要设备时直接接管，旧执行在下一次
    # checkpoint 处停止；同类让路租约之间仍按占用互斥处理。
    preemptible: bool = False


_CURRENT_SCOPE: contextvars.ContextVar[GateScope | None] = contextvars.ContextVar(
    "astrbot_phone_control_device_scope", default=None
)


def current_scope() -> GateScope | None:
    """返回当前 contextvar 内的设备作用域（无则 ``None``，即系统后台操作）。"""
    return _CURRENT_SCOPE.get()


class DeviceExecutionGate:
    """设备租约与执行代次控制器。

    Args:
        authorize: ``(event) -> str | None``，返回拒绝文案则不能进入设备。
        blocking_reason: ``(event, purpose, write) -> str``，返回非空文案表示
            当前不可执行（持久化故障、验证暂停、已过期等）。
        deadline_seconds: ``(event, purpose) -> float``，本次执行剩余秒数；
            ``inf`` 表示不限。
        autonomy_of: ``(event) -> bool``，事件是否属于插件自主任务。
        log: 可选的日志回调（字符串），异常不影响主流程。
        clock: 单调时钟，测试可注入。
        stale_seconds: 租约超过该时长后允许被新任务接管（防止漏释放永久占用）。
    """

    def __init__(
        self,
        *,
        authorize: Callable[[Any], str | None] | None = None,
        blocking_reason: Callable[[Any, str, bool], str] | None = None,
        deadline_seconds: Callable[[Any, str], float] | None = None,
        autonomy_of: Callable[[Any], bool] | None = None,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        stale_seconds: float = 1800.0,
    ) -> None:
        self._authorize = authorize
        self._blocking_reason = blocking_reason
        self._deadline_seconds = deadline_seconds
        self._autonomy_of = autonomy_of
        self._log_fn = log
        self._clock = clock
        self._stale_seconds = max(60.0, float(stale_seconds))
        self._lease: _Lease | None = None
        self._epoch = 0

    # ---- 状态 ---------------------------------------------------------

    @property
    def epoch(self) -> int:
        """当前取消代次；每次撤销租约/暂停/停止都会递增。"""
        return self._epoch

    @property
    def busy(self) -> bool:
        """是否已有任务持有设备租约。"""
        return self._lease is not None

    @property
    def owner_umo(self) -> str:
        lease = self._lease
        return lease.owner_umo if lease is not None else ""

    @property
    def owner_kind(self) -> str:
        lease = self._lease
        return lease.kind if lease is not None else ""

    def status_text(self) -> str:
        """``/phone status`` 用的占用状态文本。"""
        lease = self._lease
        if lease is None:
            return "设备占用：空闲"
        elapsed = max(0.0, self._clock() - lease.since)
        kind = _KIND_LABELS.get(lease.kind, lease.kind or "任务")
        purpose = f"（{lease.purpose}）" if lease.purpose else ""
        return (
            f"设备占用：{kind}{purpose}使用中，已持续 {elapsed:.0f}s；"
            "其他手机任务需等待其结束或发送 /phone stop"
        )

    # ---- 进入 / 释放 --------------------------------------------------

    def enter(
        self,
        event: Any,
        *,
        purpose: str,
        write: bool = True,
        preemptible: bool = False,
    ) -> GateScope:
        """进入设备执行作用域（不激活 contextvar）。

        Args:
            preemptible: 本次租约是否可让路（收尾清理等低优先级事务）。已有可
                让路租约时，普通任务直接接管并使其下一次检查中止；可让路任务
                之间仍遵循占用互斥。

        Raises:
            GateError: 鉴权失败、暂停/故障、期限已到或设备被其他任务占用。
        """
        denied = self._authorize(event) if self._authorize is not None else None
        if denied:
            raise GateError(denied)
        reason = self._blocked(event, purpose, write)
        if reason:
            raise GateError(reason)
        key = self._event_key(event)
        umo = self._event_umo(event)
        now = self._clock()
        lease = self._lease
        takeover = False
        if lease is not None and lease.key != key:
            if now - lease.since >= self._stale_seconds:
                self._log(
                    f"设备租约：回收超时租约（{lease.kind} {lease.key}，"
                    f"已持续 {now - lease.since:.0f}s）"
                )
                self.force_release("stale_lease")
                lease = None
            elif lease.preemptible and not preemptible:
                # 可让路租约（收尾清理）：等新请求通过期限校验后再接管，避免
                # 新请求本身无效时把旧执行的中止点提前。
                takeover = True
            else:
                raise GateError(self._busy_message(lease, now))
        # 期限在绑定新租约之前校验，避免到期操作留下无人释放的租约。
        remaining = self._remaining(event, purpose)
        if remaining <= 0:
            raise ExecutionCancelled("本次手机执行已到期，停止继续操作并转人工协助")
        if takeover and self._lease is not None:
            previous = self._lease
            self._epoch += 1
            self._lease = None
            lease = None
            self._log(
                f"设备租约：接管可让路租约（{previous.purpose or previous.kind}），"
                "旧执行将在下一次检查处停止"
            )
        if lease is None:
            kind = self._kind_for(event, purpose)
            lease = _Lease(
                key=key,
                kind=kind,
                owner_umo=umo,
                purpose=str(purpose or ""),
                since=now,
                preemptible=bool(preemptible),
            )
            self._lease = lease
            self._log(
                f"设备租约：{_KIND_LABELS.get(kind, kind)}获取"
                f"（{purpose or kind}，umo={umo or '未知'}）"
            )
        deadline = now + remaining if math.isfinite(remaining) else math.inf
        return GateScope(
            gate=self,
            event=event,
            key=key,
            kind=lease.kind,
            purpose=str(purpose or ""),
            write=bool(write),
            owner_umo=lease.owner_umo,
            epoch=self._epoch,
            deadline=deadline,
        )

    @contextlib.contextmanager
    def scope(
        self,
        event: Any,
        *,
        purpose: str,
        write: bool = True,
        preemptible: bool = False,
    ) -> Iterator[GateScope]:
        """``enter`` + 激活 contextvar；退出只清作用域，租约保留到 run 结束。"""
        gate_scope = self.enter(
            event, purpose=purpose, write=write, preemptible=preemptible
        )
        token = _CURRENT_SCOPE.set(gate_scope)
        try:
            yield gate_scope
        finally:
            _CURRENT_SCOPE.reset(token)

    def checkpoint(self, op: str = "") -> None:
        """当前作用域内设备操作前的统一检查。

        无活动作用域表示插件后台基础设施操作，直接放行；有作用域时校验暂停/
        故障、租约归属、取消代次、执行期限与鉴权结果。

        Raises:
            ExecutionCancelled: 需要立即中止当前执行。
        """
        scope = _CURRENT_SCOPE.get()
        if scope is None or scope.gate is not self:
            return
        lease = self._lease
        if lease is None or lease.key != scope.key:
            raise ExecutionCancelled("手机已被其他任务接管或任务已被停止，请立即收尾")
        if self._epoch != scope.epoch:
            raise ExecutionCancelled("手机操作已被暂停或取消，请立即停止本轮操作")
        now = self._clock()
        if now >= scope.deadline:
            raise ExecutionCancelled(
                "本次手机执行已到期，停止继续操作并转人工协助"
            )
        denied = self._authorize(scope.event) if self._authorize is not None else None
        if denied:
            raise ExecutionCancelled(denied)
        reason = self._blocked(scope.event, scope.purpose, scope.write)
        if reason:
            raise ExecutionCancelled(reason)
        if op:
            self._log(f"设备操作校验通过：{op}")

    def release_event(self, event: Any) -> bool:
        """释放该事件持有的租约（Agent run / 命令处理结束）。"""
        key = self._read_event_key(event)
        umo = self._event_umo(event)
        lease = self._lease
        if lease is None:
            return False
        if key is None:
            # 事件不携带 extra（或已被框架重建）：按 UMO 兜底释放。
            if not umo or lease.owner_umo != umo:
                return False
        elif lease.key != key:
            return False
        self._epoch += 1
        self._lease = None
        self._log(
            f"设备租约：释放（{_KIND_LABELS.get(lease.kind, lease.kind)}，"
            f"umo={lease.owner_umo or '未知'}）"
        )
        return True

    def preempt(self, reason: str = "user_priority") -> list[str]:
        """撤销自主任务租约（用户任务优先）；返回被撤销的租约键。"""
        lease = self._lease
        if lease is None or lease.kind != KIND_AUTONOMOUS:
            return []
        revoked = [lease.key]
        self._epoch += 1
        self._lease = None
        self._log(f"设备租约：撤销自主任务（{reason}），在途操作将中止")
        return revoked

    def force_release(self, reason: str = "force") -> None:
        """强制清空占用（用户 /phone stop、verify cancel、插件停用）。"""
        had = self._lease is not None
        self._epoch += 1
        self._lease = None
        if had:
            self._log(f"设备租约：强制释放（{reason}），在途操作将中止")

    # ---- 内部 ---------------------------------------------------------

    def _blocked(self, event: Any, purpose: str, write: bool) -> str:
        if self._blocking_reason is None:
            return ""
        try:
            return str(self._blocking_reason(event, purpose, write) or "")
        except Exception:  # noqa: BLE001 - 门禁回调异常时保守放行，由业务层自行校验
            return ""

    def _remaining(self, event: Any, purpose: str) -> float:
        if self._deadline_seconds is None:
            return math.inf
        try:
            value = float(self._deadline_seconds(event, purpose))
        except Exception:  # noqa: BLE001 - 期限回调异常时不限制
            return math.inf
        if math.isnan(value):
            return math.inf
        return value

    def _kind_for(self, event: Any, purpose: str) -> str:
        # 命令层传 ``command:<子命令>``（子命令用于暂停期只读放行判断）。
        if str(purpose or "").split(":", 1)[0] == "command":
            return KIND_COMMAND
        if self._autonomy_of is not None:
            try:
                if self._autonomy_of(event):
                    return KIND_AUTONOMOUS
            except Exception:  # noqa: BLE001 - 鸭子类型事件
                return KIND_USER
        return KIND_USER

    def _busy_message(self, lease: _Lease, now: float) -> str:
        elapsed = max(0.0, now - lease.since)
        kind = _KIND_LABELS.get(lease.kind, lease.kind or "任务")
        return (
            f"error: 手机正被另一个{kind}使用（已持续 {elapsed:.0f}s）；"
            "请等它结束，或发送 /phone stop 强制停止后再操作"
        )

    def _event_key(self, event: Any) -> str:
        key = self._read_event_key(event)
        if key:
            return key
        key = f"run-{uuid.uuid4().hex[:12]}"
        try:
            event.set_extra(RUN_KEY_EXTRA, key)
        except Exception:  # noqa: BLE001 - 鸭子类型事件不支持 extra
            return f"event-{id(event)}"
        return key

    @staticmethod
    def _read_event_key(event: Any) -> str | None:
        try:
            value = event.get_extra(RUN_KEY_EXTRA)
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            return None
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _event_umo(event: Any) -> str:
        try:
            return str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:  # noqa: BLE001 - 鸭子类型事件
            return ""

    def _log(self, message: str) -> None:
        if self._log_fn is None:
            return
        try:
            self._log_fn(message)
        except Exception:  # noqa: BLE001 - 日志回调异常不影响门禁
            pass
