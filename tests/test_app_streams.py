"""真实TLS二进制通道的会话绑定、一次性授权及断线撤销。"""
import asyncio
import ssl
import pytest
from core.app_bridge import AppBridge
from core.app_streams import valid_service


@pytest.mark.parametrize('service',['tcp:5555','localabstract:other','sync:/etc','artifact:../key',''])
def test_service_rejects_arbitrary_targets(service):
    assert not valid_service(service)


def test_binary_tls_session_binding_replay_and_disconnect(tmp_path):
    async def run():
        bridge=AppBridge(tmp_path,tmp_path/'app.apk');await bridge.start(0)
        ctx=ssl.create_default_context(cafile=str(tmp_path/'server.pem'));ctx.check_hostname=False
        async def peer(hello):
            reader,writer=await asyncio.open_connection('127.0.0.1',bridge.port,ssl=ctx)
            await bridge.send(writer,hello)
            return reader,writer
        main_reader,main_writer=await peer({'v':1,'token':bridge._state['token'],'device_id':'fixture','features':['binary_streams']})
        ready=await bridge.read(main_reader)
        connected=asyncio.Event();hold=asyncio.Event()
        payload=bytes(range(256))*4096
        async def consume():
            async with bridge.streams.connect('sync:') as (reader,writer):
                writer.write(payload);await writer.drain()
                assert await reader.readexactly(len(payload))==payload
                connected.set()
                assert await reader.read()==b''
                await hold.wait()
        consumer=asyncio.create_task(consume())
        try:
            call=await bridge.read(main_reader)
            hello={'v':1,'type':'stream','token':bridge._state['token'],'device_id':'fixture',
                   'session':ready['session'],'stream_id':call['params']['stream_id']}
            for key in ('token','device_id','session','stream_id'):
                reader,writer=await peer({**hello,key:'wrong'})
                assert await asyncio.wait_for(reader.read(),2)==b''
                writer.close();await writer.wait_closed()
            reader,writer=await peer(hello)
            assert (await bridge.read(reader))['type']=='stream_ready'
            await bridge.send(main_writer,{'type':'result','id':call['id'],'ok':True})
            received=await reader.readexactly(len(payload));assert received==payload
            writer.write(received);await writer.drain();await asyncio.wait_for(connected.wait(),3)
            duplicate,duplicate_writer=await peer(hello)
            assert await asyncio.wait_for(duplicate.read(),2)==b''
            duplicate_writer.close();await duplicate_writer.wait_closed()
            main_writer.close();await main_writer.wait_closed()
            assert await asyncio.wait_for(reader.read(),3)==b''
            hold.set();await consumer
            assert not bridge.streams.entries
            writer.close();await writer.wait_closed()
        finally:
            hold.set();consumer.cancel();await asyncio.gather(consumer,return_exceptions=True)
            main_writer.close();await bridge.close()
    asyncio.run(run())


def test_forward_close_releases_live_local_client(tmp_path):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from core.app_adb import AppAdbClient
    async def run():
        entered=asyncio.Event();released=asyncio.Event()
        class Streams:
            @asynccontextmanager
            async def connect(self,service):
                reader=asyncio.StreamReader()
                class Writer:
                    def write(self,data):pass
                    async def drain(self):pass
                entered.set()
                try:yield reader,Writer()
                finally:released.set()
        adb=AppAdbClient(SimpleNamespace(streams=Streams()),tmp_path)
        port=int(await adb.run(['forward','tcp:0','localabstract:scrcpy_12345678']))
        reader,writer=await asyncio.open_connection('127.0.0.1',port)
        try:
            await entered.wait()
            await asyncio.wait_for(adb.disconnect(),2)
            assert await reader.read()==b'' and released.is_set() and not adb._forwards
        finally:writer.close();await writer.wait_closed()
    asyncio.run(run())


def test_file_window_waits_for_authenticated_phone_progress_and_disconnect(tmp_path):
    from core.errors import DeviceOfflineError
    async def run():
        bridge=AppBridge(tmp_path,tmp_path/'app.apk');await bridge.start(0)
        ctx=ssl.create_default_context(cafile=str(tmp_path/'server.pem'));ctx.check_hostname=False
        reader,writer=await asyncio.open_connection('127.0.0.1',bridge.port,ssl=ctx)
        await bridge.send(writer,{'v':1,'token':bridge._state['token'],'device_id':'flow','features':['binary_streams','stream_progress']})
        ready=await bridge.read(reader)
        drained=asyncio.Event();again=asyncio.Event()
        payload=b'x'*65536
        async def produce():
            async with bridge.streams.connect('sync:') as (_response,upload):
                upload.write(payload);await upload.drain();drained.set()
                await again.wait()
                upload.write(payload)
                with pytest.raises(DeviceOfflineError):await upload.drain()
        task=asyncio.create_task(produce())
        data_writer=None
        try:
            call=await bridge.read(reader);assert call['params']['flow_control'] is True
            identity=call['params']['stream_id']
            data_reader,data_writer=await asyncio.open_connection('127.0.0.1',bridge.port,ssl=ctx)
            await bridge.send(data_writer,{'v':1,'type':'stream','token':bridge._state['token'],'device_id':'flow','session':ready['session'],'stream_id':identity})
            assert (await bridge.read(data_reader))['type']=='stream_ready'
            await bridge.send(writer,{'type':'result','id':call['id'],'ok':True})
            assert await data_reader.readexactly(len(payload))==payload
            await asyncio.sleep(.02);assert not drained.is_set()
            progress={'type':'stream_progress','session':ready['session'],'stream_id':identity,'received_bytes':len(payload)}
            await bridge.send(writer,progress);await asyncio.wait_for(drained.wait(),2)
            for invalid in ({'received_bytes':len(payload)+1},{'received_bytes':True},{'received_bytes':0},{'session':'stale'}):
                with pytest.raises(ValueError):bridge.streams.progress({**progress,**invalid})
            again.set();assert await data_reader.readexactly(len(payload))==payload
            await bridge.close();await asyncio.wait_for(task,2)
            assert not bridge.streams.entries
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            if data_writer is not None:data_writer.close()
            writer.close();await bridge.close()
    asyncio.run(run())
