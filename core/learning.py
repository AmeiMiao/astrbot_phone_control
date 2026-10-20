"""持续刷手机的学习档案：把每轮真正学到的内容写入本地 JSONL。

存储结构：``root/learning.jsonl``，每行一个条目（UTF-8 JSON）：:

    {"ts": 1760000000.0, "content": "……", "source": "哔哩哔哩", "tags": ["AI"]}

条目超过 ``max_entries``（默认 2000）时整体重写，只保留最近条目。查询/渲染
结果按时间正序返回，单条内容渲染最多 120 字。本模块只依赖标准库；除内容校验
抛 :class:`LearningError` 外，读写异常都吞掉并返回空结果，避免影响手机主流程。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import PhoneControlError

LEARNING_FILENAME = "learning.jsonl"
DEFAULT_MAX_ENTRIES = 2000
MIN_CONTENT_CHARS = 1
MAX_CONTENT_CHARS = 500
RENDER_CONTENT_CHARS = 120
DEFAULT_RENDER_CHARS = 1200
MAX_TAGS = 10
MAX_TAG_CHARS = 50


class LearningError(PhoneControlError):
    """学习内容的参数/存储错误。"""


def _normalize_content(content: Any) -> str:
    """校验并归一化学习内容：压缩空白、1-500 字，非法时抛 :class:`LearningError`。"""
    text = " ".join(str(content or "").split())
    if not text:
        raise LearningError("学习内容不能为空：请用 1-500 字记录真正学到的内容")
    if len(text) > MAX_CONTENT_CHARS:
        raise LearningError(
            f"学习内容过长（{len(text)} 字），请压缩到 {MAX_CONTENT_CHARS} 字以内"
        )
    return text


def _normalize_tags(tags: Any) -> list[str]:
    """把标签归一化为去空去重的字符串列表（最多 10 个，单个最多 50 字）。"""
    if tags is None:
        return []
    if isinstance(tags, str):
        raw: Iterable[Any] = tags.replace("，", ",").split(",")
    elif isinstance(tags, (list, tuple, set, frozenset)):
        raw = tags
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(str(item or "").split())
        if not text:
            continue
        if len(text) > MAX_TAG_CHARS:
            text = text[: MAX_TAG_CHARS - 1] + "…"
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= MAX_TAGS:
            break
    return result


def _normalize_record(raw: Any) -> dict[str, Any] | None:
    """校验并归一化从 JSONL 读出的条目；不可用条目返回 None。"""
    if not isinstance(raw, dict):
        return None
    content = " ".join(str(raw.get("content") or "").split())
    if not content:
        return None
    try:
        ts = float(raw.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    tags = _normalize_tags(raw.get("tags"))
    return {
        "ts": ts,
        "content": content,
        "source": str(raw.get("source") or "").strip(),
        "tags": tags,
    }


def _record_ts(record: dict[str, Any]) -> float:
    """读取记录时间戳，非法值按 0 处理（用于按时间排序）。"""
    try:
        return float(record.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _sorted_by_ts(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按时间正序（旧→新）排序记录。"""
    return sorted(entries, key=_record_ts)


def _coerce_limit(limit: Any, default: int) -> int:
    """把调用方传入的条数限制收敛为非负整数；非法时回退默认值。"""
    if limit is None:
        return max(0, int(default))
    try:
        return max(0, int(limit))
    except (TypeError, ValueError):
        return max(0, int(default))


