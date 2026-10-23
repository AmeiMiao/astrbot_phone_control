"""core.platform_bins 单元测试：宿主平台识别、二进制选择与可执行位修复。

WireGuard helper 面向 Linux x86_64；外部运行组件由组件准备器管理。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from core.platform_bins import (
    PLATFORM_LINUX_X86_64,
    SUPPORTED_PLATFORMS,
    bundled_binary,
    bundled_phone_wg,
    ensure_bundled_executables,
    host_platform,
    platform_dir,
)


def test_host_platform_mapping():
    assert host_platform(system="Linux", machine="x86_64") == PLATFORM_LINUX_X86_64
    assert host_platform(system="Linux", machine="amd64") == PLATFORM_LINUX_X86_64
    assert host_platform(system="Linux", machine="AMD64") == PLATFORM_LINUX_X86_64
    # 其余平台不随包分发二进制，返回空串由 adb.path/PATH 兜底。
    assert host_platform(system="Linux", machine="aarch64") == ""
    assert host_platform(system="Linux", machine="arm64") == ""
    assert host_platform(system="Windows", machine="AMD64") == ""
    assert host_platform(system="Darwin", machine="x86_64") == ""
    assert host_platform(system="Darwin", machine="arm64") == ""
    assert host_platform(system="Linux", machine="riscv64") == ""
    assert host_platform(system="FreeBSD", machine="x86_64") == ""


def test_supported_platforms_only_linux_x86_64():
    assert SUPPORTED_PLATFORMS == (PLATFORM_LINUX_X86_64,)


def test_bundled_binary_selects_only_linux_x86_64(tmp_path):
    platform = tmp_path / "bin" / PLATFORM_LINUX_X86_64
    platform.mkdir(parents=True)
    (platform / "adb").write_bytes(b"ELF")
    (platform / "phone-wg").write_bytes(b"ELF")

    assert bundled_phone_wg(tmp_path) == platform / "phone-wg"
    assert bundled_binary("missing", root=tmp_path) is None

    # helper 按宿主平台选择。
    arm = tmp_path / "bin" / "linux-aarch64"
    arm.mkdir(parents=True)
    (arm / "adb").write_bytes(b"ELF")
    assert platform_dir(tmp_path, platform_id="linux-aarch64") is None


def test_ensure_bundled_executables_repairs_modes(tmp_path):
    platform = PLATFORM_LINUX_X86_64
    directory = tmp_path / "bin" / platform
    directory.mkdir(parents=True)
    adb = directory / "adb"
    helper = directory / "phone-wg"
    for path in (adb, helper):
        path.write_bytes(b"ELF")
        path.chmod(0o644)

    result = ensure_bundled_executables(tmp_path, platform_id=platform)
    assert set(result) == {
        f"bin/{platform}/phone-wg",
    }
    assert all(result.values())
    for path in (helper,):
        mode = path.stat().st_mode
        assert mode & stat.S_IXUSR and mode & stat.S_IXGRP and mode & stat.S_IXOTH


def test_external_runtime_components_are_acquired_separately():
    repository = Path(__file__).resolve().parents[1]
    assert bundled_phone_wg(repository, platform_id=PLATFORM_LINUX_X86_64) is not None
    for name in ("adb", "libzt.so", "tailscale", "tailscaled"):
        assert not (repository / "bin" / PLATFORM_LINUX_X86_64 / name).exists()


def test_repository_ships_only_linux_x86_64_platform_dir():
    """插件体积限制：其他平台目录已移除，用户自行编译/提供系统 adb。"""
    root = Path(__file__).resolve().parents[1]
    platform_dirs = {
        path.name
        for path in (root / "bin").iterdir()
        if path.is_dir() and "-" in path.name
    }
    assert platform_dirs == {PLATFORM_LINUX_X86_64}
    assert not (root / "bin" / "linux-aarch64").exists()
    assert not (root / "bin" / "windows-x86_64").exists()
