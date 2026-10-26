"""绑定控制会话的一次性认证二进制通道。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import secrets
import re

from .errors import ActionFailedError, DeviceOfflineError


def valid_service(service):
    return isinstance(service, str) and (
        service in ("sync:", "frames:")
        or re.fullmatch(r"localabstract:scrcpy_[0-9a-f]{8}", service) is not None
        or re.fullmatch(r"artifact:[0-9a-f]{32}", service) is not None
    )


class ProgressWriter:
    """以手机已写入本机ADB的字节数约束发送窗口。"""
    def __init__(self,stream,entry,bridge):self.stream=stream;self.entry=entry;self.bridge=bridge
    def __getattr__(self,name):return getattr(self.stream,name)
    def write(self,data):
        self.entry['sent']+=len(data)
        self.stream.write(data)
    async def drain(self):
        await self.stream.drain()
        entry=self.entry
        while entry['received']<entry['sent']:
            entry['progress'].clear()
            if entry['closed'] or self.bridge.writer is None or entry['session']!=self.bridge.session:
                raise DeviceOfflineError('手机数据连接已结束')
            await entry['progress'].wait()


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
        flow=service=='sync:' and 'stream_progress' in bridge.peer.get('features',[])
        frame_ack=service=='frames:' and 'frame_ack' in bridge.peer.get('features',[])
        entry = {"session": bridge.session, "future": asyncio.get_running_loop().create_future(),
                 "done": asyncio.Event(), "writer": None, 'flow':flow, 'sent':0,'received':0,'progress':asyncio.Event(),'closed':False}
        self.entries[nonce] = entry
        try:
            async with asyncio.timeout(timeout):
                await bridge.call("stream_open", stream_id=nonce, service=service, timeout=timeout, **({'flow_control':True} if flow else {}), **({'frame_ack':True} if frame_ack else {}))
                reader, writer = await entry["future"]
            yield reader, ProgressWriter(writer,entry,bridge) if flow else writer
        finally:
            self.entries.pop(nonce, None)
            entry['closed']=True;entry['progress'].set()
            entry["done"].set()
            writer = entry["writer"]
            if writer is not None:
                writer.close()
            future = entry["future"]
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def progress(self,value):
        if value.get('session')!=self.bridge.session:raise ValueError('数据进度会话无效')
        identity=value.get('stream_id')
        if not isinstance(identity,str) or re.fullmatch('[0-9a-f]{32}',identity) is None:raise ValueError('数据进度身份无效')
        entry=self.entries.get(identity)
        if entry is None:return
        received=value.get('received_bytes')
        if not entry['flow'] or type(received) is not int or not entry['received']<=received<=entry['sent']:
            raise ValueError('数据进度字节数无效')
        entry['received']=received;entry['progress'].set()

    async def accept(self, hello, reader, writer):
        bridge = self.bridge
        identity = hello.get("stream_id")
        entry = self.entries.get(identity) if isinstance(identity, str) else None
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
            entry['closed']=True;entry['progress'].set()
            if entry["writer"] is not None:
                entry["writer"].close()
            if not entry["future"].done():
                entry["future"].set_exception(DeviceOfflineError("手机数据通道随控制连接结束"))
            entry["done"].set()
