"""文件传输边界、分块、校验及原子提交。"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
from types import SimpleNamespace
import struct
import pytest
from core.app_files import AppFiles, phone_path, CHUNK
from core.errors import ActionFailedError
import core.app_files as module


class Lease:
    def __init__(self,*args):pass
    async def arm(self,*args,**kwargs):pass
    async def close(self):pass


class Streams:
    def __init__(self,response):self.response=response;self.output=bytearray()
    @asynccontextmanager
    async def connect(self,service):
        assert service=='sync:'
        reader=asyncio.StreamReader();reader.feed_data(self.response);reader.feed_eof()
        owner=self
        class Writer:
            def write(self,data):owner.output.extend(data)
            async def drain(self):await asyncio.sleep(0)
        yield reader,Writer()


def test_streaming_push_verifies_hash_before_rename(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'DeviceLease',Lease)
    payload=bytes(range(256))*(CHUNK//256+1);source=tmp_path/'source';source.write_bytes(payload)
    streams=Streams(b'OKAY'+struct.pack('<I',0));commands=[]
    async def shell(command,**kwargs):
        commands.append(command)
        if command.startswith('sha256sum '):return hashlib.sha256(payload).hexdigest()+' file'
        return ''
    async def run():
        files=AppFiles(SimpleNamespace(bridge=SimpleNamespace(streams=streams),shell=shell))
        assert await files.push(source,'/sdcard/Download/data.bin')==len(payload)
        wire=streams.output;assert wire.startswith(b'SEND') and payload[:CHUNK] in wire
        assert any(c.startswith('mv -f ') for c in commands)
    asyncio.run(run())


@pytest.mark.parametrize('corrupt',[False,True])
def test_pull_verifies_size_hash_and_preserves_existing_file(tmp_path,corrupt):
    payload=b'abcd'*20000
    response=b'STAT'+struct.pack('<III',0o100644,len(payload),1)
    for at in range(0,len(payload),CHUNK):
        chunk=payload[at:at+CHUNK];response+=b'DATA'+struct.pack('<I',len(chunk))+chunk
    response+=b'DONE'+struct.pack('<I',0)
    streams=Streams(response);target=tmp_path/'target';target.write_bytes(b'previous')
    async def shell(command,**kwargs):return ('0'*64 if corrupt else hashlib.sha256(payload).hexdigest())+' file'
    async def run():
        files=AppFiles(SimpleNamespace(bridge=SimpleNamespace(streams=streams),shell=shell))
        if corrupt:
            with pytest.raises(ActionFailedError,match='SHA256'):await files.pull('/sdcard/Download/data.bin',target)
            assert target.read_bytes()==b'previous'
        else:
            assert await files.pull('/sdcard/Download/data.bin',target)==len(payload)
            assert target.read_bytes()==payload
        assert not list(tmp_path.glob('*.part'))
    asyncio.run(run())


def test_pull_rejects_oversized_block_before_reading_payload(tmp_path):
    response=b'STAT'+struct.pack('<III',0o100644,100000,1)+b'DATA'+struct.pack('<I',CHUNK+1)
    files=AppFiles(SimpleNamespace(bridge=SimpleNamespace(streams=Streams(response))))
    async def run():
        with pytest.raises(ActionFailedError,match='长度'):await files.pull('/sdcard/test',tmp_path/'target')
        assert not list(tmp_path.iterdir())
    asyncio.run(run())


@pytest.mark.parametrize('path',['/proc/1/mem','/sdcard/../data/secret','/sdcard/abc\nxyz','relative','/data/private'])
def test_transfer_path_boundary(path):
    with pytest.raises(ActionFailedError):phone_path(path)
