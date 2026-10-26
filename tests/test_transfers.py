"""传输缓存身份边界和下载校验。"""
import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
import pytest
from core.transfers import TransferFiles
from core.errors import ActionFailedError


def test_download_uses_opaque_id_and_validates_checksum(tmp_path):
    payload=b'attachment-data'
    async def shell(command,**kwargs):
        return str(len(payload)) if command.startswith('test -f') else hashlib.sha256(payload).hexdigest()+' file'
    async def run(args,**kwargs):Path(args[2]).write_bytes(payload)
    async def scenario():
        files=TransferFiles(SimpleNamespace(shell=shell,run=run),tmp_path)
        result=await files.download('/sdcard/Download/test.bin')
        assert files.path(result['file_id']).read_bytes()==payload
        assert result['sha256']==hashlib.sha256(payload).hexdigest()
        assert 'path' not in result
    asyncio.run(scenario())


def test_transfer_id_blocks_traversal_and_symlink(tmp_path):
    files=TransferFiles(None,tmp_path/'transfers');files.root.mkdir()
    secret=tmp_path/'secret';secret.write_bytes(b'private')
    escaped=files.root/('a'*32+'.bin');escaped.symlink_to(secret)
    for identity in ('../secret',str(secret),'a'*32+'.bin'):
        with pytest.raises(ActionFailedError):files.path(identity)


def test_bad_download_does_not_leave_partial_or_replace_existing(tmp_path):
    payload=b'bad'
    async def shell(command,**kwargs):return '3' if command.startswith('test -f') else '0'*64+' file'
    async def run(args,**kwargs):Path(args[2]).write_bytes(payload)
    async def scenario():
        files=TransferFiles(SimpleNamespace(shell=shell,run=run),tmp_path)
        with pytest.raises(ActionFailedError,match='SHA256'):await files.download('/sdcard/file')
        assert not list(tmp_path.iterdir())
    asyncio.run(scenario())


def test_upload_imports_only_current_message_attachment(tmp_path,monkeypatch):
    import astrbot.api.message_components as components
    source=tmp_path/'attachment.bin';source.write_bytes(b'current chat attachment')
    class Attachment:
        name='user.bin'
        def get_file(self):return str(source)
    monkeypatch.setattr(components,'File',Attachment)
    async def run():
        files=TransferFiles(None,tmp_path/'transfers')
        identity=await files.attachment(SimpleNamespace(get_messages=lambda:[Attachment()]))
        assert files.path(identity).read_bytes()==source.read_bytes()
        assert identity!='user.bin'
        with pytest.raises(ActionFailedError):await files.attachment(SimpleNamespace(get_messages=lambda:[]))
    asyncio.run(run())
