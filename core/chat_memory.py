"""聊天记忆：记录最近聊天消息，供 AI 按群/人的喜好分享内容。

落盘：``plugin_data/astrbot_phone_control/chat_memory.jsonl``，字段
``{ts, umo, session_id, platform, sender_id, sender_name, text, is_group}``。
跳过空文本、以 ``/`` 开头的命令、机器人自己发的消息；全局最多 3000 条，
单会话最多保留最近 100 条。本模块只依赖标准库，便于本地单测。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

CHAT_MEMORY_FILENAME = "chat_memory.jsonl"
DEFAULT_MAX_ENTRIES = 3000
DEFAULT_PER_SESSION = 100
_MAX_TEXT_LEN = 2000


class ChatMemory:
    """按会话保存最近聊天消息的 JSONL 存储。"""

    def __init__(
        self,
        root: str | Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        per_session: int = DEFAULT_PER_SESSION,
    ) -> None:
        base = Path(root)
        self.path = base / CHAT_MEMORY_FILENAME
        self.max_entries = max(100, int(max_entries))
        self.per_session = max(1, int(per_session))

    # ---- 内部读写 -----------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
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
                        continue
                    if isinstance(record, dict):
                        entries.append(record)
        except OSError:
            return []
        return entries

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                for record in entries:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            pass

    @staticmethod
    def should_record(
        text: str,
        *,
        sender_id: str = "",
        self_id: str = "",
    ) -> bool:
        """判断一条消息是否值得记录（空/命令/机器人自己 → 不记录）。"""
        content = str(text or "").strip()
        if not content:
            return False
        if content.startswith("/") or content.startswith("／"):
            return False
        sender = str(sender_id or "").strip()
        bot = str(self_id or "").strip()
        if sender and bot and sender == bot:
            return False
        return True

    # ---- 对外接口 -----------------------------------------------------

    def record(
        self,
        *,
        umo: str,
        text: str,
        session_id: str = "",
        platform: str = "",
        sender_id: str = "",
        sender_name: str = "",
        is_group: bool = False,
        self_id: str = "",
        ts: float | None = None,
    ) -> dict[str, Any] | None:
        """记录一条消息；被过滤时返回 ``None``。"""
        if not self.should_record(text, sender_id=sender_id, self_id=self_id):
            return None
        entry = {
            "ts": float(ts if ts is not None else time.time()),
            "umo": str(umo or ""),
            "session_id": str(session_id or ""),
            "platform": str(platform or ""),
            "sender_id": str(sender_id or ""),
            "sender_name": str(sender_name or ""),
            "text": str(text or "").strip()[:_MAX_TEXT_LEN],
            "is_group": bool(is_group),
        }
        entries = self._read_entries()
        entries.append(entry)
        session_umo = entry["umo"]
        same_session = [
            record for record in entries if record.get("umo") == session_umo
        ]
        if len(same_session) > self.per_session:
            drop = len(same_session) - self.per_session
            drop_ids = {id(record) for record in same_session[:drop]}
            entries = [record for record in entries if id(record) not in drop_ids]
        if len(entries) > self.max_entries:
            entries = entries[-self.max_entries :]
        self._write_entries(entries)
        return entry

    def recent(self, umo: str, limit: int = 15) -> list[dict[str, Any]]:
        """返回指定会话最近的消息（按时间正序）。"""
        target = str(umo or "").strip()
        count = max(0, int(limit))
        if not target or count == 0:
            return []
        entries = [
            entry for entry in self._read_entries() if entry.get("umo") == target
        ]
        return entries[-count:]

    def sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        """返回最近活跃会话列表（每个会话最后一条消息摘要 + 时间）。"""
        count = max(0, int(limit))
        if count == 0:
            return []
        latest: dict[str, dict[str, Any]] = {}
        for entry in self._read_entries():
            umo = str(entry.get("umo") or "").strip()
            if not umo:
                continue
            previous = latest.get(umo)
            if previous is None or float(entry.get("ts") or 0) >= float(
                previous.get("ts") or 0
            ):
                latest[umo] = entry
        sessions = sorted(
            latest.values(), key=lambda entry: float(entry.get("ts") or 0)
        )
        return sessions[-count:][::-1]

    def last_ts(self, umo: str) -> float:
        """返回指定会话最后一条消息时间戳（无记录返回 0）。"""
        target = str(umo or "").strip()
        if not target:
            return 0.0
        last = 0.0
        for entry in self._read_entries():
            if entry.get("umo") != target:
                continue
            try:
                ts = float(entry.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if ts > last:
                last = ts
        return last

    def is_known_session(self, umo: str) -> bool:
        """会话是否在聊天记忆中出现过（是否允许作为分享目标）。"""
        return self.last_ts(umo) > 0

    def session_summaries(self, limit: int = 30) -> list[dict[str, Any]]:
        """返回最近活跃会话摘要：umo/是否群聊/最后一条文本/时间/消息条数。

        按最后活跃时间倒序；供 ``phone_recent_chat`` 与分享目标校验使用。
        """
        count = max(0, int(limit))
        if count == 0:
            return []
        latest: dict[str, dict[str, Any]] = {}
        for entry in self._read_entries():
            umo = str(entry.get("umo") or "").strip()
            if not umo:
                continue
            item = latest.get(umo)
            try:
                ts = float(entry.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if item is None:
                latest[umo] = {
                    "umo": umo,
                    "is_group": bool(entry.get("is_group")),
                    "last_ts": ts,
                    "last_text": str(entry.get("text") or ""),
                    "sender_name": str(entry.get("sender_name") or ""),
                    "platform": str(entry.get("platform") or ""),
                    "count": 1,
                }
                continue
            item["count"] = int(item.get("count") or 0) + 1
            if ts >= float(item.get("last_ts") or 0):
                item.update(
                    {
                        "is_group": bool(entry.get("is_group")),
                        "last_ts": ts,
                        "last_text": str(entry.get("text") or ""),
                        "sender_name": str(entry.get("sender_name") or ""),
                        "platform": str(entry.get("platform") or ""),
                    }
                )
        sessions = sorted(latest.values(), key=lambda item: item["last_ts"])
        return sessions[-count:][::-1]

    def stats(self) -> dict[str, Any]:
        """返回记录条数与会话数。"""
        entries = self._read_entries()
        return {
            "entries": len(entries),
            "sessions": len({entry.get("umo") for entry in entries}),
            "max_entries": self.max_entries,
            "per_session": self.per_session,
        }
