"""插件自带 WireGuard helper 的平台选择与可执行位修复。"""

from __future__ import annotations

import platform
from pathlib import Path

from .plugin_shared import ensure_executable

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# 唯一随包分发的宿主平台目录。
PLATFORM_LINUX_X86_64 = "linux-x86_64"
SUPPORTED_PLATFORMS = (PLATFORM_LINUX_X86_64,)


def _normalized_machine(machine: str | None) -> str:
    text = str(machine or "").strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86-64": "x86_64",
    }
    return aliases.get(text, text)


def host_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    """返回当前宿主平台的随包目录名；不随包分发的平台返回空串。

    Args:
        system: 覆盖 ``platform.system()``（单测用）。
        machine: 覆盖 ``platform.machine()``（单测用）。

    Returns:
        仅 Linux x86_64 返回 ``linux-x86_64``；其他平台返回空串。
    """
    system_name = str(system if system is not None else platform.system()).strip().lower()
    machine_name = _normalized_machine(
        machine if machine is not None else platform.machine()
    )
    if system_name == "linux" and machine_name == "x86_64":
        return PLATFORM_LINUX_X86_64
    return ""


def platform_dir(
    root: str | Path | None = None, *, platform_id: str | None = None
) -> Path | None:
    """返回随包平台目录（仅 Linux x86_64 存在时返回）。"""
    resolved = platform_id if platform_id is not None else host_platform()
    if resolved != PLATFORM_LINUX_X86_64:
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
    candidate = directory / str(name)
    return candidate if candidate.is_file() else None


def bundled_phone_wg(
    root: str | Path | None = None, *, platform_id: str | None = None
) -> Path | None:
    """随包 ``phone-wg`` 路径（仅 Linux x86_64）。"""
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
    if directory is None:
        return {}
    result: dict[str, bool] = {}
    for name in ("phone-wg",):
        candidate = directory / name
        if candidate.is_file():
            result[candidate.relative_to(base).as_posix()] = ensure_executable(candidate)
    return result
