"""真实 socket 分包、连续读取、控制断流与超时协议回归。"""
import asyncio
from pathlib import Path
import socket
import struct
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.scrcpy import ScrcpyClient, ScrcpyError


def test_clipboard_reads_current_unicode_response_without_cached_text():
    async def run():
        left,right=socket.socketpair()
        client=ScrcpyClient(None,server_jar='unused',video=False)
        client._started=True;client._control_socket=left
        async def device():
            for text in ('分享 https://example.org/post/1?标题=猫','https://example.org/post/2'):
                assert await asyncio.to_thread(right.recv,2)==b'\x08\x00'
                raw=text.encode()
                packet=b'\x01'+struct.pack('>Q',7)+b'\x02'+struct.pack('>HH',3,2)+b'xy'+b'\x00'+struct.pack('>I',len(raw))+raw
                for i in range(0,len(packet),3):
                    right.sendall(packet[i:i+3]);await asyncio.sleep(.001)
        task=asyncio.create_task(device())
        try:
            assert await client.get_clipboard()=='分享 https://example.org/post/1?标题=猫'
            assert await client.get_clipboard()=='https://example.org/post/2'
            await task
        finally:left.close();right.close()
    asyncio.run(run())


@pytest.mark.parametrize('payload',[b'\x00'+struct.pack('>I',1<<20),b'\xff',b'\x00\x00'])
def test_bad_clipboard_messages_close_control_stream(payload):
    async def run():
        left,right=socket.socketpair();client=ScrcpyClient(None,server_jar='unused',video=False)
        client._started=True;client._control_socket=left
        right.sendall(payload);right.shutdown(socket.SHUT_WR)
        try:
            with pytest.raises(ScrcpyError):await client.get_clipboard(timeout=.1)
            assert client._stream_failed
        finally:left.close();right.close()
    asyncio.run(run())


def test_timeout_and_cancellation_do_not_leave_stale_response_reader():
    async def run():
        for cancel in (False,True):
            left,right=socket.socketpair();client=ScrcpyClient(None,server_jar='unused',video=False)
            client._started=True;client._control_socket=left
            try:
                if cancel:
                    task=asyncio.create_task(client.get_clipboard(timeout=1))
                    await asyncio.sleep(.02);task.cancel()
                    with pytest.raises(asyncio.CancelledError):await task
                else:
                    with pytest.raises(ScrcpyError,match='超时'):await client.get_clipboard(timeout=.02)
                assert client._stream_failed
            finally:left.close();right.close()
    asyncio.run(run())