class LearningStore:
    """基于 JSONL 的学习档案（环形上限、线程安全、异常吞掉）。"""

    def __init__(
        self,
        root: str | Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        filename: str = LEARNING_FILENAME,
    ) -> None:
        """初始化学习档案。

        Args:
            root: 数据目录，档案文件写入 ``root/learning.jsonl``。
            max_entries: 最多保留的条目数（<=0 时回退默认值 2000）。
            filename: 档案文件名（默认 ``learning.jsonl``）。
        """
        self.root = Path(root)
        try:
            self.max_entries = int(max_entries)
        except (TypeError, ValueError):
            self.max_entries = DEFAULT_MAX_ENTRIES
        if self.max_entries <= 0:
            self.max_entries = DEFAULT_MAX_ENTRIES
        name = str(filename or "").strip() or LEARNING_FILENAME
        self.path = self.root / name
        self._lock = threading.RLock()
        self._entries: deque[dict[str, Any]] = deque(maxlen=self.max_entries)
        self._load()

    # ---- 落盘/加载 ----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = _normalize_record(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if record is not None:
                        self._entries.append(record)
        except OSError:
            return

    def _append_file(self, entries: Iterable[dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                for entry in entries:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return

    def _rewrite_file(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                for entry in self._entries:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            return

    # ---- 记录 ---------------------------------------------------------

    def record(
        self,
        *,
        content: Any,
        source: Any = "",
        tags: Any = None,
        ts: Any = None,
    ) -> dict[str, Any]:
        """追加一条学习记录并返回它。

        Args:
            content: 学习内容，压缩空白后须为 1-500 字。
            source: 来源（App 名、链接等，可为空）。
            tags: 标签（可传逗号分隔字符串或列表）。
            ts: 时间戳，缺省为当前时间；非法值回退当前时间。

        Raises:
            LearningError: 内容为空或超过 500 字。
        """
        text = _normalize_content(content)
        try:
            when = float(ts) if ts is not None else time.time()
        except (TypeError, ValueError):
            when = time.time()
        entry = {
            "ts": when,
            "content": text,
            "source": str(source or "").strip(),
            "tags": _normalize_tags(tags),
        }
        with self._lock:
            was_full = len(self._entries) >= self.max_entries
            self._entries.append(entry)
            if was_full:
                # 达到环形上限后整体重写一次，保证文件也不超过上限
                self._rewrite_file()
            else:
                self._append_file([entry])
            return dict(entry)

    # ---- 查询 ---------------------------------------------------------

    def entries(self, limit: int | None = None) -> list[dict[str, Any]]:
        """返回全部条目（旧→新）；``limit`` 非空时只返回最近 N 条。"""
        with self._lock:
            items = list(self._entries)
        if limit is None:
            return items
        count = _coerce_limit(limit, len(items))
        if count == 0:
            return []
        return items[-count:]

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        """返回最近 N 条记录（按时间旧→新，可直接拼进时间线）。"""
        count = _coerce_limit(limit, 5)
        if count == 0:
            return []
        with self._lock:
            items = _sorted_by_ts(self._entries)
        return items[-count:]

    def search(self, query: Any, limit: int = 5) -> list[dict[str, Any]]:
        """按关键词检索学习档案（内容/来源/标签，大小写不敏感）。

        Args:
            query: 关键词；为空时等价于 :meth:`recent`。
            limit: 最多返回条数；匹配按时间倒序截取后以正序（旧→新）返回。

        Returns:
            匹配到的记录列表（旧→新）。
        """
        count = _coerce_limit(limit, 5)
        if count == 0:
            return []
        needle = str(query or "").strip().lower()
        if not needle:
            return self.recent(count)
        with self._lock:
            items = _sorted_by_ts(self._entries)
        matched: list[dict[str, Any]] = []
        for record in reversed(items):
            if needle in self._search_text(record):
                matched.append(record)
                if len(matched) >= count:
                    break
        matched.reverse()
        return matched

    @staticmethod
    def _search_text(record: dict[str, Any]) -> str:
        parts = [
            str(record.get("content") or ""),
            str(record.get("source") or ""),
            " ".join(str(tag) for tag in record.get("tags") or []),
        ]
        return "\n".join(parts).lower()

    def render_for_prompt(
        self, limit: int = 5, *, max_chars: int = DEFAULT_RENDER_CHARS
    ) -> str:
        """把最近 N 条学习记录渲染成可塞进系统提示词的中文摘要。

        Args:
            limit: 最多渲染条数。
            max_chars: 整段摘要的最大字符数（超出截断并加省略号）。

        Returns:
            无记录时返回空串；否则为「【最近学到】」开头的多行文本。
        """
        records = self.recent(limit)
        if not records:
            return ""
        lines: list[str] = ["【最近学到】"]
        for record in records:
            when = time.strftime(
                "%m-%d %H:%M", time.localtime(float(record.get("ts") or 0))
            )
            source = str(record.get("source") or "").strip()
            content = str(record.get("content") or "")
            if len(content) > RENDER_CONTENT_CHARS:
                content = content[: RENDER_CONTENT_CHARS - 1] + "…"
            tags = [str(tag) for tag in record.get("tags") or [] if str(tag)]
            line = f"- （{when}）"
            if source:
                line += f"[{source}]"
            line += f" {content}"
            if tags:
                line += "（" + " ".join(f"#{tag}" for tag in tags) + "）"
            lines.append(line)
        text = "\n".join(lines)
        try:
            limit_chars = int(max_chars)
        except (TypeError, ValueError):
            limit_chars = DEFAULT_RENDER_CHARS
        if limit_chars > 0 and len(text) > limit_chars:
            text = text[: max(0, limit_chars - 1)] + "…"
        return text

    def stats(self) -> dict[str, Any]:
        """返回学习档案统计信息（条目数、来源分布、最近时间等）。"""
        with self._lock:
            items = list(self._entries)
        sources: dict[str, int] = {}
        last_ts = 0.0
        for record in items:
            source = str(record.get("source") or "").strip() or "未知"
            sources[source] = sources.get(source, 0) + 1
            try:
                last_ts = max(last_ts, float(record.get("ts") or 0))
            except (TypeError, ValueError):
                continue
        return {
            "entries": len(items),
            "sources": sources,
            "last_ts": last_ts,
            "max_entries": self.max_entries,
            "path": str(self.path),
        }
