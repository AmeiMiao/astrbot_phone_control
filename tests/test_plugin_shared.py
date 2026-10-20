"""插件共享小工具测试：自带二进制的可执行位修复。"""

from __future__ import annotations

import stat

from core.plugin_shared import ensure_executable


def _make_file(tmp_path, mode: int):
    path = tmp_path / "adb"
    path.write_bytes(b"#!/bin/sh\n")
    path.chmod(mode)
    return path


def test_ensure_executable_adds_all_exec_bits(tmp_path) -> None:
    path = _make_file(tmp_path, 0o644)
    assert ensure_executable(path) is True
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & stat.S_IXUSR
    assert mode & stat.S_IXGRP
    assert mode & stat.S_IXOTH
    assert mode & stat.S_IWUSR  # 不破坏原有写权限


def test_ensure_executable_keeps_existing_mode(tmp_path) -> None:
    path = _make_file(tmp_path, 0o755)
    assert ensure_executable(path) is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_ensure_executable_rejects_missing_and_directory(tmp_path) -> None:
    assert ensure_executable(tmp_path / "missing") is False
    directory = tmp_path / "bin"
    directory.mkdir()
    assert ensure_executable(directory) is False


