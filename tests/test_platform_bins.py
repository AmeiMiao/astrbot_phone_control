"""core.platform_bins 单元测试：宿主平台识别、二进制选择与可执行位修复。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from core.platform_bins import (
    PLATFORM_LINUX_AARCH64,
    PLATFORM_LINUX_X86_64,
    PLATFORM_WINDOWS_X86_64,
    binary_filename,
    bundled_adb,
    bundled_binary,
    bundled_phone_wg,
    ensure_bundled_executables,
    host_platform,
)


def test_host_platform_mapping():
    assert host_platform(system="Linux", machine="x86_64") == PLATFORM_LINUX_X86_64
    assert host_platform(system="Linux", machine="amd64") == PLATFORM_LINUX_X86_64
    assert host_platform(system="Linux", machine="aarch64") == PLATFORM_LINUX_AARCH64
    assert host_platform(system="Linux", machine="arm64") == PLATFORM_LINUX_AARCH64
    assert host_platform(system="Windows", machine="AMD64") == PLATFORM_WINDOWS_X86_64
    # macOS / 其他架构不随包分发二进制，返回空串由 adb.path/PATH 兜底。
    assert host_platform(system="Darwin", machine="arm64") == ""
    assert host_platform(system="Linux", machine="riscv64") == ""
    assert host_platform(system="FreeBSD", machine="x86_64") == ""


def test_binary_filename_windows_uses_exe():
    assert binary_filename("adb", platform_id=PLATFORM_WINDOWS_X86_64) == "adb.exe"
    assert binary_filename("phone-wg", platform_id=PLATFORM_WINDOWS_X86_64) == "phone-wg.exe"
    assert binary_filename("adb", platform_id=PLATFORM_LINUX_X86_64) == "adb"


def test_bundled_binary_selects_platform_dir(tmp_path):
    win = tmp_path / "bin" / PLATFORM_WINDOWS_X86_64
    win.mkdir(parents=True)
    (win / "adb.exe").write_bytes(b"PE")
    assert bundled_adb(tmp_path, platform_id=PLATFORM_WINDOWS_X86_64) == win / "adb.exe"
    assert bundled_phone_wg(tmp_path, platform_id=PLATFORM_WINDOWS_X86_64) is None

    arm = tmp_path / "bin" / PLATFORM_LINUX_AARCH64
    arm.mkdir(parents=True)
    (arm / "phone-wg").write_bytes(b"ELF")
    assert bundled_phone_wg(tmp_path, platform_id=PLATFORM_LINUX_AARCH64) == arm / "phone-wg"
    assert bundled_binary("missing", root=tmp_path, platform_id=PLATFORM_LINUX_AARCH64) is None
    assert platform_missing(tmp_path) is None


def platform_missing(root: Path) -> Path | None:
    """不支持平台返回 None（避免测试里依赖运行主机）。"""
    return bundled_adb(root, platform_id="")


def test_ensure_bundled_executables_repairs_modes(tmp_path):
    platform_id = PLATFORM_LINUX_AARCH64
    directory = tmp_path / "bin" / platform_id
    lib = directory / "lib"
    lib.mkdir(parents=True)
    adb = directory / "adb"
    helper = directory / "phone-wg"
    real = lib / "adb-bin"
    for path in (adb, helper, real):
        path.write_bytes(b"ELF")
        path.chmod(0o644)

    result = ensure_bundled_executables(tmp_path, platform_id=platform_id)
    assert set(result) == {
        f"bin/{platform_id}/adb",
        f"bin/{platform_id}/phone-wg",
        f"bin/{platform_id}/lib/adb-bin",
    }
    assert all(result.values())
    for path in (adb, helper, real):
        mode = path.stat().st_mode
        assert mode & stat.S_IXUSR and mode & stat.S_IXGRP and mode & stat.S_IXOTH


def test_common_repository_layout_has_bundled_adb_for_supported_platform():
    """仓库对 x86_64 宿主确实随包提供 adb（其他交付平台允许缺失并回退）。"""
    repository = Path(__file__).resolve().parents[1]
    path = bundled_adb(repository, platform_id=PLATFORM_LINUX_X86_64)
    assert path is not None and os.access(path, os.X_OK)


def test_repository_ships_arm64_and_windows_platform_dirs():
    """仓库随包 Linux ARM64 与 Windows 的 adb/phone-wg 及说明文件。"""
    root = Path(__file__).resolve().parents[1]
    arm = root / "bin" / PLATFORM_LINUX_AARCH64
    wrapper = arm / "adb"
    assert wrapper.is_file()
    assert "lib/adb-bin" in wrapper.read_text(encoding="utf-8")
    assert (arm / "lib" / "adb-bin").is_file()
    assert (arm / "phone-wg").is_file()
    assert (arm / "NOTICE.txt").is_file()
    assert list((arm / "lib").glob("*.so*")), "ARM64 adb 依赖库缺失"

    win = root / "bin" / PLATFORM_WINDOWS_X86_64
    assert (win / "adb.exe").is_file()
    assert (win / "AdbWinApi.dll").is_file()
    assert (win / "AdbWinUsbApi.dll").is_file()
    assert (win / "phone-wg.exe").is_file()
    assert (win / "NOTICE.txt").is_file()
