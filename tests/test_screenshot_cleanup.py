"""core.screenshot_cleanup 单元测试：保留时长归一化、过期判定与目录清理。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from core.screenshot_cleanup import (
    DEFAULT_RETENTION_MINUTES,
    MIN_RETENTION_MINUTES,
    cleanup_directory,
    is_expired,
    normalize_retention_minutes,
    plan_expired_files,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (3, 3),
        ("5", 5),
        (1.9, 1),
        (0, MIN_RETENTION_MINUTES),
        (-5, MIN_RETENTION_MINUTES),
        (None, DEFAULT_RETENTION_MINUTES),
        ("", DEFAULT_RETENTION_MINUTES),
        ("bad", DEFAULT_RETENTION_MINUTES),
        (1440, 1440),
        (99999, 99999),
    ],
)
def test_normalize_retention_minutes(raw, expected):
    assert normalize_retention_minutes(raw) == expected


def test_is_expired_boundary():
    now = 1_000_000.0
    assert is_expired(now - 181, retention_minutes=3, now=now) is True
    assert is_expired(now - 179, retention_minutes=3, now=now) is False
    assert is_expired(now - 61, retention_minutes=1, now=now) is True
    assert is_expired(now - 59, retention_minutes=1, now=now) is False
    # 非法 mtime 不删
    assert is_expired(0, now=now) is False
    assert is_expired("bad", now=now) is False


def test_plan_expired_files_filters_prefix_and_age():
    now = 1_000_000.0
    entries = [
        ("shot-old.jpg", now - 200),
        ("frame-old.jpg", now - 200),
        ("screenshot-old.png", now - 200),
        ("shot-new.jpg", now - 10),
        ("user-photo.png", now - 9999),  # 非插件截图不清理
        ("live_frame_last.jpg", now - 9999),
    ]
    planned = plan_expired_files(entries, retention_minutes=3, now=now)
    assert set(planned) == {
        "shot-old.jpg",
        "frame-old.jpg",
        "screenshot-old.png",
        "live_frame_last.jpg",
    }


def test_cleanup_directory_deletes_only_expired_plugin_files(tmp_path):
    now = time.time()
    old = tmp_path / "shot-1.jpg"
    old.write_bytes(b"x" * 10)
    os.utime(old, (now - 400, now - 400))
    fresh = tmp_path / "shot-2.jpg"
    fresh.write_bytes(b"y")
    foreign = tmp_path / "diary.jsonl"
    foreign.write_text("keep", encoding="utf-8")
    subdir = tmp_path / "sub"
    subdir.mkdir()
    nested = subdir / "shot-nested.jpg"
    nested.write_bytes(b"z")

    result = cleanup_directory(tmp_path, retention_minutes=3, now=now)

    assert result["deleted"] == 1
    assert result["bytes_freed"] == 10
    assert result["scanned"] >= 2
    assert not old.exists()
    assert fresh.exists()
    assert foreign.exists()
    assert nested.exists()  # 只清理当前目录，不递归


def test_cleanup_directory_missing_dir_is_noop(tmp_path):
    result = cleanup_directory(tmp_path / "nope", retention_minutes=3)
    assert result == {
        "scanned": 0,
        "deleted": 0,
        "bytes_freed": 0,
        "errors": 0,
        "retention_minutes": 3,
    }


def test_cleanup_directory_retention_change(tmp_path):
    now = time.time()
    file = tmp_path / "shot-a.jpg"
    file.write_bytes(b"data")
    os.utime(file, (now - 90, now - 90))
    # 保留 3 分钟：不删
    assert cleanup_directory(tmp_path, retention_minutes=3, now=now)["deleted"] == 0
    # 保留 1 分钟：删除
    result = cleanup_directory(tmp_path, retention_minutes=1, now=now)
    assert result["deleted"] == 1
    assert not file.exists()
