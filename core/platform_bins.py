"""插件自带宿主平台二进制的选择与权限修复。

插件随包分发 ``adb`` 与 ``phone-wg`` 的多个宿主平台版本，目录约定::

    bin/<platform-id>/adb[.exe]
    bin/<platform-id>/phone-wg[.exe]
    bin/<platform-id>/lib/adb-bin         # Linux 平台 adb 的动态链接真实二进制
    bin/<platform-id>/lib/*.so*          # adb-bin 依赖的随包共享库

支持的宿主平台：Linux x86_64 / Linux ARM64 / Windows x86_64。其他平台（如
macOS）不随包分发二进制，回退到配置的 ``adb.path`` 或 PATH 中的 ``adb``。

本模块只依赖标准库，不导入 astrbot，便于本地单测与在插件装配早期使用。
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from .plugin_shared import ensure_executable

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# 宿主平台目录：linux-x86_64 / linux-aarch64 / windows-x86_64。
PLATFORM_LINUX_X86_64 = "linux-x86_64"
PLATFORM_LINUX_AARCH64 = "linux-aarch64"
PLATFORM_WINDOWS_X86_64 = "windows-x86_64"

SUPPORTED_PLATFORMS = (
    PLATFORM_LINUX_X86_64,
    PLATFORM_LINUX_AARCH64,
    PLATFORM_WINDOWS_X86_64,
)


def _normalized_machine(machine: str | None) -> str:
    text = str(machine or "").strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86-64": "x86_64",
        "arm64": "aarch64",
        "aarch64_be": "aarch64",
    }
    return aliases.get(text, text)


def host_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    """返回当前宿主平台的随包目录名；不支持或未知平台返回空串。

    Args:
        system: 覆盖 ``platform.system()``（单测用）。
        machine: 覆盖 ``platform.machine()``（单测用）。

    Returns:
        ``linux-x86_64`` / ``linux-aarch64`` / ``windows-x86_64``，或空串。
    """
    system_name = str(system if system is not None else platform.system()).strip().lower()
    machine_name = _normalized_machine(
        machine if machine is not None else platform.machine()
    )
    if system_name == "linux":
        if machine_name == "x86_64":
            return PLATFORM_LINUX_X86_64
        if machine_name == "aarch64":
            return PLATFORM_LINUX_AARCH64
        return ""
    if system_name in {"windows", "win32", "cygwin", "msys"}:
        if machine_name == "x86_64":
            return PLATFORM_WINDOWS_X86_64
        return ""
    return ""


def is_windows() -> bool:
    """当前宿主是否 Windows（用于路径与进程收尾分支）。"""
    return sys.platform.startswith("win")


def binary_filename(name: str, *, platform_id: str | None = None) -> str:
    """平台目录内的可执行文件名（Windows 追加 ``.exe``）。"""
    resolved = platform_id if platform_id is not None else host_platform()
    if resolved == PLATFORM_WINDOWS_X86_64 or (not resolved and is_windows()):
        return f"{name}.exe"
    return name


def platform_dir(
    root: str | Path | None = None, *, platform_id: str | None = None
) -> Path | None:
    """返回随包平台目录（不存在时返回 None）。"""
    resolved = platform_id if platform_id is not None else host_platform()
    if not resolved:
        return None
    directory = Path(root or PLUGIN_ROOT) / "bin" / resolved
    return directory if directory.is_dir() else None


def bundled_binary(
    name: str,
    *,
    root: str | Path | None = None,
    platform_id: str | None = None,
) -> Path | None:
    """返回随包分发的平台二进制路径；未分发或缺失时返回 None。"""
    directory = platform_dir(root, platform_id=platform_id)
    if directory is None:
        return None
    candidate = directory / binary_filename(name, platform_id=platform_id)
    return candidate if candidate.is_file() else None


def bundled_adb(
    root: str | Path | None = None, *, platform_id: str | None = None
) -> Path | None:
    """随包 ``adb`` 路径（按宿主平台选择）。"""
    return bundled_binary("adb", root=root, platform_id=platform_id)


def bundled_phone_wg(
    root: str | Path | None = None, *, platform_id: str | None = None
) -> Path | None:
    """随包 ``phone-wg`` 路径（按宿主平台选择）。"""
    return bundled_binary("phone-wg", root=root, platform_id=platform_id)


def ensure_bundled_executables(
    root: str | Path | None = None,
    *,
    platform_id: str | None = None,
) -> dict[str, bool]:
    """给随包二进制补可执行位（市场/zip 安装会丢权限）。

    Args:
        root: 插件根目录，默认本插件。
        platform_id: 覆盖宿主平台（单测用），默认按当前宿主识别。

    Returns:
        ``{相对路径: 是否可执行}``；只包含该平台实际存在的文件。
    """
    base = Path(root or PLUGIN_ROOT)
    resolved = platform_id if platform_id is not None else host_platform()
    directory = platform_dir(base, platform_id=resolved)
    if directory is None or not resolved:
        return {}
    candidates = [
        directory / binary_filename("adb", platform_id=resolved),
        directory / binary_filename("phone-wg", platform_id=resolved),
        directory / "lib" / "adb-bin",
    ]
    result: dict[str, bool] = {}
    for candidate in candidates:
        if candidate.is_file():
            result[candidate.relative_to(base).as_posix()] = ensure_executable(candidate)
    return result
