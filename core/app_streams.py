"""绑定控制会话的一次性认证二进制通道。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import secrets
import re

from .errors import ActionFailedError, DeviceOfflineError


def valid_service(service):
    return isinstance(service, str) and (
        service == "sync:"
        or re.fullmatch(r"localabstract:scrcpy_[0-9a-f]{8}", service) is not None
        or re.fullmatch(r"artifact:[0-9a-f]{32}", service) is not None
    )


class AppStreams:
    def __init__(self, bridge):
        self.bridge = bridge
        self.entries = {}

    @asynccontextmanager
    async def connect(self, service, *, timeout=15):
        bridge = self.bridge
        if not valid_service(service):
            raise ActionFailedError("手机数据通道类型无效")
        if bridge.writer is None:
            raise DeviceOfflineError("Relay App 未连接")
        if "binary_streams" not in bridge.peer.get("features", []):
            raise ActionFailedError("请更新 Phone Relay App 以使用音频与文件传输")
        if len(self.entries) >= 8:
            raise ActionFailedError("手机数据通道已达到并发上限")
        nonce = secrets.token_hex(16)
        entry = {"session": bridge.session, "future": asyncio.get_running_loop().create_future(),
                 "done": asyncio.Event(), "writer": None}
        self.entries[nonce] = entry
        try:
            async with asyncio.timeout(timeout):
                await bridge.call("stream_open", stream_id=nonce, service=service, timeout=timeout)
                reader, writer = await entry["future"]
            yield reader, writer
        finally:
            self.entries.pop(nonce, None)
            entry["done"].set()
            writer = entry["writer"]
            if writer is not None:
                writer.close()
            future = entry["future"]
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def accept(self, hello, reader, writer):
        bridge = self.bridge
        entry = self.entries.get(hello.get("stream_id"))
        if (entry is None or entry["future"].done() or entry["writer"] is not None
                or hello.get("v") != 1 or bridge.writer is None
                or entry["session"] != bridge.session or hello.get("session") != bridge.session
                or hello.get("device_id") != bridge._state.get("device_id")
                or not secrets.compare_digest(str(hello.get("token", "")), bridge._state["token"])):
            return
        entry["writer"] = writer
        await bridge.send(writer, {"type": "stream_ready"})
        entry["future"].set_result((reader, writer))
        await entry["done"].wait()

    async def close(self):
        for entry in list(self.entries.values()):
            if entry["writer"] is not None:
                entry["writer"].close()
            if not entry["future"].done():
                entry["future"].set_exception(DeviceOfflineError("手机数据通道随控制连接结束"))
            entry["done"].set()
