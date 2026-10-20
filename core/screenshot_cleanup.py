"""手机截图自动清理：按保留时长删除过期截图（纯标准库，便于本地单测）。

截图默认落盘在 ``plugin_data/astrbot_phone_control/screenshots/``（``shot-*`` /
``frame-*`` / ``screenshot-*`` 等）；持续刷/实时页会不断产生新截图，若不清理会持续
占用磁盘（实测连续使用数小时可到 GB 级）。本模块提供：

* 保留分钟数归一化（默认 3 分钟，最小 1 分钟）；
* 过期判定与目录清理（只删目录内普通文件，跳过符号链接/子目录，删除失败计数）。

实际调用节流与后台循环在 ``core/plugin/bootstrap.py`` / ``PhoneService`` 完成。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterable

DEFAULT_RETENTION_MINUTES = 3
MIN_RETENTION_MINUTES = 1
# 只清理截图目录里由插件产生的文件，避免误删用户放置的其他文件
SCREENSHOT_NAME_PREFIXES = ("shot-", "frame-", "screenshot-", "live_frame_last")


def normalize_retention_minutes(raw: Any) -> int:
    """归一化截图保留分钟数：非法回退默认 3，最小 1 分钟（不设上限）。"""
    if raw is None or raw == "":
        return DEFAULT_RETENTION_MINUTES
    try:
        minutes = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_MINUTES
    return max(MIN_RETENTION_MINUTES, minutes)


def is_expired(
    mtime: float,
    *,
    retention_minutes: Any = DEFAULT_RETENTION_MINUTES,
    now: float | None = None,
) -> bool:
    """判断文件修改时间是否超过保留时长。"""
    try:
        modified = float(mtime)
    except (TypeError, ValueError):
        return False
    if modified <= 0:
        return False
    current = float(now) if now is not None else time.time()
    retention_seconds = normalize_retention_minutes(retention_minutes) * 60
    return (current - modified) > retention_seconds


def _is_screenshot_name(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in SCREENSHOT_NAME_PREFIXES)


def plan_expired_files(
    entries: Iterable[tuple[str, float]],
    *,
    retention_minutes: Any = DEFAULT_RETENTION_MINUTES,
    now: float | None = None,
) -> list[str]:
    """从 ``(文件名, mtime)`` 序列中挑出过期截图文件名（纯函数，供测试）。"""
    result: list[str] = []
    for name, mtime in entries:
        if _is_screenshot_name(name) and is_expired(
            mtime, retention_minutes=retention_minutes, now=now
        ):
            result.append(name)
    return result


def cleanup_directory(
    directory: str | Path,
    *,
    retention_minutes: Any = DEFAULT_RETENTION_MINUTES,
    now: float | None = None,
) -> dict[str, Any]:
    """删除截图目录中超过保留时长的插件截图。

    Args:
        directory: 截图目录（不存在时返回空结果，不报错）。
        retention_minutes: 保留分钟数（默认 3，最小 1）。
        now: 当前时间戳（测试注入用）。

    Returns:
        ``{"scanned": int, "deleted": int, "bytes_freed": int, "errors": int,
        "retention_minutes": int}``。
    """
    minutes = normalize_retention_minutes(retention_minutes)
    result: dict[str, Any] = {
        "scanned": 0,
        "deleted": 0,
        "bytes_freed": 0,
        "errors": 0,
        "retention_minutes": minutes,
    }
    root = Path(directory)
    try:
        entries = list(os.scandir(root))
    except (FileNotFoundError, NotADirectoryError):
        return result
    except OSError:
        result["errors"] += 1
        return result
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            if not _is_screenshot_name(entry.name):
                continue
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            result["errors"] += 1
            continue
        result["scanned"] += 1
        if not is_expired(stat.st_mtime, retention_minutes=minutes, now=now):
            continue
        try:
            os.unlink(entry.path)
        except OSError:
            result["errors"] += 1
            continue
        result["deleted"] += 1
        result["bytes_freed"] += int(stat.st_size or 0)
    return result
