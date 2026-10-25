"""复用设备控制契约，通过 Relay App 的本机 ADB 执行。"""
from __future__ import annotations
import base64
import shlex
from .adb import AdbClient
from .app_bridge import APP_UNSUPPORTED
from .errors import ActionFailedError, AdbCommandError, DeviceOfflineError


class AppAdbClient(AdbClient):
    app_mode = True

    def __init__(self, bridge, home_dir, *, command_timeout=30, logger=None):
        super().__init__(None, "relay-app", 1, home_dir, defer_binary=True,
                         command_timeout=command_timeout, logger=logger)
        self.bridge = bridge

    async def _is_online_raw(self):
        return self.bridge.writer is not None

    async def _do_connect(self, force=False):
        if not await self._is_online_raw():
            raise DeviceOfflineError("Relay App 未连接")
        info = await self._device_info(retry=False)
        self._connected = True
        return info

    async def _run_adb(self, argv, *, timeout=None, output_limit=None):
        args = list(argv)[3:]  # _base_command 的程序、-s 和 serial。
        if not args or args[0] not in ("shell", "exec-out"):
            raise ActionFailedError(APP_UNSUPPORTED)
        if args[0] == "exec-out" and args[1:2] not in (["screencap"], ["uiautomator"], ["cat"]):
            raise ActionFailedError(APP_UNSUPPORTED)
        if args[0] == "exec-out" and args[1:2] == ["cat"] and not all(x.startswith("/sdcard/") and x.endswith(".xml") for x in args[2:]):
            raise ActionFailedError(APP_UNSUPPORTED)
        command = args[1] if args[0] == "shell" else shlex.join(args[1:])
        if args == ["exec-out", "uiautomator", "dump", "/dev/tty"]:
            value = await self.bridge.call("nodes", limit=8 * 1024 * 1024, timeout=timeout or self._command_timeout)
        else:
            value = await self.bridge.call("shell", command=command, limit=output_limit or 8 * 1024 * 1024,
                                           timeout=timeout or self._command_timeout)
        return int(value["rc"]), base64.b64decode(value.get("stdout", ""), validate=True), base64.b64decode(value.get("stderr", ""), validate=True)

    async def _execute(self, argv, *, timeout=None, retry=True):
        rc, out, err = await self._run_adb(argv, timeout=timeout)
        if rc:
            raise AdbCommandError("App ADB 命令失败", returncode=rc, detail=err.decode("utf-8", "replace")[:500])
        return rc, out, err

    async def read_clipboard(self):
        value = await self.bridge.call("clipboard", timeout=self._command_timeout)
        return value.get("text", "")

    async def input_text(self, text, *, press_enter=False):
        await self.bridge.call("input", text=text, press_enter=press_enter, timeout=self._command_timeout)

    async def disconnect(self):
        self._connected = False

    async def close(self):
        await self.bridge.close()
        self._connected = False

    async def reconfigure(self, **kwargs):
        raise ActionFailedError("App 连接配置已保存，请停止任务并重新加载插件")
