"""插件本地指标采集：截图耗时与 LLM token/s 历史。

记录到插件数据目录下的 ``metrics.jsonl``（环形上限 20000 行），另在内存保存
最近记录。Dashboard 后端用 :meth:`MetricsStore.recent_series` 读取按时间过滤的
时间序列与 10s token/s 分桶；LLM 点按每次模型调用采样，桶内 tps 按耗时加权。

本模块只依赖标准库；任何写入/读取异常都吞掉并返回空结果，避免影响正常手机流程。
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

METRICS_FILENAME = "metrics.jsonl"
DEFAULT_MAX_ENTRIES = 20000
BUCKET_SECONDS = 10
_MAX_SERIES_SCREENSHOTS = 600
_MAX_SERIES_LLM = 600
_MAX_SERIES_BUCKETS = 360


def _plain(value: Any) -> Any:
    """把值压成 JSON 可序列化类型；异常时返回 None。"""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return None


def _record_from_json(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    kind = record.get("kind")
    if kind not in {"screenshot", "llm"}:
        return None
    try:
        record["ts"] = float(record.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    return record


class MetricsStore:
    """截图/LLM 指标记录与查询。"""

    def __init__(
        self,
        root: str | Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        base = Path(root)
        self.path = base / METRICS_FILENAME
        self.max_entries = max(100, int(max_entries))
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
                    record = _record_from_json(line)
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

    def _append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            was_full = len(self._entries) >= self.max_entries
            self._entries.append(entry)
            if was_full:
                # 达到环形上限后整体重写一次，保证文件也不超过上限
                self._rewrite_file()
            else:
                self._append_file([entry])

    # ---- 记录 ---------------------------------------------------------

    def record_screenshot(
        self,
        *,
        ms: float,
        source: str = "adb",
        method: str = "png",
        ts: float | None = None,
    ) -> dict[str, Any] | None:
        """记录一次截图耗时（ms / 来源 / 方法）。"""
        try:
            entry = {
                "ts": float(ts if ts is not None else time.time()),
                "kind": "screenshot",
                "ms": round(max(0.0, float(ms)), 1),
                "source": str(source or "adb"),
                "method": str(method or "png"),
            }
        except (TypeError, ValueError):
            return None
        self._append(entry)
        return entry

    def record_llm(
        self,
        *,
        tokens: float,
        ms: float,
        tps: float | None = None,
        session: str = "",
        ts: float | None = None,
    ) -> dict[str, Any] | None:
        """记录一次 LLM 请求的 token 数、耗时与 token/s。"""
        try:
            token_value = max(0.0, float(tokens))
            duration = max(0.0, float(ms))
            rate = (
                float(tps)
                if tps is not None
                else (token_value / (duration / 1000.0) if duration > 0 else 0.0)
            )
            entry = {
                "ts": float(ts if ts is not None else time.time()),
                "kind": "llm",
                "tokens": round(token_value, 1),
                "ms": round(duration, 1),
                "tps": round(max(0.0, rate), 2),
                "session": str(session or ""),
            }
        except (TypeError, ValueError):
            return None
        self._append(entry)
        return entry

    # ---- 查询 ---------------------------------------------------------

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)

    def recent_series(
        self, minutes: float = 30, *, now: float | None = None
    ) -> dict[str, Any]:
        """返回最近 ``minutes`` 分钟的截图/LLM 点与 10s token/s 分桶。

        每个 LLM 点由一次模型调用记录（tokens 为输入 token 数、ms 为调用耗时）；
        分桶 tps 按桶内调用耗时加权，而不是把 tokens 摊到固定 10s 窗口。
        """
        try:
            window_minutes = max(0.1, float(minutes))
        except (TypeError, ValueError):
            window_minutes = 30.0
        current = float(now) if now is not None else time.time()
        cutoff = current - window_minutes * 60.0
        with self._lock:
            recent = [entry for entry in self._entries if entry.get("ts", 0) >= cutoff]
        screenshots: list[dict[str, Any]] = []
        llm: list[dict[str, Any]] = []
        for entry in recent:
            if entry.get("kind") == "screenshot":
                screenshots.append(
                    {
                        "ts": entry.get("ts", 0),
                        "ms": entry.get("ms", 0),
                        "source": entry.get("source", "adb"),
                    }
                )
            elif entry.get("kind") == "llm":
                llm.append(
                    {
                        "ts": entry.get("ts", 0),
                        "tokens": entry.get("tokens", 0),
                        "ms": entry.get("ms", 0),
                        "tps": entry.get("tps", 0),
                    }
                )
        screenshots = screenshots[-_MAX_SERIES_SCREENSHOTS:]
        llm = llm[-_MAX_SERIES_LLM:]

        buckets: dict[int, dict[str, float]] = {}
        for point in llm:
            ts = float(point.get("ts") or 0)
            slot = int(ts // BUCKET_SECONDS) * BUCKET_SECONDS
            bucket = buckets.setdefault(slot, {"tokens": 0.0, "ms": 0.0})
            bucket["tokens"] += float(point.get("tokens") or 0)
            bucket["ms"] += float(point.get("ms") or 0)
        bucket_list = [
            {
                "ts": slot,
                # 桶内按各次调用耗时加权：tps = 桶内 token 总量 / 桶内调用总耗时
                "tps": round(
                    value["tokens"] / (value["ms"] / 1000.0), 2
                )
                if value["ms"] > 0
                else 0.0,
                "tokens": round(value["tokens"], 1),
                "ms": round(value["ms"], 1),
            }
            for slot, value in sorted(buckets.items())
        ][-_MAX_SERIES_BUCKETS:]
        return {
            "minutes": window_minutes,
            "screenshots": screenshots,
            "llm": llm,
            "buckets": bucket_list,
        }

    def latest(self, kind: str) -> dict[str, Any] | None:
        """返回最近一条指定类型记录（不含 kind 字段）。"""
        target = str(kind or "").strip()
        with self._lock:
            for entry in reversed(self._entries):
                if entry.get("kind") != target:
                    continue
                result = {
                    key: value for key, value in entry.items() if key != "kind"
                }
                return result
        return None

    def latest_screenshot(self) -> dict[str, Any] | None:
        return self.latest("screenshot")

    def latest_llm(self) -> dict[str, Any] | None:
        return self.latest("llm")

    def stats(self) -> dict[str, int]:
        with self._lock:
            total = len(self._entries)
            screenshots = sum(
                1 for entry in self._entries if entry.get("kind") == "screenshot"
            )
            llm = sum(1 for entry in self._entries if entry.get("kind") == "llm")
        return {
            "entries": total,
            "screenshots": screenshots,
            "llm": llm,
            "max_entries": self.max_entries,
        }
