"""非 Linux 平台降级测试：进程监护、/proc 回收与命令直启。"""

from __future__ import annotations

import asyncio
import sys

from core.process_lease import guard_supported, guarded_command, reclaim_legacy_processes


def test_guard_supported_is_linux_only(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert guard_supported() is True
    monkeypatch.setattr(sys, "platform", "win32")
    assert guard_supported() is False
    monkeypatch.setattr(sys, "platform", "darwin")
    assert guard_supported() is False


def test_guarded_command_falls_back_to_direct_command(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    command = ["mitmdump", "-q", "-p", "18080"]
    assert guarded_command(command, tmp_path / "process.lock") == command


def test_reclaim_skips_non_linux_without_touching_proc(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert asyncio.run(reclaim_legacy_processes([tmp_path])) == 0
