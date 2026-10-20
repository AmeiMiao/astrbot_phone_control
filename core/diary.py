"""浏览日记：把每次信息流采集写入本地 JSONL，供聊天汇报与提示词召回。

存储结构：``root/diary.jsonl``，每行一个条目（UTF-8 JSON）：:

    {"id": "...", "ts": 1760000000.0, "app": "抖音",
     "items": [{"title": "...", "author": "...", "text": "...",
                "signature": "...", "screenshot": "..."}],
     "note": "", "summary": "..."}

条目超过 ``max_entries`` 时整体重写，只保留最近条目。读写统一加 asyncio 锁，
避免并发命令把文件写坏。除日志统一使用 ``from astrbot.api import logger``
外只依赖标准库。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import BrowseResult, FeedItem

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


class PhoneDiary:
    """基于 JSONL 的浏览日记。"""

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

    # ---- 内部读写 -----------------------------------------------------

    def _read_entries_unlocked(self) -> list[dict[str, Any]]:
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
                        logger.warning("忽略日记中无法解析的行：%s", line[:80])
                        continue
                    if isinstance(record, dict):
                        entries.append(record)
        except OSError as exc:
            logger.warning("读取浏览日记失败：%s", exc)
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

    def add_browse(self, result: BrowseResult, *, note: str = "") -> str:
        """追加一条浏览记录。

        Args:
            result: 一次浏览的结果。
            note: 附加备注（可为空）。

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
        entries = self._read_entries_unlocked()
        entries.append(record)
        if len(entries) > self.max_entries:
            entries = entries[-self.max_entries :]
        self._write_entries_unlocked(entries)
        return entry_id

    async def add_browse_async(self, result: BrowseResult, *, note: str = "") -> str:
        """``add_browse`` 的加锁版本（运行在事件循环中）。"""
        async with self._lock:
            return await asyncio.to_thread(self.add_browse, result, note=note)

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

    def stats(self) -> dict[str, Any]:
        """返回日记统计信息。"""
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
