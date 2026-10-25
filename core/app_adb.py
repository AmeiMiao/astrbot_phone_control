"""复用设备控制契约，通过 Relay App 的本机 ADB 执行。"""

from __future__ import annotations
import base64
import shlex
import io
import time
from .adb import AdbClient
from .app_bridge import APP_UNSUPPORTED
from .errors import ActionFailedError, AdbCommandError, DeviceOfflineError


class AppAdbClient(AdbClient):
    app_mode = True

    def __init__(
        self,
        bridge,
        home_dir,
        *,
        command_timeout=30,
        logger=None,
        screen_source="scrcpy",
    ):
        super().__init__(
            None,
            "relay-app",
            1,
            home_dir,
            defer_binary=True,
            command_timeout=command_timeout,
            logger=logger,
        )
        self.bridge = bridge
        self._video_screen = screen_source == "scrcpy"

    @property
    def screen_source(self):
        version = str(getattr(self.bridge, "peer", {}).get("version", ""))
        parts = version.split(".")
        legacy = (
            len(parts) >= 2
            and parts[0] == "0"
            and parts[1].isdigit()
            and int(parts[1]) < 4
        )
        return "app_scrcpy" if self._video_screen and not legacy else "app_screencap"

    async def capture_screen(self, *, fast=False, timeout=None):
        if self.screen_source != "app_scrcpy":
            return await super().capture_screen(fast=fast, timeout=timeout)
        started = time.perf_counter()
        value = await self.bridge.call(
            "screen", timeout=timeout or self._command_timeout
        )
        data = base64.b64decode(value.get("image", ""), validate=True)
        from PIL import Image

        age = value.get("age_ms")
        if (
            value.get("source") != "app_scrcpy"
            or value.get("format") != "jpeg"
            or len(data) > 8 * 1024 * 1024
            or type(age) is not int
            or not 0 <= age <= 2000
        ):
            raise ActionFailedError("手机 scrcpy 视频帧格式无效，请检查 App 版本")
        with Image.open(io.BytesIO(data)) as frame:
            if frame.width * frame.height > 9000000 or frame.format != "JPEG":
                raise ActionFailedError("手机 scrcpy 视频帧尺寸无效")
            frame.load()
        return data, "jpeg", round((time.perf_counter() - started) * 1000)

    async def screenshot_png(self, *, timeout=None):
        if self.screen_source != "app_scrcpy":
            return await super().screenshot_png(timeout=timeout)
        data, _format, _duration = await self.capture_screen(timeout=timeout)
        from PIL import Image

        out = io.BytesIO()
        with Image.open(io.BytesIO(data)) as frame:
            frame.save(out, format="PNG")
        return out.getvalue()

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
        if args[0] == "exec-out" and args[1:2] not in (
            ["screencap"],
            ["uiautomator"],
            ["cat"],
        ):
            raise ActionFailedError(APP_UNSUPPORTED)
        if (
            args[0] == "exec-out"
            and args[1:2] == ["cat"]
            and not all(
                x.startswith("/sdcard/") and x.endswith(".xml") for x in args[2:]
            )
        ):
            raise ActionFailedError(APP_UNSUPPORTED)
        command = args[1] if args[0] == "shell" else shlex.join(args[1:])
        if args == ["exec-out", "uiautomator", "dump", "/dev/tty"]:
            value = await self.bridge.call(
                "nodes", limit=8 * 1024 * 1024, timeout=timeout or self._command_timeout
            )
        else:
            value = await self.bridge.call(
                "shell",
                command=command,
                limit=output_limit or 8 * 1024 * 1024,
                timeout=timeout or self._command_timeout,
            )
        return (
            int(value["rc"]),
            base64.b64decode(value.get("stdout", ""), validate=True),
            base64.b64decode(value.get("stderr", ""), validate=True),
        )

    async def _execute(self, argv, *, timeout=None, retry=True):
        rc, out, err = await self._run_adb(argv, timeout=timeout)
        if rc:
            raise AdbCommandError(
                "App ADB 命令失败",
                returncode=rc,
                detail=err.decode("utf-8", "replace")[:500],
            )
        return rc, out, err

    async def read_clipboard(self):
        value = await self.bridge.call("clipboard", timeout=self._command_timeout)
        return value.get("text", "")

    async def dump_ui_output(self, path, *, timeout=None):
        if path == "/dev/tty":
            return await self.exec_out("uiautomator", "dump", path, timeout=timeout)
        from .ui_commands import ui_dump_command

        return (await self.shell(ui_dump_command(path), timeout=timeout)).encode()

    async def input_text(self, text, *, press_enter=False):
        await self.bridge.call(
            "input", text=text, press_enter=press_enter, timeout=self._command_timeout
        )

    async def disconnect(self):
        self._connected = False

    async def close(self):
        await self.bridge.close()
        self._connected = False

    async def reconfigure(self, **kwargs):
        raise ActionFailedError("App 连接配置已保存，请停止任务并重新加载插件")
