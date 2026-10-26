"""手机推帧缓存的新鲜度、操作代次及断线约束。"""
import asyncio
from contextlib import asynccontextmanager
import io
import json
from types import SimpleNamespace
import struct
import time
import pytest
from PIL import Image
from core.app_frames import AppFrames
from core.errors import ActionFailedError


def jpeg(color):
    out=io.BytesIO();Image.new('RGB',(12,16),color).save(out,format='JPEG');return out.getvalue()


def frame(data,generation=0,age=0):
    return {'data':data,'meta':{'generation':generation,'age_ms':age,'captured_at_ms':int(time.monotonic()*1000)},'session':'session',
            'received':time.monotonic(),'validated':False}


def test_push_cache_rejects_old_operation_generation_and_expired_image():
    async def run():
        adb=SimpleNamespace(bridge=SimpleNamespace(session='session',writer=True),_video_generation=7,_video_clock_offset_ms=0)
        frames=AppFrames(adb);frames.task=asyncio.create_task(asyncio.sleep(30))
        old=jpeg('red');fresh=jpeg('blue');frames.latest=frame(old,6)
        async def deliver():
            await asyncio.sleep(.02);frames.latest=frame(fresh,7);frames.changed.set()
        producer=asyncio.create_task(deliver())
        try:
            data,fmt,_=await frames.capture(1)
            assert data==fresh and fmt=='jpeg' and frames.latest['validated']
            frames.latest['received']-=1
            with pytest.raises(ActionFailedError,match='操作后'):await frames.capture(.03)
            frames.latest=frame(fresh,7);frames.error='control disconnected'
            with pytest.raises(ActionFailedError,match='disconnected'):await frames.capture(.1)
        finally:
            await producer;await frames.close()
    asyncio.run(run())


def test_same_frame_is_validated_once_and_cache_does_not_request_phone():
    async def run():
        frames=AppFrames(SimpleNamespace(bridge=SimpleNamespace(session='session',writer=True),_video_generation=0,_video_clock_offset_ms=0))
        frames.task=asyncio.create_task(asyncio.sleep(30));frames.latest=frame(jpeg('green'))
        calls=[];original=frames.validate
        def validate(data):calls.append(1);original(data)
        frames.validate=validate
        try:
            one=await frames.capture(1);two=await frames.capture(1)
            assert one[0]==two[0] and calls==[1]
        finally:await frames.close()
    asyncio.run(run())


@pytest.mark.parametrize('meta,length',[({'generation':True,'age_ms':0},1),({'generation':1,'age_ms':-1},1),({'generation':1,'age_ms':0},9*1024*1024)])
def test_binary_frame_bounds_before_allocation(meta,length):
    async def run():
        metadata={**meta,'captured_at_ms':int(time.monotonic()*1000)}
        header=json.dumps(metadata).encode();reader=asyncio.StreamReader()
        reader.feed_data(struct.pack('!I',len(header))+header+struct.pack('!I',length));reader.feed_eof()
        class Streams:
            @asynccontextmanager
            async def connect(self,service):
                assert service=='frames:';yield reader,None
        bridge=SimpleNamespace(session='session',streams=Streams())
        frames=AppFrames(SimpleNamespace(bridge=bridge))
        await frames.receive()
        assert frames.error and frames.latest is None and frames.retry_at>time.monotonic()
    asyncio.run(run())


def test_network_queue_delay_cannot_relabel_old_frame_as_fresh():
    async def run():
        now=time.monotonic()*1000
        adb=SimpleNamespace(bridge=SimpleNamespace(session='session',writer=True),_video_generation=0,
                            _video_clock_offset_ms=now-1000-500)
        frames=AppFrames(adb);frames.task=asyncio.create_task(asyncio.sleep(30))
        frames.latest=frame(jpeg('green'));frames.latest['meta']['captured_at_ms']=1000
        try:
            with pytest.raises(ActionFailedError,match='操作后'):await frames.capture(.03)
        finally:await frames.close()
    asyncio.run(run())


def test_closed_control_connection_rejects_fresh_cached_frame():
    from core.errors import DeviceOfflineError
    async def run():
        bridge=SimpleNamespace(session='session',writer=None)
        frames=AppFrames(SimpleNamespace(bridge=bridge,_video_generation=0,_video_clock_offset_ms=0))
        frames.latest=frame(jpeg('blue'))
        with pytest.raises(DeviceOfflineError):await frames.capture(1)
        assert frames.task is None
    asyncio.run(run())


def test_weak_network_policy_keeps_generation_guard_and_reports_age():
    async def run():
        adb=SimpleNamespace(bridge=SimpleNamespace(session='session',writer=True),_video_generation=8,_video_clock_offset_ms=0,_frame_max_age_ms=1200)
        frames=AppFrames(adb);frames.task=asyncio.create_task(asyncio.sleep(30))
        frames.latest=frame(jpeg('blue'),8,age=600)
        try:
            assert (await frames.capture(.1))[1]=='jpeg'
            assert adb.last_screen_meta['frame_age_ms']>=600
            assert adb.last_screen_meta['frame_age_limit_ms']==1200
            frames.latest['meta']['generation']=7
            with pytest.raises(ActionFailedError,match='操作后'):await frames.capture(.03)
        finally:await frames.close()
    asyncio.run(run())


def test_reduced_frame_restores_touch_coordinate_dimensions():
    data=jpeg('blue')
    restored=AppFrames.restore_size(data,{'screen_width':24,'screen_height':32})
    with Image.open(io.BytesIO(restored)) as image:assert image.size==(24,32)
    for meta in ({'screen_width':9000,'screen_height':32},{'screen_width':True,'screen_height':32},{'screen_width':24,'screen_height':60}):
        with pytest.raises(ActionFailedError):AppFrames.restore_size(data,meta)


def test_frame_credit_acknowledges_complete_frame_and_rejects_replay():
    async def run():
        packet=jpeg('blue');now=int(time.monotonic()*1000)
        metadata={'generation':1,'captured_at_ms':now,'age_ms':0,'frame_id':1}
        header=json.dumps(metadata).encode();wire=struct.pack('!I',len(header))+header+struct.pack('!I',len(packet))+packet
        reader=asyncio.StreamReader();reader.feed_data(wire+wire);reader.feed_eof()
        sent=[]
        class Writer:
            def write(self,data):sent.append(data)
            async def drain(self):pass
        class Streams:
            @asynccontextmanager
            async def connect(self,service):yield reader,Writer()
        bridge=SimpleNamespace(session='session',streams=Streams(),peer={'features':['frame_ack']})
        frames=AppFrames(SimpleNamespace(bridge=bridge))
        await frames.receive()
        assert sent==[b'FACK'+struct.pack('!Q',1)]
        assert frames.latest['data']==packet and '序号无效' in frames.error
    asyncio.run(run())
