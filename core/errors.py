"""插件异常体系。

所有对外可预期的失败都转成带中文提示的异常；工具层捕获后以 "error: ..." 文本
返回给模型，命令层捕获后以友好文案回复用户。
"""

from __future__ import annotations


class PhoneControlError(Exception):
    """插件异常基类。"""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or ""

    def __str__(self) -> str:
        return f"{self.message}（{self.detail}）" if self.detail else self.message


class AdbNotConfiguredError(PhoneControlError):
    """未配置 ADB 或 adb 二进制不存在。"""


class AdbConnectError(PhoneControlError):
    """ADB 连接设备失败（端口不通/未授权/离线）。"""


class AdbCommandError(PhoneControlError):
    """ADB 命令执行失败。"""

    def __init__(
        self,
        message: str,
        *,
        command: str = "",
        returncode: int | None = None,
        stderr: str = "",
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


class DeviceOfflineError(AdbCommandError):
    """设备掉线 / offline 状态。"""


class UiDumpError(PhoneControlError):
    """uiautomator dump 失败或 XML 无法解析。"""


class AppNotInstalledError(PhoneControlError):
    """目标 App 未安装。"""


class AppNotForegroundError(PhoneControlError):
    """目标 App 未能切到前台。"""


class ActionFailedError(PhoneControlError):
    """单次设备动作失败（参数非法/超时等）。"""


class PermissionDeniedError(PhoneControlError):
    """调用者无权限使用该能力。"""


class BrowseError(PhoneControlError):
    """浏览流程失败。"""


class MediaCaptureError(PhoneControlError):
    """媒体音频采集失败（scrcpy server 未启动/无音频/连接中断）。"""
