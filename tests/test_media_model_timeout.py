"""模型超时归因、同一片段重试与明确结束的生命周期回归。"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.content_task import ContentTasks
from core.errors import MediaModelTimeout
from test_content_tasks import service


def test_model_timeout_cancels_request_and_identifies_provider(tmp_path):
    async def run():
        manager = ContentTasks(service(tmp_path))
        cancelled = asyncio.Event()
        async def request():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with pytest.raises(MediaModelTimeout, match="视频识别.*video-test.*0.01s"):
            await manager.model_request(request(), provider_id="video-test", stage="视频识别", timeout=.01)
        assert cancelled.is_set()
        await manager.close()
    asyncio.run(run())


def test_post_model_timeout_retains_material_and_retry_uses_same_task(tmp_path):
    async def run():
        s = service(tmp_path)
        s.app_context = SimpleNamespace()
        s._resolve_media_vision_provider_id = lambda *_: asyncio.sleep(0, result=("eyes", ""))
        s.record_internal_usage = lambda *a, **k: None
        s.config = replace(s.config, media=replace(s.config.media, vision_timeout_seconds=.01))
        m = ContentTasks(s)
        event = SimpleNamespace(unified_msg_origin="owner")
        task = await m.begin(event, kind="post", content_id="post")
        material = m.root / task['id'] / 'original.png'
        material.write_bytes(b'original')
        m.post_material = lambda _: ("正文", [material])
        cancelled = asyncio.Event()
        calls = []
        async def generate(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return SimpleNamespace(completion_text="实际摘要", usage=None)
        m.providers.generate = generate
        with pytest.raises(MediaModelTimeout, match="图文理解.*eyes.*游标未推进"):
            await m.read(event, task['id'])
        assert cancelled.is_set() and m.current(event) is task
        assert task['cursor'] == 0 and task['summaries'] == []
        assert material.read_bytes() == b'original' and '_reader' not in task
        result = await m.read(event, task['id'])
        assert result['summary'] == '实际摘要' and task['cursor'] == 1
        assert calls[0]['image_urls'] == calls[1]['image_urls']
        await m.finish_owner('owner', abnormal=False)
        assert not m.active and task['_sampler'].done()
        await m.close()
    asyncio.run(run())


def test_upstream_timeout_also_keeps_typed_stage(tmp_path):
    async def run():
        m = ContentTasks(service(tmp_path))
        async def request():
            raise TimeoutError('upstream')
        with pytest.raises(MediaModelTimeout, match="语音识别.*ears"):
            await m.model_request(request(), provider_id="ears", stage="语音识别", timeout=60)
        await m.close()
    asyncio.run(run())


def test_video_timeout_retries_same_range_and_finish_reclaims_capture(tmp_path, monkeypatch):
    async def run():
        s = service(tmp_path)
        s.config = replace(s.config, model=replace(s.config.model, video_provider_id='video-test'),
                           media=replace(s.config.media, audio_enabled=False, vision_timeout_seconds=.01))
        s.app_context = SimpleNamespace(get_provider_by_id=lambda _: SimpleNamespace())
        s.record_internal_usage = lambda *a, **k: None
        m = ContentTasks(s)
        event = SimpleNamespace(unified_msg_origin='owner')
        task = await m.begin(event, kind='video', content_id='video', capture=False)
        task['_start'] = time.monotonic() - 10
        frame = m.root / task['id'] / 'frame.jpg'
        frame.write_bytes(b'frame')
        task['frames'] = [[0, str(frame)], [3, str(frame)]]
        task['_sampler'] = asyncio.create_task(asyncio.sleep(100))
        ranges = []
        async def encode(frames, start, end, audio, path):
            ranges.append((start, end))
            path.write_bytes(b'video')
        monkeypatch.setattr('core.media_video.encode_segment', encode)
        async def generate(**kw):
            if len(ranges) == 1:
                await asyncio.Event().wait()
            return SimpleNamespace(completion_text='真实片段', usage=None)
        m.providers.generate = generate
        with pytest.raises(MediaModelTimeout, match='视频识别.*video-test'):
            await m.read(event, seconds=3)
        assert task['cursor'] == 0 and not task['summaries']
        assert not task['_sampler'].done() and frame.exists()
        result = await m.read(event, seconds=3)
        assert result['understood_until'] == 3 and ranges == [(0, 3), (0, 3)]
        assert task['summary_ranges'] == [[0, 3]]
        await m.finish_owner('owner', abnormal=True)
        assert task['_sampler'].done() and not frame.parent.exists()
        await m.close()
    import time
    asyncio.run(run())
