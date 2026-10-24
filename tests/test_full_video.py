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
    for url in ('http://127.0.0.1/video.mp4','http://169.254.169.254/video.mp4','file:///tmp/video.mp4'):
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
        s=SimpleNamespace(data_dir=tmp_path,config=PhoneConfig.from_mapping({'model':{'video_provider_id':'video'}}),actions=SimpleNamespace(foreground=foreground),app_context=SimpleNamespace(get_provider_by_id=lambda _:SimpleNamespace()),record_internal_usage=lambda *a,**kw:None)
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
