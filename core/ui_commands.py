"""根据设备提供的运行时执行系统 UI 节点采集。"""

import shlex


def ui_dump_command(path: str) -> str:
    target = shlex.quote(path)
    return (
        'if [ -x /system/bin/app_process32 ] && [ -n "$(getprop ro.product.cpu.abilist32)" ]; '
        "then CLASSPATH=/system/framework/uiautomator.jar /system/bin/app_process32 "
        "/system/bin com.android.commands.uiautomator.Launcher dump "
        + target
        + "; else uiautomator dump "
        + target
        + "; fi"
    )
