"""点赞/收藏/主动评论/被动回复/发帖的许可、限额与记账。

插件不编排任何固定 App 流程：AI 先用 ``phone_interaction_gate`` 申请一个名额，
再自己用低层工具（phone_screen / tap / input_text / key 等）完成主动评论、
被动回复或发帖，最后用 ``phone_interaction_record`` 记录结果。本模块负责：

* 名额预约（pending，落盘 ``interaction_reservations.json``，重启不丢）；
* 每日次数（按本地时区）与冷却校验（主动评论/发帖共用全局冷却，
  被动回复按自身成功记录独立冷却）；
* 成功/失败记录落盘 ``interactions.jsonl``（成功才计当日用量）；
* ``/phone interactions`` 的状态文本。

只依赖标准库，便于本地单测。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KIND_COMMENT = "comment"
KIND_POST = "post"
KIND_PASSIVE_REPLY = "passive_reply"
KIND_LIKE = "like"
KIND_FAVORITE = "favorite"
LIGHT_KINDS = frozenset({KIND_LIKE, KIND_FAVORITE})
RUN_EXTRA = "phone_interaction_run"
KIND_LABELS = {
    KIND_LIKE: "点赞",
    KIND_FAVORITE: "收藏",
    KIND_COMMENT: "评论",
    KIND_POST: "发帖",
    KIND_PASSIVE_REPLY: "被动回复",
}
KIND_ALLOW_KEYS = {
    KIND_LIKE: "security.allow_interactions",
    KIND_FAVORITE: "security.allow_interactions",
    KIND_COMMENT: "security.allow_comments",
    KIND_POST: "security.allow_posts",
    KIND_PASSIVE_REPLY: "security.allow_passive_replies",
}
# kind 别名（已统一转小写后匹配）。reply/回复 归为主动评论；
# 被动回复需显式的 passive 写法，两类语义互不混淆。
_PASSIVE_KIND_ALIASES = frozenset(
    {KIND_PASSIVE_REPLY, "passive", "被动回复", "被动评论", "回评"}
)
_COMMENT_KIND_ALIASES = frozenset(
    {KIND_COMMENT, "评论", "主动评论", "回复", "comment", "reply"}
)
_POST_KIND_ALIASES = frozenset({KIND_POST, "发帖", "帖子", "post", "publish"})
RESERVATIONS_FILENAME = "interaction_reservations.json"
RECORDS_FILENAME = "interactions.jsonl"
DEFAULT_MAX_ENTRIES = 5000
DEFAULT_MAX_SUMMARY_LEN = 120
DEFAULT_PENDING_TTL_SECONDS = 2 * 3600
_MAX_SUMMARY_LEN = DEFAULT_MAX_SUMMARY_LEN
_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_END_RE = re.compile(r"[。！？!?.]$")


class InteractionError(Exception):
    """可预期的互动校验、名额或记录错误（工具层转成 ``error:`` 文本）。"""


def interaction_plan(security):
    """将实际授权与可执行的独立决策入口交给自主 Agent。"""
    states = []
    for kind, enabled, limit in (
        (KIND_COMMENT, security.allow_comments, security.max_comments_per_day),
        (KIND_PASSIVE_REPLY, security.allow_passive_replies, security.max_passive_replies_per_day),
        (KIND_POST, security.allow_posts, security.max_posts_per_day),
    ):
        quota = f"每日上限{limit}条" if limit > 0 else "每日不限次数"
        states.append(f"{kind}：{'已授权，' + quota if enabled else '未授权'}")
    return (
        "【自主互动决策】" + "；".join(states) + "。"
        "进入 App 后，有被动回复授权时先 phone_check_inbox 阅读实际消息并决定是否回应。"
        "理解正文后独立判断是否有具体感想，想评论则 phone_content decide comment；"
        "想回复某条评论则先 read_comments 并阅读目标，再 decide reply_comment(target=具体评论)。"
        "本轮结束前也判断是否有适合当前社区的独立主题值得发帖（post），无需等待用户另外下任务。"
        "只对已授权类型申请 phone_interaction_gate，通过后操作并 phone_interaction_record；"
        "限额与冷却以 gate 实时结果为准，评论/回评/发帖每轮合计最多一条。"
        "没有合适内容可以不发；静默指不向聊天逐步汇报，App 内已授权互动按上述规则独立决定。"
    )


def _now(now: float | None = None) -> float:
    return float(now) if now is not None else time.time()


def _day_key(ts: float) -> tuple[int, int, int]:
    """把时间戳转成本地时区的日期键（与 AstrBot 部署机时区一致）。"""
    local = time.localtime(float(ts))
    return (local.tm_year, local.tm_mon, local.tm_mday)


def normalize_kind(kind: Any) -> str:
    """归一化 kind：支持主动评论、被动回复、发帖及各自的中文别名。"""
    text = str(kind or "").strip().lower()
    if text in {KIND_LIKE, "点赞", "赞"}:
        return KIND_LIKE
    if text in {KIND_FAVORITE, "collect", "收藏"}:
        return KIND_FAVORITE
    if text in _PASSIVE_KIND_ALIASES:
        return KIND_PASSIVE_REPLY
    if text in _COMMENT_KIND_ALIASES:
        return KIND_COMMENT
    if text in _POST_KIND_ALIASES:
        return KIND_POST
    raise InteractionError(
        "kind 必须是 comment（评论/主动评论/回复）、passive_reply（被动回复）"
        "、post（发帖）、like（点赞）或 favorite（收藏）"
    )


def interaction_run(event: Any, *, restart: bool = False) -> str:
    """同一次 Agent 任务共享轮次；直接工具调用共享当前事件的轮次。"""
    value = event.get_extra(RUN_EXTRA)
    if restart or not value:
        value = uuid.uuid4().hex
        event.set_extra(RUN_EXTRA, value)
    return str(value)


def normalize_summary(summary: Any) -> str:
    """归一化并校验 AI 自己写的 summary（去换行、限长 120 字）。"""
    text = _WHITESPACE_RE.sub(" ", str(summary or "")).strip()
    if not text:
        raise InteractionError(
            "summary 不能为空：请用不超过 120 字概括这次评论/回复/发帖的内容或失败原因"
        )
    if len(text) > _MAX_SUMMARY_LEN:
        raise InteractionError(
            f"summary 过长（{len(text)} 字），请压缩到 {_MAX_SUMMARY_LEN} 字以内"
        )
    return text


@dataclass(frozen=True)
class Reservation:
    """一次已预留的互动名额。"""

    kind: str
    app: str
    session: str
    ts: float
    run: str = ""
    id: str = ""


@dataclass(frozen=True)
class GateDecision:
    """gate 的判定结果。"""

    allowed: bool
    message: str
    reservation: Reservation | None = None


@dataclass
class InteractionStore:
    """互动的名额簿与记录存储。"""

    root: str | Path
    max_entries: int = DEFAULT_MAX_ENTRIES
    pending_ttl_seconds: float = DEFAULT_PENDING_TTL_SECONDS
    records_path: Path = field(init=False)
    reservations_path: Path = field(init=False)

    def __post_init__(self) -> None:
        base = Path(self.root)
        self.records_path = base / RECORDS_FILENAME
        self.reservations_path = base / RESERVATIONS_FILENAME
        self.max_entries = max(100, int(self.max_entries))

    # ---- 记录文件 -----------------------------------------------------

    def _read_records(self) -> list[dict[str, Any]]:
        if not self.records_path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            with self.records_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError as exc:
            raise InteractionError("读取互动记录失败") from exc
        return records

    def _write_records(self, records: list[dict[str, Any]]) -> None:
        try:
            self.records_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.records_path.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            os.replace(tmp, self.records_path)
        except OSError as exc:
            raise InteractionError("保存互动记录失败") from exc

    # ---- 预约文件 -----------------------------------------------------

    def _prune_reservations(
        self, reservations: list[Reservation], now: float
    ) -> list[Reservation]:
        return [
            reservation
            for reservation in reservations
            if now - float(reservation.ts) <= self.pending_ttl_seconds
        ]

    def _load_reservations(self, now: float | None = None) -> list[Reservation]:
        current = _now(now)
        if not self.reservations_path.exists():
            return []
        try:
            raw = json.loads(self.reservations_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InteractionError("读取互动预约失败") from exc
        reservations: list[Reservation] = []
        for item in raw.get("reservations", []) if isinstance(raw, dict) else []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            if kind not in KIND_LABELS:
                continue
            try:
                ts = float(item.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            reservations.append(
                Reservation(
                    kind=kind,
                    app=str(item.get("app") or ""),
                    session=str(item.get("session") or ""),
                    ts=ts,
                    run=str(item.get("run") or ""),
                    id=str(item.get("id") or ""),
                )
            )
        return self._prune_reservations(reservations, current)

    def _save_reservations(self, reservations: list[Reservation]) -> None:
        try:
            self.reservations_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "reservations": [
                    {
                        "kind": reservation.kind,
                        "app": reservation.app,
                        "session": reservation.session,
                        "ts": reservation.ts,
                        "run": reservation.run,
                        "id": reservation.id,
                    }
                    for reservation in reservations
                ]
            }
            tmp = self.reservations_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.reservations_path)
        except OSError as exc:
            raise InteractionError("保存互动预约失败") from exc

    @staticmethod
    def _reservation_key(session: str, kind: str) -> str:
        return f"{str(session or '')}|{kind}"

    def reservations(self, now: float | None = None) -> list[Reservation]:
        """当前有效的预约列表。"""
        return self._load_reservations(now)

    def get_reservation(
        self, session: str, kind: str, now: float | None = None
    ) -> Reservation | None:
        """查找某会话某类型的待完成预约。"""
        for reservation in self._load_reservations(now):
            if reservation.session == str(session or "") and reservation.kind == kind:
                return reservation
        return None

    # ---- 预约/释放 ----------------------------------------------------

    def reserve(
        self,
        *,
        kind: str,
        app: str = "",
        session: str = "",
        now: float | None = None,
        run: str = "",
    ) -> Reservation:
        """预约一个名额；同会话同类型已有待完成预约时拒绝。"""
        current = _now(now)
        existing = self.get_reservation(session, kind, current)
        if existing is not None:
            raise InteractionError(
                f"已有待完成的{KIND_LABELS[kind]}名额，请先调用 "
                "phone_interaction_record 记录本轮结果（success=true/false）"
            )
        reservations = self._load_reservations(current)
        reservation = Reservation(
            kind=kind,
            app=str(app or ""),
            session=str(session or ""),
            ts=current,
            run=run,
            id=uuid.uuid4().hex,
        )
        reservations.append(reservation)
        self._save_reservations(reservations)
        return reservation

    def release(
        self, session: str, kind: str, now: float | None = None
    ) -> Reservation | None:
        """释放预约（记录失败或放弃时）；返回被释放的预约。"""
        current = _now(now)
        reservations = self._load_reservations(current)
        kept: list[Reservation] = []
        released: Reservation | None = None
        for reservation in reservations:
            if (
                released is None
                and reservation.session == str(session or "")
                and reservation.kind == kind
            ):
                released = reservation
                continue
            kept.append(reservation)
        if released is not None:
            self._save_reservations(kept)
        return released

    # ---- 统计 ---------------------------------------------------------

    def run_usage(self, run: str, now: float | None = None) -> tuple[int, int]:
        """点赞与收藏共用单轮额度，待完成预约同样占额。"""
        used = sum(1 for r in self._read_records() if r.get("run") == run
                   and r.get("kind") in LIGHT_KINDS and r.get("success") is True)
        pending = sum(1 for r in self._load_reservations(now)
                      if r.run == run and r.kind in LIGHT_KINDS)
        return used, pending

    def finish_run(self, run: str) -> None:
        """任务结束释放本轮尚未完成的点赞与收藏预约。"""
        if run:
            reservations = self._load_reservations()
            kept = [r for r in reservations if r.run != run or r.kind not in LIGHT_KINDS]
            if len(kept) != len(reservations):
                self._save_reservations(kept)

    def used_today(self, kind: str, now: float | None = None) -> int:
        """今日成功次数（本地时区）。"""
        day = _day_key(_now(now))
        return sum(
            1
            for record in self._read_records()
            if record.get("kind") == kind
            and record.get("success") is True
            and _day_key(float(record.get("ts") or 0)) == day
        )

    def pending_count(self, kind: str, now: float | None = None) -> int:
        """今日已预约但未完成的数量。"""
        day = _day_key(_now(now))
        return sum(
            1
            for reservation in self._load_reservations(now)
            if reservation.kind == kind and _day_key(reservation.ts) == day
        )

    def last_success_at(
        self, now: float | None = None, *, kind: str | None = None
    ) -> float | None:
        """最近一次成功互动的时间戳。

        ``kind`` 为空时统计任意类型（主动评论/被动回复/发帖共用的全局冷却）；
        传入具体 kind 时只统计该类型（被动回复据此计算独立冷却）。
        """
        latest: float | None = None
        for record in self._read_records():
            if record.get("success") is not True:
                continue
            if kind is None and record.get("kind") in LIGHT_KINDS:
                continue
            if kind is not None and record.get("kind") != kind:
                continue
            try:
                ts = float(record.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if latest is None or ts > latest:
                latest = ts
        return latest

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        """最近记录（按时间正序）。"""
        count = max(1, int(limit))
        return self._read_records()[-count:]

    # ---- 记录结果 -----------------------------------------------------

    def record_result(
        self,
        *,
        kind: str,
        success: bool,
        summary: Any,
        app: str = "",
        session: str = "",
        now: float | None = None,
        run: str | None = None,
    ) -> dict[str, Any]:
        """记录一次互动结果；成功计一次当日用量，失败释放名额。"""
        current = _now(now)
        reservation = self.get_reservation(session, kind, current)
        if reservation is None:
            raise InteractionError(
                f"没有找到待完成的{KIND_LABELS[kind]}预约；请先调用 "
                "phone_interaction_gate 申请名额，完成后再记录结果"
            )
        if run is not None and reservation.run != run:
            raise InteractionError("互动预约属于其他轮次，请先申请本轮名额")
        text = normalize_summary(summary)
        reservation_id = reservation.id or f"{reservation.session}|{kind}|{reservation.ts}"
        records = self._read_records()
        for record in records:
            if record.get("reservation_id") == reservation_id:
                self.release(session, kind, current)
                return record
        entry = {
            "ts": current,
            "app": str(app or reservation.app or ""),
            "kind": kind,
            "summary": text,
            "session": str(session or ""),
            "success": bool(success),
            "run": reservation.run,
            "reservation_id": reservation_id,
        }
        records.append(entry)
        if len(records) > self.max_entries:
            records = records[-self.max_entries :]
        self._write_records(records)
        self.release(session, kind, current)
        return entry

    # ---- 展示 ---------------------------------------------------------

    def status_text(
        self,
        *,
        allow_comments: bool,
        allow_posts: bool,
        max_comments: int,
        max_posts: int,
        cooldown_seconds: int,
        allow_passive_replies: bool = False,
        max_passive_replies: int = 0,
        passive_cooldown_seconds: int = 0,
        allow_interactions: bool = False,
        max_likes_per_session: int = 5,
        limit: int = 5,
        now: float | None = None,
    ) -> str:
        """``/phone interactions`` 的状态文本。"""
        current = _now(now)
        comment_used = self.used_today(KIND_COMMENT, current)
        post_used = self.used_today(KIND_POST, current)
        passive_used = self.used_today(KIND_PASSIVE_REPLY, current)
        pending = self.reservations(current)
        comment_pending = sum(
            1 for reservation in pending if reservation.kind == KIND_COMMENT
        )
        post_pending = sum(1 for reservation in pending if reservation.kind == KIND_POST)
        passive_pending = sum(
            1 for reservation in pending if reservation.kind == KIND_PASSIVE_REPLY
        )
        def _limit_text(used: int, limit: int) -> str:
            return f"今日已用 {used}，不限次数" if int(limit) <= 0 else f"今日已用 {used}/{limit}"

        def _cooldown_text(seconds: int) -> str:
            return "无冷却" if int(seconds) <= 0 else f"{int(seconds)} 秒"

        lines = [
            "手机互动状态（互动由 AI 自己用低层工具完成，插件只做许可/限额/记账）：",
            (f"- 点赞/收藏：{'已开启' if allow_interactions else '未开启'}"
             f"（security.allow_interactions）；单轮合计上限 {max_likes_per_session} 次；"
             f"今日点赞 {self.used_today(KIND_LIKE, current)}，收藏 {self.used_today(KIND_FAVORITE, current)}"),
            (
                f"- 评论：{'已开启' if allow_comments else '未开启'}"
                f"（security.allow_comments）；{_limit_text(comment_used, max_comments)}"
                + (f"，待完成 {comment_pending}" if comment_pending else "")
            ),
            (
                f"- 被动回复：{'已开启' if allow_passive_replies else '未开启'}"
                "（security.allow_passive_replies）；"
                f"{_limit_text(passive_used, max_passive_replies)}，"
                f"待完成 {passive_pending}；"
                f"独立冷却 {_cooldown_text(passive_cooldown_seconds)}"
            ),
            (
                f"- 发帖：{'已开启' if allow_posts else '未开启'}"
                f"（security.allow_posts）；{_limit_text(post_used, max_posts)}"
                + (f"，待完成 {post_pending}" if post_pending else "")
            ),
            f"- 冷却：两次成功互动间隔 {_cooldown_text(cooldown_seconds)}",
        ]
        last = self.last_success_at(current)
        if last is not None:
            elapsed = max(0.0, current - last)
            lines.append(
                f"- 距上次成功互动：{int(elapsed)} 秒前"
                f"（{time.strftime('%m-%d %H:%M', time.localtime(last))}）"
            )
        records = self.recent(limit)
        if records:
            lines.append("- 最近记录：")
            for record in records:
                when = time.strftime(
                    "%m-%d %H:%M", time.localtime(float(record.get("ts") or 0))
                )
                label = KIND_LABELS.get(str(record.get("kind")), "互动")
                ok = "成功" if record.get("success") is True else "失败"
                summary = str(record.get("summary") or "")
                if len(summary) > 40:
                    summary = summary[:40] + "…"
                app = str(record.get("app") or "").strip()
                app_text = f"[{app}] " if app else ""
                lines.append(f"  · {when} {app_text}{label}{ok}：{summary}")
        else:
            lines.append("- 最近记录：暂无（还没有成功/失败记录）")
        return "\n".join(lines)


def evaluate_gate(
    store: InteractionStore,
    *,
    kind: str,
    allow: bool,
    max_per_day: int,
    cooldown_seconds: int,
    cooldown_kind: str | None = None,
    app: str = "",
    session: str = "",
    now: float | None = None,
    run: str = "",
    max_per_session: int = 5,
) -> GateDecision:
    """按「开关 → 每日上限 → 冷却 → 预约」顺序判定是否发放名额。

    ``cooldown_kind`` 为空时沿用「任意成功互动」的全局冷却（主动评论/发帖）；
    传入具体 kind（如 ``KIND_PASSIVE_REPLY``）时只按该类型的最近成功时间计算
    独立冷却。``max_per_day <= 0`` 表示不限次数、``cooldown_seconds <= 0`` 表示
    无冷却（均为默认值）。token 全局额度与 owner 门禁在工具层更早校验（本函数只
    负责互动规则）。
    """
    label = KIND_LABELS[kind]
    allow_key = KIND_ALLOW_KEYS[kind]
    current = _now(now)
    if not allow:
        return GateDecision(
            False,
            f"未开启{label}，需在插件配置 {allow_key} 打开",
        )
    if kind in LIGHT_KINDS:
        if not run:
            return GateDecision(False, "点赞/收藏需要有效的任务轮次")
        used, pending = store.run_usage(run, current)
        if used + pending >= max(0, int(max_per_session)):
            return GateDecision(False, f"本轮点赞/收藏名额已满（已用 {used}，待完成 {pending}，上限 {max_per_session}）")
        try:
            reservation = store.reserve(kind=kind, app=app, session=session, now=current, run=run)
        except InteractionError as exc:
            return GateDecision(False, str(exc))
        return GateDecision(True, f"已预留 1 次{label}名额（本轮合计剩余 {max_per_session-used-pending-1} 次）。"
                            "请观察按钮状态后操作一次，确认成功后 phone_interaction_record 记录结果；失败或放弃记录 success=false。",
                            reservation)
    try:
        max_per_day = max(0, int(max_per_day))
        cooldown = max(0, int(cooldown_seconds))
    except (TypeError, ValueError):
        max_per_day = 0
        cooldown = 0
    used = store.used_today(kind, current)
    pending = store.pending_count(kind, current)
    # 上限 <= 0 表示不限次数（默认值），只有用户显式配置正数时才执行每日用量限制。
    if max_per_day > 0 and used >= max_per_day:
        return GateDecision(
            False,
            f"今日{label}已达上限 {max_per_day} 次，请明天再试",
        )
    if max_per_day > 0 and used + pending >= max_per_day:
        return GateDecision(
            False,
            (
                f"今日{label}名额已满（已用 {used}，待完成 {pending}，"
                f"上限 {max_per_day}）；请先完成或放弃当前预约"
            ),
        )
    last = store.last_success_at(current, kind=cooldown_kind)
    if last is not None:
        elapsed = current - last
        if cooldown > 0 and elapsed < cooldown:
            remaining = max(0.0, cooldown - elapsed)
            if cooldown_kind is None:
                source_label = "互动"
            else:
                source_label = KIND_LABELS.get(cooldown_kind, label)
            return GateDecision(
                False,
                (
                    f"操作太快：距上次成功{source_label}仅 {int(max(0.0, elapsed))} 秒，"
                    f"{label}需间隔 {cooldown} 秒（还需约 {int(remaining)} 秒）"
                ),
            )
    try:
        reservation = store.reserve(
            kind=kind, app=app, session=session, now=current, run=run
        )
    except InteractionError as exc:
        return GateDecision(False, str(exc))
    remaining = max(0, max_per_day - used - pending - 1)
    quota_text = "不限次数" if max_per_day <= 0 else f"今日剩余 {remaining} 次"
    return GateDecision(
        True,
        (
            f"已预留 1 次{label}名额（{quota_text}）。"
            "请用 phone_status / phone_screen / phone_tap / phone_input_text / "
            "phone_key 等低层工具自己完成操作；完成后调用 phone_interaction_record"
            f"（kind={kind}，success=true/false，summary=不超过 120 字的说明）记录结果。"
            "每轮最多 1 次，不要刷屏。"
        ),
        reservation,
    )
