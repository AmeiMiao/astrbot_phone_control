"""统一后台清理：逐包清理，保留常驻白名单与系统操作入口。"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from .errors import ActionFailedError, PhoneControlError

DEFAULT_KEEP_PACKAGES = ("com.wireguard.android", "com.tailscale.ipn", "fun.acat.phonerelay", "com.zerotier.one")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")


def keep_packages(values: Iterable[str]) -> set[str]:
    return {str(value).strip() for value in values if PACKAGE_RE.fullmatch(str(value).strip())}


async def clean_background(
    adb: Any,
    *,
    whitelist: Iterable[str] = DEFAULT_KEEP_PACKAGES,
    foreground: str = "",
    package: str = "",
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """每次动作复核租约；白名单、桌面、输入法和指定前台始终保留。"""
    def check():
        if checkpoint is not None:
            checkpoint("cleanup_force_stop")

    check()
    home = await adb.shell(
        "cmd package resolve-activity --brief -a android.intent.action.MAIN "
        "-c android.intent.category.HOME"
    )
    keyboard = await adb.shell("settings get secure default_input_method")
    protected = {"android", "com.android.systemui", foreground} | keep_packages(whitelist)
    for value in (home, keyboard):
        protected.update(re.findall(r"([A-Za-z][\w]*(?:\.[\w]+)+)/", str(value)))
    if package:
        if not PACKAGE_RE.fullmatch(package):
            raise ActionFailedError("包名格式非法")
        if package in protected:
            raise ActionFailedError(f"{package} 属于清理保留项，未停止")
        targets = {package}
    else:
        installed = await adb.shell("pm list packages -3")
        recents = await adb.shell("dumpsys activity recents")
        targets = set(re.findall(r"^package:([\w.]+)\s*$", str(installed), re.M))
        targets.update(re.findall(r"(?:cmp=|realActivity=|mActivityComponent=|A=\d+:)([\w.]+)/", str(recents)))
    report: dict[str, Any] = {"stopped": 0, "errors": [], "preserved": sorted(targets & protected)}
    for target in sorted(keep_packages(targets) - protected):
        check()
        try:
            await adb.shell(f"am force-stop --user current {target}")
        except PhoneControlError as exc:
            report["errors"].append(f"{target}: {exc}")
            continue
        check()
        try:
            alive = await adb.shell(f"pidof {target} || true")
            if str(alive).strip():
                report["errors"].append(f"{target} 仍有进程")
            else:
                report["stopped"] += 1
        except PhoneControlError as exc:
            report["errors"].append(f"{target}: {exc}")
    check()
    return report
