"""浏览日志：把每次信息流采集写入本地 JSONL，供聊天汇报与提示词召回。

存储结构：``root/diary.jsonl``，每行一个条目（UTF-8 JSON）：:

    {"id": "...", "ts": 1760000000.0, "app": "抖音",
     "items": [{"title": "...", "author": "...", "text": "...",
                "signature": "...", "screenshot": "..."}],
     "note": "", "summary": "...",
     "round_id": "轮次编号", "round_kind": "user|command|autonomous"}

条目超过上限时整体重写，只保留最近条目；最近 ``keep_rounds`` 轮任务产生的
日志受保护，即使这些轮次的日志总量超过上限也不清理。读写统一加 asyncio 锁，
避免并发命令把文件写坏。除日志统一使用 ``from astrbot.api import logger``
外只依赖标准库。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from functools import wraps
import os
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import BrowseResult, FeedItem
from .errors import PhoneControlError


class DiaryError(PhoneControlError):
    """浏览日志读取或删除失败。"""


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._io_lock:
            return method(self, *args, **kwargs)
    return call

DIARY_FILENAME = "diary.jsonl"
DEFAULT_MAX_ENTRIES = 500


def _entry_id(ts: float) -> str:
    return f"{int(ts * 1000):013d}-{uuid.uuid4().hex[:6]}"


def _item_text(item: FeedItem, *, max_chars: int = 0) -> str:
    text = " ".join(part for part in item.texts if part).strip()
    if not text:
        text = item.title or ""
    if max_chars > 0 and len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def _round_id(record: dict[str, Any]) -> str:
    """读取条目的轮次编号（旧条目没有该字段时为空串）。"""
    value = record.get("round_id")
    return str(value) if isinstance(value, str) else ""


def protected_round_ids(
    entries: list[dict[str, Any]], keep_rounds: int
) -> list[str]:
    """返回最近 ``keep_rounds`` 轮任务的编号（按条目时间顺序从新到旧）。

    只统计带 ``round_id`` 的条目；旧数据/无轮次条目不受保护。
    """
    try:
        count = max(0, int(keep_rounds))
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return []
    rounds: list[str] = []
    for record in reversed(entries):
        round_id = _round_id(record)
        if round_id and round_id not in rounds:
            rounds.append(round_id)
            if len(rounds) >= count:
                break
    return rounds


def trim_entries(
    entries: list[dict[str, Any]],
    max_entries: int,
    keep_rounds: int = 0,
) -> list[dict[str, Any]]:
    """按上限清理旧日志，并保留最近 ``keep_rounds`` 轮任务的日志。

    Args:
        entries: 按写入顺序（旧→新）排列的条目。
        max_entries: 最多保留条数；<=0 时回退默认值。
        keep_rounds: 保护最近多少轮任务；0 表示不做轮次保护。

    Returns:
        清理后的条目列表（保持原顺序）。受保护的轮次日志不会被删除，因此
        返回列表长度可能大于 ``max_entries``。
    """
    try:
        limit = int(max_entries)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_ENTRIES
    if limit <= 0:
        limit = DEFAULT_MAX_ENTRIES
    if len(entries) <= limit:
        return list(entries)
    protected = set(protected_round_ids(entries, keep_rounds))
    if not protected:
        return list(entries[-limit:])
    kept: list[dict[str, Any]] = []
    for index, record in enumerate(entries):
        if len(entries) - index <= limit:
            kept.append(record)
            continue
        # 超出上限的旧条目：受保护轮次保留，其余丢弃。
        if _round_id(record) in protected:
            kept.append(record)
    return kept


class PhoneDiary:
    """基于 JSONL 的浏览日志。"""

    def __init__(self, root: str | Path, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        """初始化日记。

        Args:
            root: 数据目录，日记文件写入 ``root/diary.jsonl``。
            max_entries: 最多保留的条目数（<=0 时回退默认值）。
        """
        self.root = Path(root)
        self.max_entries = max_entries if max_entries > 0 else DEFAULT_MAX_ENTRIES
        self.path = self.root / DIARY_FILENAME
        self._lock = asyncio.Lock()
        self._io_lock = threading.RLock()

    # ---- 内部读写 -----------------------------------------------------

    def _read_entries_unlocked(self, *, strict=False) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        if strict:
                            raise DiaryError("浏览日志含损坏记录，请检查存储")
                        logger.warning("忽略日记中无法解析的行：%s", line[:80])
                        continue
                    if isinstance(record, dict):
                        if not isinstance(record.get("id"), str) or not record["id"]:
                            ts = float(record.get("ts") or 0)
                            app = " ".join(str(record.get("app") or "").split()) or "未知 App"
                            base = hashlib.sha256(f"{ts}:{app}".encode()).hexdigest()[:16]
                            used = {e["id"] for e in entries}
                            key = base
                            n = 0
                            while key in used:
                                n += 1
                                key = f"{base}-{n}"
                            record["id"] = key
                        entries.append(record)
                    elif strict:
                        raise DiaryError("浏览日志含非法记录，请检查存储")
        except OSError as exc:
            if strict:
                raise DiaryError("浏览日志读取失败，请检查存储") from exc
            logger.warning("读取浏览日志失败：%s", exc)
            return []
        return entries

    def _write_entries_unlocked(self, entries: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".jsonl.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for record in entries:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(tmp_path, self.path)

    # ---- 对外接口 -----------------------------------------------------

    @_serialized
    def add_browse(
        self,
        result: BrowseResult,
        *,
        note: str = "",
        round_id: str = "",
        round_kind: str = "",
        max_entries: int | None = None,
        keep_rounds: int = 0,
    ) -> str:
        """追加一条浏览日志并在超过上限时清理旧日志。

        Args:
            result: 一次浏览的结果。
            note: 附加备注（可为空）。
            round_id: 任务轮次编号；用于清理保护。
            round_kind: 轮次来源（user/command/autonomous 等）。
            max_entries: 本次生效的日志上限；为空时用实例默认值。
            keep_rounds: 保护最近多少轮任务的日志。

        Returns:
            新条目的 id。
        """
        ts = time.time()
        entry_id = _entry_id(ts)
        items = [
            {
                "title": item.title,
                "author": item.author,
                "text": _item_text(item),
                "signature": item.signature,
                "screenshot": item.screenshot_path,
            }
            for item in result.items
        ]
        record = {
            "id": entry_id,
            "ts": ts,
            "app": result.app,
            "items": items,
            "note": str(note or "").strip(),
            "summary": result.summary_text(),
        }
        if str(round_id or "").strip():
            record["round_id"] = str(round_id).strip()
            record["round_kind"] = str(round_kind or "").strip()
        entries = self._read_entries_unlocked()
        entries.append(record)
        limit = self.max_entries if max_entries is None else max_entries
        entries = trim_entries(entries, limit, keep_rounds)
        self._write_entries_unlocked(entries)
        return entry_id

    async def add_browse_async(
        self,
        result: BrowseResult,
        *,
        note: str = "",
        round_id: str = "",
        round_kind: str = "",
        max_entries: int | None = None,
        keep_rounds: int = 0,
    ) -> str:
        """``add_browse`` 的加锁版本（运行在事件循环中）。"""
        async with self._lock:
            return await asyncio.to_thread(
                self.add_browse,
                result,
                note=note,
                round_id=round_id,
                round_kind=round_kind,
                max_entries=max_entries,
                keep_rounds=keep_rounds,
            )

    @_serialized
    def delete(self, entry_id, *, commit=None):
        """删除指定日志；持久化保留记录编号，截图文件独立保留。"""
        entries = self._read_entries_unlocked(strict=True)
        remaining = [entry for entry in entries if entry["id"] != entry_id]
        if len(remaining) == len(entries):
            raise DiaryError("浏览日志不存在，请刷新列表")
        writer = lambda: self._write_entries_unlocked(remaining)
        if commit is None:
            writer()
        else:
            commit(writer)

    async def delete_async(self, entry_id, *, commit=None):
        async with self._lock:
            await asyncio.to_thread(self.delete, entry_id, commit=commit)

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        """返回最近的条目（新在前）。"""
        count = max(0, int(limit))
        if count == 0:
            return []
        entries = self._read_entries_unlocked()
        return list(reversed(entries[-count:]))

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """按关键词检索日记（标题/作者/正文/备注/摘要，大小写不敏感）。"""
        needle = str(query or "").strip().lower()
        count = max(0, int(limit))
        if count == 0:
            return []
        entries = self._read_entries_unlocked()
        if not needle:
            return list(reversed(entries[-count:]))
        matched: list[dict[str, Any]] = []
        for record in reversed(entries):
            haystack = self._search_text(record)
            if needle in haystack:
                matched.append(record)
                if len(matched) >= count:
                    break
        return matched

    async def search_async(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """``search`` 的加锁版本（运行在事件循环中）。"""
        async with self._lock:
            return await asyncio.to_thread(self.search, query, limit=limit)

    @staticmethod
    def _search_text(record: dict[str, Any]) -> str:
        parts: list[str] = [
            str(record.get("app") or ""),
            str(record.get("note") or ""),
            str(record.get("summary") or ""),
        ]
        items = record.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                parts.extend(
                    str(item.get(key) or "")
                    for key in ("title", "author", "text")
                )
        return "\n".join(parts).lower()

    def render_for_prompt(self, limit: int = 5, *, max_chars: int = 1200) -> str:
        """把最近条目渲染成适合作为本轮请求临时内容块的中文摘要。"""
        entries = self.recent(limit)
        if not entries:
            return ""
        lines: list[str] = ["【最近浏览记忆】"]
        for record in entries:
            app = str(record.get("app") or "未知")
            when = time.strftime("%m-%d %H:%M", time.localtime(float(record.get("ts") or 0)))
            titles = [
                str(item.get("title") or "").strip()
                for item in record.get("items") or []
                if isinstance(item, dict)
            ]
            titles = [title for title in titles if title]
            head = f"- [{when}] {app}：" + ("；".join(titles[:3]) if titles else "（无文本条目）")
            note = str(record.get("note") or "").strip()
            if note:
                head += f"（备注：{note}）"
            lines.append(head)
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max(0, max_chars - 1)] + "…"
        return text

    @_serialized
    def apply_limits(self, *, max_entries: int, keep_rounds: int = 0) -> dict[str, Any]:
        """按配置清理一次日志（插件启动/配置变更时调用）。

        Returns:
            ``{"before": 旧条数, "after": 新条数, "removed": 删除条数,
            "protected_rounds": [...]}``。
        """
        entries = self._read_entries_unlocked()
        before = len(entries)
        trimmed = trim_entries(entries, max_entries, keep_rounds)
        if trimmed != entries:
            self._write_entries_unlocked(trimmed)
        return {
            "before": before,
            "after": len(trimmed),
            "removed": max(0, before - len(trimmed)),
            "protected_rounds": protected_round_ids(entries, keep_rounds),
        }

    async def apply_limits_async(
        self, *, max_entries: int, keep_rounds: int = 0
    ) -> dict[str, Any]:
        """``apply_limits`` 的加锁版本。"""
        async with self._lock:
            return await asyncio.to_thread(
                self.apply_limits,
                max_entries=max_entries,
                keep_rounds=keep_rounds,
            )

    def page(
        self,
        query: str = "",
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页读取浏览日志（最新在前），供 Dashboard「浏览日志」页使用。

        Args:
            query: 关键词（标题/作者/正文/备注/摘要，大小写不敏感）；空串返回全部。
            limit: 每页条数（上限 100）。
            offset: 偏移量。

        Returns:
            ``{"query","limit","offset","total","entries"}``；条目保持原始结构
            但去除截图绝对路径等与页面无关的字段。
        """
        needle = str(query or "").strip().lower()
        try:
            count = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            count = 20
        try:
            start = max(0, int(offset))
        except (TypeError, ValueError):
            start = 0
        entries = self._read_entries_unlocked()
        if needle:
            matched = [
                record
                for record in reversed(entries)
                if needle in self._search_text(record)
            ]
        else:
            matched = list(reversed(entries))
        page_entries = matched[start : start + count]
        items: list[dict[str, Any]] = []
        for record in page_entries:
            items.append(
                {
                    "id": record.get("id") or "",
                    "ts": record.get("ts") or 0,
                    "app": record.get("app") or "",
                    "note": record.get("note") or "",
                    "summary": record.get("summary") or "",
                    "round_id": record.get("round_id") or "",
                    "round_kind": record.get("round_kind") or "",
                    "items": [
                        {
                            "title": item.get("title") or "",
                            "author": item.get("author") or "",
                            "text": item.get("text") or "",
                        }
                        for item in (record.get("items") or [])
                        if isinstance(item, dict)
                    ],
                }
            )
        return {
            "query": str(query or "").strip(),
            "limit": count,
            "offset": start,
            "total": len(matched),
            "entries": items,
        }

    async def page_async(
        self, query: str = "", *, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        """``page`` 的加锁版本（运行在事件循环中）。"""
        async with self._lock:
            return await asyncio.to_thread(
                self.page, query, limit=limit, offset=offset
            )

    def stats(self) -> dict[str, Any]:
        """返回日志统计信息。"""
        entries = self._read_entries_unlocked()
        apps: dict[str, int] = {}
        item_count = 0
        last_ts = 0.0
        for record in entries:
            app = str(record.get("app") or "未知")
            apps[app] = apps.get(app, 0) + 1
            items = record.get("items")
            if isinstance(items, list):
                item_count += len(items)
            try:
                last_ts = max(last_ts, float(record.get("ts") or 0))
            except (TypeError, ValueError):
                continue
        return {
            "entries": len(entries),
            "items": item_count,
            "apps": apps,
            "last_ts": last_ts,
            "max_entries": self.max_entries,
            "path": str(self.path),
        }

    def all_entries(self) -> Iterable[dict[str, Any]]:
        """按写入顺序返回全部条目（内部/调试用）。"""
        return self._read_entries_unlocked()
