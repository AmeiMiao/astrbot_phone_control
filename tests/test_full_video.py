"""完整点播优先原文件，直播与超阈值文件保持有序分段。"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
from core.config import PhoneConfig
from core.content_task import ContentTasks
from core.full_video import FullVideoWatch, FullVideoTooLarge, download_video, validate_source_url
from core.errors import PhoneControlError


def test_full_video_defaults_and_threshold_bounds():
    config=PhoneConfig.from_mapping({})
    assert config.media.full_video_enabled is False and config.media.full_video_max_mb==6
    assert PhoneConfig.from_mapping({'media':{'full_video_max_mb':100}}).media.full_video_max_mb==6
    assert PhoneConfig.from_mapping({'media':{'full_video_max_mb':0}}).media.full_video_max_mb==.25


def test_source_must_be_observed_and_cannot_target_local_metadata():
    with pytest.raises(PhoneControlError,match='观测'):
        validate_source_url('https://cdn.invalid/video.mp4',set())
    for url in ('http://127.0.0.1/video.mp4','http://169.254.169.254/video.mp4','file:///tmp/video.mp4','http://[::ffff:127.0.0.1]/video.mp4'):
        with pytest.raises(PhoneControlError):validate_source_url(url,{url})


@pytest.mark.parametrize('length,chunks,status,expected',[(100,[b'data'],200,'阈值'),(None,[b'abcd',b'efgh'],200,'阈值'),(4,[b'data'],206,'完整文件')])
def test_download_checks_announced_and_actual_size_and_completeness(tmp_path,monkeypatch,length,chunks,status,expected):
    async def run():
        import aiohttp
        class Content:
            async def iter_chunked(self,size):
                for chunk in chunks:yield chunk
        class Response:
            headers={};content_length=length;content=Content()
            async def __aenter__(self):return self
            async def __aexit__(self,*a):pass
        Response.status=status
        class Session:
            def __init__(self,**kw):self.connector=kw['connector']
            async def __aenter__(self):return self
            async def __aexit__(self,*a):await self.connector.close()
            def get(self,*a,**kw):return Response()
        monkeypatch.setattr(aiohttp,'ClientSession',Session)
        url='https://cdn.invalid/video.mp4';out=tmp_path/'video.mp4'
        with pytest.raises(PhoneControlError,match=expected):
            await download_video(url,out,maximum=6,observed={url})
        assert not out.exists() and not out.with_suffix('.part').exists()
    asyncio.run(run())


def test_live_never_attempts_full_download(tmp_path):
    async def run():
        async def segment(*a,**kw):return {'summary':'直播片段'}
        service=SimpleNamespace(_watch_segment=segment)
        manager=SimpleNamespace(service=service,active={},owner=lambda _: 'owner')
        result=await FullVideoWatch(manager).watch(None,stream_kind='live',video_url='https://unused.invalid/video.mp4')
        assert result['watch_mode']=='segments' and '直播' in result['segment_reason']
    asyncio.run(run())


def test_original_file_is_analysed_whole_without_recording(tmp_path,monkeypatch):
    async def run():
        async def foreground():return 'test.player','Video'
        s=SimpleNamespace(guard_device=lambda _:None,data_dir=tmp_path,config=PhoneConfig.from_mapping({'model':{'video_provider_id':'video'}}),actions=SimpleNamespace(foreground=foreground),app_context=SimpleNamespace(get_provider_by_id=lambda _:SimpleNamespace()),record_internal_usage=lambda *a,**kw:None)
        m=ContentTasks(s)
        async def download(url,path,**kw):path.write_bytes(b'whole-original-video');return path
        async def info(path):return 9,{'streams':[{'codec_type':'video'},{'codec_type':'audio'}]}
        async def generate(**kw):
            assert kw['video_path'].read_bytes()==b'whole-original-video'
            assert '完整点播视频' in kw['prompt'] and '9.000' in kw['prompt']
            return SimpleNamespace(completion_text='整条视频总结',usage=None)
        monkeypatch.setattr('core.full_video.download_video',download)
        monkeypatch.setattr('core.full_video.video_info',info)
        m.providers.generate=generate
        r=await m.full.watch(SimpleNamespace(unified_msg_origin='owner'),stream_kind='video',video_url='https://cdn.invalid/video.mp4')
        assert r['full_video'] and r['understood_until']==9 and r['source']=='original_file'
        assert not m.current(SimpleNamespace(unified_msg_origin='owner')).get('_sampler')
        assert not list(m.root.glob('*/original-video.mp4'))
        await m.close()
    asyncio.run(run())


def recording_manager(tmp_path):
    async def foreground():return 'test.player','Video'
    s=SimpleNamespace(data_dir=tmp_path,config=PhoneConfig.from_mapping({'model':{'video_provider_id':'video'}}),actions=SimpleNamespace(foreground=foreground),guard_device=lambda _:None,app_context=SimpleNamespace(get_provider_by_id=lambda _:SimpleNamespace()),record_internal_usage=lambda *a,**kw:None)
    return ContentTasks(s)


def test_recording_fallback_returns_whole_after_confirmed_start(tmp_path,monkeypatch):
    async def run():
        m=recording_manager(tmp_path)
        calls=[]
        async def capture(task,**kwargs):
            assert kwargs=={'play_x':10,'play_y':20}
            calls.append('play_from_start');task.update(_capture_finished=True,_recorded_until=9)
        async def encode(frames,start,end,audio,out,**kwargs):
            assert (start,end)==(0,9) and kwargs['maximum_bytes']==6*1024*1024
            out.write_bytes(b'whole-recorded-video');return out
        async def generate(**kwargs):
            assert kwargs['video_path'].read_bytes()==b'whole-recorded-video'
            calls.append('whole_model');return SimpleNamespace(completion_text='整条录制的内容',usage=None)
        m.start_capture=capture;m.providers.generate=generate
        monkeypatch.setattr('core.media_video.encode_segment',encode)
        r=await m.full.watch(SimpleNamespace(unified_msg_origin='owner'),stream_kind='video',duration_seconds=9,play_x=10,play_y=20)
        assert r['watch_mode']=='full' and r['source']=='recording' and r['understood_until']==9
        assert calls==['play_from_start','whole_model']
        await m.close()
    asyncio.run(run())


def test_oversized_recording_falls_back_to_existing_cache(tmp_path,monkeypatch):
    async def run():
        from core.media_video import VideoTooLarge
        m=recording_manager(tmp_path)
        async def capture(task,**kw):task.update(_capture_finished=True,_recorded_until=9)
        async def encode(*a,**kw):raise VideoTooLarge('large')
        async def read(event,**kw):return {'understood_until':3,'summary':'缓存第一段'}
        m.start_capture=capture;m.read=read
        monkeypatch.setattr('core.media_video.encode_segment',encode)
        r=await m.full.watch(SimpleNamespace(unified_msg_origin='owner'),stream_kind='video',duration_seconds=9,play_x=10,play_y=20)
        assert r['watch_mode']=='segments' and r['understood_until']==3
        assert '大小阈值' in r['segment_reason']
        await m.close()
    asyncio.run(run())


def test_full_record_polling_does_not_submit_partial_video(tmp_path,monkeypatch):
    async def run():
        import core.full_video as module
        m=recording_manager(tmp_path);clock=SimpleNamespace(now=0)
        monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:clock.now))
        async def wait(_):clock.now+=1
        monkeypatch.setattr(module.asyncio,'sleep',wait)
        task={'id':'fixture','_full_target':300,'_start':0,'status':'active'}
        result=await m.full.poll_recording(None,task)
        assert result['status']=='recording_full' and result['remaining_seconds']==280
        assert not result.get('summary') and not result.get('full_video')
    asyncio.run(run())


def test_streaming_full_audio_export_matches_time_window(tmp_path):
    async def run():
        from core.media import BYTES_PER_SECOND
        import wave
        data=b'\x01\x00'*(BYTES_PER_SECOND//2)
        source=tmp_path/'source.pcm';source.write_bytes(data)
        target=tmp_path/'export.wav'
        await FullVideoWatch.export_audio(SimpleNamespace(path=source,size=len(data)),.1,.2,target)
        with wave.open(str(target),'rb') as f:
            assert f.getnframes()/f.getframerate()==.2
            assert f.readframes(f.getnframes())==data[int(.1*BYTES_PER_SECOND):int(.3*BYTES_PER_SECOND)]
    asyncio.run(run())


def test_finishing_owner_cancels_inflight_whole_video_request(tmp_path,monkeypatch):
    async def run():
        m=recording_manager(tmp_path);started=asyncio.Event()
        async def download(url,path,**kw):path.write_bytes(b'video');return path
        async def info(path):return 9,{'streams':[{'codec_type':'video'},{'codec_type':'audio'}]}
        async def generate(**kw):started.set();await asyncio.Event().wait()
        monkeypatch.setattr('core.full_video.download_video',download)
        monkeypatch.setattr('core.full_video.video_info',info)
        m.providers.generate=generate
        event=SimpleNamespace(unified_msg_origin='owner')
        pending=asyncio.create_task(m.full.watch(event,stream_kind='video',video_url='https://cdn.invalid/video.mp4'))
        await started.wait()
        await m.finish_owner('owner',abnormal=True)
        with pytest.raises(asyncio.CancelledError):await pending
        assert not m.active and not list(m.root.glob('*/original-video.mp4'))
    asyncio.run(run())
