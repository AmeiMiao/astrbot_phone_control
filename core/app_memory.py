"""App 使用经验记忆：把 App 布局与操作方式沉淀为可复用经验。

AI 在手机上操作 App 时记录界面布局、按钮位置与操作步骤；后续操作同一 App 时
先注入这些经验，减少重复探索。界面改版后按新界面覆盖同 (App, 页面/场景) 的
经验，版本号随覆盖递增。

存储结构：``root/app_memory.json``（UTF-8 JSON 对象）：:

    {
      "version": 1,
      "entries": [
        {
          "app": "com.ss.android.ugc.aweme",
          "label": "抖音",
          "page": "发布",
          "note": "右下角 + 号进入发布页，相册在第一行第二个格子",
          "steps": ["点击右下角 + 号", "选择相册"],
          "updated_at": 1760000000.0,
          "hits": 3,
          "version": 2
        }
      ]
    }

App 标识归一化：优先用 :func:`core.apps.resolve_package` 解析出的包名（小写），
解析不到时用去掉首尾空白并小写化的输入；可读名优先内置中文名，其次调用方
``label``，最后回退原输入。

经验按 ``(app, page)`` 覆盖写入；查询命中累加 ``hits``；超过 ``max_entries``
按 ``updated_at`` 淘汰最旧条目。本模块只依赖标准库与 :mod:`core.apps` 纯函数，
不导入 astrbot；除参数校验抛 :class:`AppMemoryError` 外，读写异常都吞掉，避免
影响手机主流程。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .apps import app_label, app_label_for_package, resolve_app, resolve_package
from .errors import PhoneControlError

MEMORY_FILENAME = "app_memory.json"
DEFAULT_MAX_ENTRIES = 500
MIN_NOTE_CHARS = 1
MAX_NOTE_CHARS = 500
MAX_STEPS = 20
MAX_STEP_CHARS = 200
MAX_PAGE_CHARS = 100
DEFAULT_LOOKUP_LIMIT = 5
DEFAULT_RENDER_LIMIT = 3
DEFAULT_RENDER_CHARS = 600
DEFAULT_PROMPT_LIMIT = 5
DEFAULT_PROMPT_CHARS = 900
RENDER_NOTE_CHARS = 120
RENDER_STEP_CHARS = 60
PROMPT_NOTE_CHARS = 90
PROMPT_STEP_CHARS = 50
PROMPT_STEPS_SHOWN = 3
PROMPT_ENTRIES_PER_APP = 2


class AppMemoryError(PhoneControlError):
    """App 使用经验的参数/存储错误。"""


@dataclass(frozen=True)
class AppMemoryEntry:
    """一条 App 使用经验（不可变，更新时整体替换）。"""

    app: str
    """归一化后的 App 标识：优先包名小写，解析不到时为输入小写。"""
    label: str
    """可读名，如「抖音」。"""
    page: str
    """页面/场景；空串表示该 App 的通用经验。"""
    note: str
    """经验正文（压缩空白后 1-500 字）。"""
    steps: tuple[str, ...] = ()
    """操作步骤（最多 20 条，单条最多 200 字）。"""
    updated_at: float = 0.0
    """最后更新时间戳（秒）。"""
    hits: int = 0
    """被查询命中并注入提示词的次数。"""
    version: int = 1
    """覆盖更新版本号；同一 (app, page) 每次覆盖 +1。"""

    def as_dict(self) -> dict[str, Any]:
        """转为可直接 JSON 序列化的字典。"""
        return {
            "app": self.app,
            "label": self.label,
            "page": self.page,
            "note": self.note,
            "steps": list(self.steps),
            "updated_at": self.updated_at,
            "hits": self.hits,
            "version": self.version,
        }


def _coerce_float(value: Any, default: float = 0.0) -> float:
    """把任意值转成 float；非法值回退默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int = 0) -> int:
    """把任意值转成 int；非法值回退默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_limit(limit: Any, default: int) -> int:
    """把条数限制收敛为非负整数；非法值回退默认值。"""
    if limit is None:
        return max(0, int(default))
    try:
        return max(0, int(limit))
    except (TypeError, ValueError):
        return max(0, int(default))


def _clip(text: str, max_chars: int) -> str:
    """超长文本截断并加省略号。"""
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def _truncate(text: str, max_chars: Any) -> str:
    """整段文本按 ``max_chars`` 截断；``max_chars`` 非法或 <=0 时不截断。"""
    try:
        limit_chars = int(max_chars)
    except (TypeError, ValueError):
        return text
    if limit_chars > 0 and len(text) > limit_chars:
        return text[: max(0, limit_chars - 1)] + "…"
    return text


def _format_time(ts: float) -> str:
    """把时间戳格式化为「MM-DD HH:MM」；非法值返回「未知时间」。"""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (OverflowError, OSError, ValueError):
        return "未知时间"


def _normalize_page(page: Any) -> str:
    """归一化页面/场景：压缩空白，超长截断，空串表示通用经验。"""
    text = " ".join(str(page or "").split())
    return _clip(text, MAX_PAGE_CHARS)


def _normalize_note(note: Any) -> str:
    """校验并归一化经验正文：压缩空白后 1-500 字，非法时抛 :class:`AppMemoryError`。"""
    text = " ".join(str(note or "").split())
    if not text:
        raise AppMemoryError(
            f"App 使用经验不能为空：请用 {MIN_NOTE_CHARS}-{MAX_NOTE_CHARS} 字记录布局/操作方式"
        )
    if len(text) > MAX_NOTE_CHARS:
        raise AppMemoryError(
            f"App 使用经验过长（{len(text)} 字），请压缩到 {MAX_NOTE_CHARS} 字以内"
        )
    return text


def _normalize_steps(steps: Any) -> tuple[str, ...]:
    """归一化操作步骤：最多 20 条、单条最多 200 字，空步骤丢弃。"""
    if steps is None:
        return ()
    if isinstance(steps, str):
        raw: Iterable[Any] = (
            steps.replace("；", "\n").replace(";", "\n").splitlines()
        )
    elif isinstance(steps, (list, tuple, set, frozenset)):
        raw = steps
    else:
        return ()
    result: list[str] = []
    for item in raw:
        text = " ".join(str(item or "").split())
        if not text:
            continue
        result.append(_clip(text, MAX_STEP_CHARS))
        if len(result) >= MAX_STEPS:
            break
    return tuple(result)


def _normalize_app(name: Any, label_hint: Any = "") -> tuple[str, str] | None:
    """归一化 App 标识，返回 ``(小写 key, 可读名)``；名称为空时返回 None。

    key 优先取解析出的包名小写，解析不到时用输入小写；可读名优先内置中文名，
    其次调用方 ``label_hint``，最后回退原输入。
    """
    text = str(name or "").strip()
    if not text:
        return None
    package = resolve_package(text) or ""
    key = package.lower() if package else text.lower()

    display = app_label_for_package(package) if package else ""
    if not display:
        profile = resolve_app(text)
        if profile is not None and profile.label:
            candidate = profile.label.strip()
            # generic 画像的 label 就是包名本身，不能当可读名用
            if candidate and candidate.lower() not in {text.lower(), key}:
                display = candidate
    if not display:
        display = str(label_hint or "").strip()
    if not display:
        display = app_label(text) or text
    return key, display


def _recency_key(entry: AppMemoryEntry) -> tuple[float, int, str, str]:
    """排序/淘汰用的键：更新越晚越新，版本号大者优先，其余保证确定性。"""
    return (entry.updated_at, entry.version, entry.app, entry.page)


def _entry_from_dict(raw: Any) -> AppMemoryEntry | None:
    """把 JSON 里的一条经验还原为 :class:`AppMemoryEntry`；不可用返回 None。"""
    if not isinstance(raw, dict):
        return None
    app = str(raw.get("app") or "").strip().lower()
    note = " ".join(str(raw.get("note") or "").split())
    if not app or not note or len(note) > MAX_NOTE_CHARS:
        return None
    label = str(raw.get("label") or "").strip() or app
    return AppMemoryEntry(
        app=app,
        label=label,
        page=_normalize_page(raw.get("page")),
        note=note,
        steps=_normalize_steps(raw.get("steps")),
        updated_at=_coerce_float(raw.get("updated_at")),
        hits=max(0, _coerce_int(raw.get("hits"))),
        version=max(1, _coerce_int(raw.get("version"), 1)),
    )


class AppMemoryStore:
    """基于 JSON 文件的 App 使用经验档案（线程安全、原子写、异常吞掉）。"""

    def __init__(
        self,
        root: str | Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        filename: str = MEMORY_FILENAME,
    ) -> None:
        """初始化经验档案。

        Args:
            root: 数据目录，档案写入 ``root/app_memory.json``。
            max_entries: 最多保留的经验条数（<=0 或非法时回退默认值 500）。
            filename: 档案文件名（默认 ``app_memory.json``）。
        """
        self.root = Path(root)
        try:
            self.max_entries = int(max_entries)
        except (TypeError, ValueError):
            self.max_entries = DEFAULT_MAX_ENTRIES
        if self.max_entries <= 0:
            self.max_entries = DEFAULT_MAX_ENTRIES
        name = str(filename or "").strip() or MEMORY_FILENAME
        self.path = self.root / name
        self._lock = threading.RLock()
        self._entries: list[AppMemoryEntry] = []
        self._load()

    # ---- 落盘/加载 ----------------------------------------------------

    def _load(self) -> None:
        """从磁盘读取经验；文件缺失/损坏/不可读时保持空档案。"""
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return
        raw_entries = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(raw_entries, list):
            return
        loaded: dict[tuple[str, str], AppMemoryEntry] = {}
        for raw in raw_entries:
            entry = _entry_from_dict(raw)
            if entry is None:
                continue
            dedupe_key = (entry.app, entry.page)
            previous = loaded.get(dedupe_key)
            # 同 (app, page) 只保留更新时间较晚的一条
            if previous is None or entry.updated_at >= previous.updated_at:
                loaded[dedupe_key] = entry
        entries = sorted(loaded.values(), key=_recency_key, reverse=True)
        self._entries = entries[: self.max_entries]

    def _save_locked(self) -> None:
        """原子写入整份档案（调用方须持锁）；异常吞掉不影响内存状态。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "entries": [entry.as_dict() for entry in self._entries],
            }
            tmp = self.path.with_name(self.path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.write("\n")
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError):
            return

    def _evict_oldest_locked(self) -> None:
        """按 ``updated_at`` 淘汰最旧的一条（调用方须持锁）。"""
        if not self._entries:
            return
        index = min(range(len(self._entries)), key=lambda i: _recency_key(self._entries[i]))
        del self._entries[index]

    def _find_locked(self, app: str, page: str) -> int | None:
        """查找同 (app, page) 的经验下标（调用方须持锁）。"""
        for index, entry in enumerate(self._entries):
            if entry.app == app and entry.page == page:
                return index
        return None

    # ---- 记录 ---------------------------------------------------------

    def remember(
        self,
        *,
        app: Any,
        note: Any,
        page: Any = "",
        label: Any = "",
        steps: Any = None,
        ts: Any = None,
    ) -> AppMemoryEntry:
        """新增或覆盖一条 App 使用经验并返回它。

        同一 ``(app, page)`` 再次写入时覆盖正文与步骤，版本号 +1 且保留 hits。

        Args:
            app: App 名称（中文名/别名/包名均可），必填。
            note: 经验正文，压缩空白后须为 1-500 字。
            page: 页面/场景；空串表示该 App 的通用经验。
            label: 可读名回退值（内置中文名优先）。
            steps: 操作步骤（列表，或分号/换行分隔的字符串；最多 20 条）。
            ts: 更新时间戳，缺省为当前时间；非法值回退当前时间。

        Raises:
            AppMemoryError: App 名称为空、经验正文为空或超过 500 字。
        """
        resolved = _normalize_app(app, label)
        if resolved is None:
            raise AppMemoryError("App 名称不能为空：请传中文名、别名或包名")
        key, display = resolved
        text = _normalize_note(note)
        page_text = _normalize_page(page)
        step_items = _normalize_steps(steps)
        try:
            when = float(ts) if ts is not None else time.time()
        except (TypeError, ValueError):
            when = time.time()

        with self._lock:
            index = self._find_locked(key, page_text)
            if index is None:
                entry = AppMemoryEntry(
                    app=key,
                    label=display,
                    page=page_text,
                    note=text,
                    steps=step_items,
                    updated_at=when,
                    hits=0,
                    version=1,
                )
                self._entries.append(entry)
                while len(self._entries) > self.max_entries:
                    self._evict_oldest_locked()
            else:
                previous = self._entries[index]
                entry = replace(
                    previous,
                    label=display,
                    note=text,
                    steps=step_items,
                    updated_at=when,
                    version=previous.version + 1,
                )
                self._entries[index] = entry
            self._save_locked()
            return entry

    # ---- 查询 ---------------------------------------------------------

    def lookup(
        self,
        app: Any,
        *,
        page: Any = "",
        limit: Any = DEFAULT_LOOKUP_LIMIT,
    ) -> list[AppMemoryEntry]:
        """查询某个 App 的使用经验，命中项累加 hits 并落盘。

        Args:
            app: App 名称（中文名/别名/包名均可）。
            page: 指定页面/场景时精确匹配的条目排在最前，其余按更新时间倒序。
            limit: 最多返回条数。

        Returns:
            经验条目列表（含本次累加后的 hits）；无记录时返回空列表。
        """
        resolved = _normalize_app(app)
        if resolved is None:
            return []
        key = resolved[0]
        page_text = _normalize_page(page)
        count = _coerce_limit(limit, DEFAULT_LOOKUP_LIMIT)
        if count == 0:
            return []
        with self._lock:
            matched = [entry for entry in self._entries if entry.app == key]
            if not matched:
                return []
            matched.sort(key=_recency_key, reverse=True)
            if page_text:
                exact = [entry for entry in matched if entry.page == page_text]
                rest = [entry for entry in matched if entry.page != page_text]
                matched = exact + rest
            chosen = matched[:count]
            bumped = [replace(entry, hits=entry.hits + 1) for entry in chosen]
            if bumped:
                replaced = {(entry.app, entry.page): entry for entry in bumped}
                self._entries = [
                    replaced.get((entry.app, entry.page), entry) for entry in self._entries
                ]
                self._save_locked()
            return bumped

    # ---- 渲染 ---------------------------------------------------------

    def render_app(
        self,
        app: Any,
        *,
        limit: Any = DEFAULT_RENDER_LIMIT,
        max_chars: Any = DEFAULT_RENDER_CHARS,
    ) -> str:
        """渲染单个 App 的使用经验（页面、更新时间、次数、步骤）。

        Args:
            app: App 名称（中文名/别名/包名均可）。
            limit: 最多渲染条数（按更新时间倒序）。
            max_chars: 整段文本最大字符数（超出截断并加省略号）。

        Returns:
            以「【App 名 使用经验】」开头的多行文本；无记录时返回空串。
        """
        resolved = _normalize_app(app)
        if resolved is None:
            return ""
        key = resolved[0]
        count = _coerce_limit(limit, DEFAULT_RENDER_LIMIT)
        if count == 0:
            return ""
        with self._lock:
            matched = [entry for entry in self._entries if entry.app == key]
        if not matched:
            return ""
        matched.sort(key=_recency_key, reverse=True)
        label = matched[0].label or matched[0].app
        lines = [f"【{label} 使用经验】"]
        for entry in matched[:count]:
            scene = f"（{entry.page}）" if entry.page else "（通用）"
            note = _clip(entry.note, RENDER_NOTE_CHARS)
            when = _format_time(entry.updated_at)
            lines.append(f"- {scene}（{when}，已用 {entry.hits} 次）{note}")
            if entry.steps:
                steps = "；".join(
                    _clip(step, RENDER_STEP_CHARS) for step in entry.steps
                )
                lines.append(f"  步骤：{steps}")
        return _truncate("\n".join(lines), max_chars)

    def render_for_prompt(
        self,
        *,
        limit: Any = DEFAULT_PROMPT_LIMIT,
        max_chars: Any = DEFAULT_PROMPT_CHARS,
    ) -> str:
        """渲染可注入本轮请求临时内容块的 App 使用经验摘要。

        按最近更新的 App 排序，每个 App 取最近的 1-2 条经验，拼成
        「【App 使用经验】」开头的多行中文文本。

        Args:
            limit: 最多渲染的 App 数量。
            max_chars: 整段摘要最大字符数（超出截断并加省略号）。

        Returns:
            无记录时返回空串。
        """
        count = _coerce_limit(limit, DEFAULT_PROMPT_LIMIT)
        if count == 0:
            return ""
        with self._lock:
            items = list(self._entries)
        if not items:
            return ""
        grouped: dict[str, list[AppMemoryEntry]] = {}
        for entry in items:
            grouped.setdefault(entry.app, []).append(entry)
        ranked = sorted(
            grouped.values(),
            key=lambda group: max(_recency_key(entry) for entry in group),
            reverse=True,
        )
        lines = ["【App 使用经验】"]
        for group in ranked[:count]:
            group.sort(key=_recency_key, reverse=True)
            for entry in group[:PROMPT_ENTRIES_PER_APP]:
                label = entry.label or entry.app
                scene = f"（{entry.page}）" if entry.page else ""
                note = _clip(entry.note, PROMPT_NOTE_CHARS)
                line = f"- {label}{scene}：{note}"
                if entry.steps:
                    shown = " → ".join(
                        _clip(step, PROMPT_STEP_CHARS)
                        for step in entry.steps[:PROMPT_STEPS_SHOWN]
                    )
                    line += f"；步骤：{shown}"
                    if len(entry.steps) > PROMPT_STEPS_SHOWN:
                        line += " …"
                lines.append(line)
        return _truncate("\n".join(lines), max_chars)

    # ---- 维护 ---------------------------------------------------------

    def forget(self, app: Any, *, page: Any = None) -> int:
        """删除某个 App 的使用经验。

        Args:
            app: App 名称（中文名/别名/包名均可）。
            page: 指定页面/场景时只删该页面；``None`` 表示删除该 App 全部经验。

        Returns:
            实际删除的条数。
        """
        resolved = _normalize_app(app)
        if resolved is None:
            return 0
        key = resolved[0]
        page_text = None if page is None else _normalize_page(page)
        with self._lock:
            if page_text is None:
                kept = [entry for entry in self._entries if entry.app != key]
            else:
                kept = [
                    entry
                    for entry in self._entries
                    if not (entry.app == key and entry.page == page_text)
                ]
            removed = len(self._entries) - len(kept)
            if removed:
                self._entries = kept
                self._save_locked()
            return removed

    def entries(self) -> list[AppMemoryEntry]:
        """返回全部经验，按更新时间倒序（最新在前）。"""
        with self._lock:
            items = list(self._entries)
        return sorted(items, key=_recency_key, reverse=True)

    def stats(self) -> dict[str, Any]:
        """返回档案统计信息：条目数、App 数、容量上限与文件路径。"""
        with self._lock:
            items = list(self._entries)
        return {
            "entries": len(items),
            "apps": len({entry.app for entry in items}),
            "max_entries": self.max_entries,
            "path": str(self.path),
        }
