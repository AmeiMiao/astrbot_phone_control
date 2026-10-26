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
