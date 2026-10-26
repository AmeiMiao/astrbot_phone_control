"""手机录制归属、成品校验与失败/断线清理。"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
from types import SimpleNamespace
import pytest
from core.app_recording import AppRecording
from core.transfers import TransferFiles
from core.errors import ActionFailedError


class Bridge:
    session='current-session'
    writer=True
    peer={'features':['media_recording','binary_streams']}
    def __init__(self):
        self.calls=[];self.payload=b'controlled-mp4-content';self.corrupt=False;self.identifier=None
        self.streams=self
    async def call(self,op,**params):
        self.calls.append((op,params))
        if op=='media_start':
            self.identifier=params['recording_id']
            return {'recording_id':self.identifier,'running':True}
        if op=='media_stop':
            return {'recording_id':self.identifier,'running':False,'ready':True,'mode':'both','size':len(self.payload),
                    'sha256':'0'*64 if self.corrupt else hashlib.sha256(self.payload).hexdigest()}
        return {'running':True}
    @asynccontextmanager
    async def connect(self,service):
        assert service=='artifact:'+self.identifier
        reader=asyncio.StreamReader();reader.feed_data(self.payload);reader.feed_eof()
        yield reader,None


def test_recording_owner_success_and_phone_cleanup(tmp_path):
    async def run():
        bridge=Bridge();files=TransferFiles(None,tmp_path);record=AppRecording(bridge,files)
        await record.start('owner',seconds=3)
        with pytest.raises(ActionFailedError,match='其他会话'):await record.stop('other')
        saved=await record.stop('owner')
        assert files.path(saved['file_id']).read_bytes()==bridge.payload
        assert record.current is None and bridge.calls[-1][0]=='media_delete'
    asyncio.run(run())


def test_recording_hash_failure_preserves_phone_artifact_for_retry(tmp_path):
    async def run():
        bridge=Bridge();bridge.corrupt=True
        record=AppRecording(bridge,TransferFiles(None,tmp_path))
        await record.start('owner',seconds=3)
        with pytest.raises(ActionFailedError,match='SHA256'):await record.stop('owner')
        assert record.current is not None and not list(tmp_path.iterdir())
        await record.cancel('owner')
        assert record.current is None
    asyncio.run(run())


def test_old_session_cannot_stop_new_recording(tmp_path):
    async def run():
        bridge=Bridge();record=AppRecording(bridge,TransferFiles(None,tmp_path))
        await record.start('owner',seconds=3);bridge.session='new-session'
        with pytest.raises(ActionFailedError,match='连接结束'):await record.stop('owner')
        assert not any(op=='media_stop' for op,_ in bridge.calls)
    asyncio.run(run())


@pytest.mark.parametrize('seconds',[0,901,float('inf'),float('nan')])
def test_recording_duration_boundary_before_phone_action(tmp_path,seconds):
    async def run():
        bridge=Bridge();record=AppRecording(bridge,TransferFiles(None,tmp_path))
        with pytest.raises(ActionFailedError):await record.start('owner',seconds=seconds)
        assert not bridge.calls
    asyncio.run(run())


def test_start_failure_keeps_original_error_and_cleans_owned_id(tmp_path):
    class Failed(Bridge):
        async def call(self,op,**params):
            self.calls.append((op,params))
            if op=='media_start':raise ActionFailedError('codec unavailable')
            return {'ok':True}
    async def run():
        bridge=Failed();record=AppRecording(bridge,TransferFiles(None,tmp_path))
        with pytest.raises(ActionFailedError,match='codec unavailable'):await record.start('owner',seconds=3)
        assert [x[0] for x in bridge.calls]==['media_start','media_delete'] and record.current is None
        assert bridge.calls[0][1]['recording_id']==bridge.calls[1][1]['recording_id']
    asyncio.run(run())
