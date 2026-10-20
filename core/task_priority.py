"""用户任务优先调度：自主玩手机与用户手机任务的让路/暂停/恢复判定（纯逻辑）。

AI 自刷（持续刷/随机/后台）玩手机期间，用户提出手机相关任务时应先执行用户任务：
自主任务不创建新轮次、自主任务的工具调用返回让路提示；用户任务全部结束后再自动
恢复持续刷。本模块只承载其中的纯状态机部分：不导入 astrbot、不做 I/O，便于本地单测。

术语：

- 自主任务（autonomous）：插件自身创建的手机任务，任务载荷里的 ``origin`` 为
  :data:`AUTONOMOUS_ORIGINS` 之一；
- 用户任务：用户在会话里直接发起的手机操作，事件没有 cron 任务载荷，``origin``
  为空串（见 :func:`is_autonomous_origin`）；
- 用户任务活跃：至少有一个用户手机任务尚未结束，由 :class:`UserTaskPriority`
  的 ``begin``/``end`` 维护。

``UserTaskPriority`` 只在事件循环线程内使用（插件单实例、单线程回调），因此不加锁。
"""

from __future__ import annotations

__all__ = [
    "AUTONOMOUS_ORIGINS",
    "UserTaskPriority",
    "is_autonomous_origin",
    "should_pause_autonomous",
    "should_resume_continuous",
    "should_yield_to_user",
]

# 插件自主创建的任务类型：主动玩手机、随机玩手机、后台定时任务、持续刷手机。
AUTONOMOUS_ORIGINS = frozenset(
    {
        "phone_play",
        "phone_random_play",
        "phone_background_task",
        "phone_continuous",
    }
)


def is_autonomous_origin(origin: object | None) -> bool:
    """判断任务 origin 是否属于插件自主创建的任务。

    Args:
        origin: 任务载荷里的 origin 字段；用户会话为空串或 ``None``。

    Returns:
        ``origin`` 精确属于 :data:`AUTONOMOUS_ORIGINS` 时返回 ``True``；
        ``None``、空串/纯空白、非字符串以及未知 origin 一律返回 ``False``
        （用户会话返回空 origin，不会被误判成自主任务）。
    """
    if not isinstance(origin, str):
        return False
    return origin in AUTONOMOUS_ORIGINS


def should_yield_to_user(*, origin: object | None, user_active: bool) -> bool:
    """自主任务的这次工具调用是否应让路给用户任务。

    Args:
        origin: 当前事件的任务 origin；用户自己的会话为空串。
        user_active: 是否已有用户手机任务活跃（``UserTaskPriority.active``）。

    Returns:
        仅当 ``user_active`` 为真且 ``origin`` 是自主任务时返回 ``True``；
        用户自己的任务（空 origin）永远不让路，避免用户任务被自己阻塞。
    """
    return bool(user_active) and is_autonomous_origin(origin)


def should_pause_autonomous(
    *,
    user_active: bool,
    continuous_enabled: bool,
    continuous_job_active: bool,
    random_enabled: bool,
) -> bool:
    """自主任务循环此刻是否应暂停创建新轮次。

    供持续刷/随机玩手机的后台循环在每次迭代判断：返回 ``True`` 时本轮不应创建
    新的自主任务，调用方等待后继续循环。

    Args:
        user_active: 是否已有用户手机任务活跃。
        continuous_enabled: 持续刷手机是否开启。
        continuous_job_active: 是否已有一轮持续刷任务在途。
        random_enabled: 随机玩手机是否开启。

    Returns:
        真值表（按优先级从上到下）：

        1. ``user_active`` 为真 → ``True``：用户任务优先，自主任务不开新轮；
        2. ``continuous_job_active`` 为真 → ``True``：已有一轮持续刷在途，按原有
           逻辑等它结束再排下一轮，不叠加新轮次（这里只表示「不新建」，不中断
           在途轮次）；
        3. 持续刷与随机玩手机都未开启 → ``True``：没有可调度的自主任务；
        4. 其他情况 → ``False``：交回调用方按原有间隔/概率逻辑继续调度。

    调用方仍需保留自身的模式分支（如持续刷循环在 ``continuous_enabled`` 为假时
    直接休眠）与间隔/概率判断，本函数只回答「此刻是否因用户任务或已有在途轮次而
    不应新建轮次」。
    """
    if bool(user_active):
        return True
    if bool(continuous_job_active):
        return True
    return not (bool(continuous_enabled) or bool(random_enabled))


def should_resume_continuous(
    *, continuous_enabled: bool, user_active_after_end: bool
) -> bool:
    """用户最后一次手机任务结束后，是否应立即恢复持续刷。

    Args:
        continuous_enabled: 持续刷手机是否开启。
        user_active_after_end: 结束该用户任务后是否仍有用户任务活跃
            （通常传 ``UserTaskPriority.active``）。

    Returns:
        持续刷开启且已无用户任务活跃时返回 ``True``：调用方应把持续刷的
        ``last_finished`` 置 0，让下一轮立即排程；否则返回 ``False``。
    """
    return bool(continuous_enabled) and not bool(user_active_after_end)


class UserTaskPriority:
    """用户手机任务活跃登记表（按会话去重，事件循环线程内使用）。

    一次用户手机任务从 ``begin(session)`` 开始、到 ``end(session)`` 结束；同一个
    会话重复 ``begin`` 是幂等的，不会重复计数；多个会话可以同时活跃，只有全部
    ``end`` 之后才算「无用户任务活跃」。
    """

    def __init__(self) -> None:
        self._sessions: set[str] = set()

    def begin(self, session: object | None) -> bool:
        """登记一个用户手机任务。

        Args:
            session: 会话标识，通常是 ``event.unified_msg_origin``；会做
                ``str(...).strip()`` 归一化。

        Returns:
            本次是否使状态从「无活跃用户任务」变为「有活跃用户任务」；
            同会话重复 ``begin``、空会话（``None``/空串/纯空白）返回 ``False``。
        """
        key = _normalize_session(session)
        if not key:
            return False
        was_active = bool(self._sessions)
        self._sessions.add(key)
        return not was_active

    def end(self, session: object | None) -> bool:
        """结束该会话的用户手机任务。

        Args:
            session: 会话标识，归一化规则与 :meth:`begin` 一致。

        Returns:
            调用结束后是否已无任何活跃用户任务（``True`` 表示可以自动恢复持续刷）；
            未知/空会话不改变状态，返回值仍按当前活跃集合计算。重复 ``end``
            同一个已结束的会话不会报错，也不会影响其他会话。
        """
        key = _normalize_session(session)
        if key:
            self._sessions.discard(key)
        return not self._sessions

    @property
    def active(self) -> bool:
        """是否至少有一个用户手机任务活跃。"""
        return bool(self._sessions)

    @property
    def sessions(self) -> frozenset[str]:
        """当前活跃的用户任务会话集合（只读快照，修改它不影响内部状态）。"""
        return frozenset(self._sessions)

    def clear(self) -> None:
        """清空所有登记（插件停止/重载或用户主动停止时使用）。"""
        self._sessions.clear()

    def __len__(self) -> int:
        return len(self._sessions)

    def __repr__(self) -> str:
        return (
            f"UserTaskPriority(active={self.active}, "
            f"sessions={sorted(self._sessions)!r})"
        )


def _normalize_session(session: object | None) -> str:
    """会话标识归一化：转字符串并去首尾空白；空值返回空串。"""
    return str(session or "").strip()
