"""内容任务的真实文件生命周期、身份关联和音频连续性回归。"""

import asyncio
import json
import os
import socket
import threading
import time
from types import SimpleNamespace
import wave

import pytest
from core.config import PhoneConfig
from core.content_task import ContentTasks
from core.errors import PhoneControlError, MediaCaptureError
from core.media import AudioStream, BYTES_PER_SECOND, normalize_pcm
from core.post_material import load_post_material


def service(tmp_path, clean=True):
    async def foreground():
        return "test.app", "Post"

    return SimpleNamespace(
        data_dir=tmp_path,
        config=PhoneConfig.from_mapping(
            {
                "perception": {
                    "screenshot_auto_clean": clean,
                    "screenshot_retention_minutes": 1,
                }
            }
        ),
        actions=SimpleNamespace(foreground=foreground),
        guard_device=lambda _: None,
        traffic=None,
    )


def test_expired_assets_survive_active_task_and_clean_after_finish(tmp_path):
    async def run():
        m = ContentTasks(service(tmp_path))
        e = SimpleNamespace(unified_msg_origin="owner")
        t = await m.begin(e, kind="post", content_id="123")
        p = m.root / t["id"] / "frame-test.jpg"
        p.write_bytes(b"image")
        os.utime(p, (time.time() - 120,) * 2)
        m.cleanup()
        assert p.exists()
        await m.finish_owner("owner", abnormal=False)
        assert not p.exists()

    asyncio.run(run())


def test_abnormal_cleans_even_if_auto_clean_off_and_does_not_touch_other_files(
    tmp_path,
):
    async def run():
        s = service(tmp_path, False)
        m = ContentTasks(s)
        e = SimpleNamespace(unified_msg_origin="owner")
        keep = tmp_path / "app_memory.json"
        keep.write_text("memory")
        t = await m.begin(e, kind="post", content_id="123")
        p = m.root / t["id"] / "image.jpg"
        p.write_bytes(b"image")
        await m.finish_owner("owner", abnormal=True)
        assert not p.parent.exists()
        assert keep.read_text() == "memory"

    asyncio.run(run())


def test_other_session_cannot_take_content_or_end_owner(tmp_path):
    async def run():
        m = ContentTasks(service(tmp_path))
        a = SimpleNamespace(unified_msg_origin="a")
        b = SimpleNamespace(unified_msg_origin="b")
        t = await m.begin(a, kind="post", content_id="123")
        with pytest.raises(PhoneControlError):
            m.current(b, t["id"])
        with pytest.raises(PhoneControlError):
            await m.begin(b, kind="post", content_id="456")
        assert m.current(a)["content_id"] == "123"
        await m.close()

    asyncio.run(run())


def test_comments_and_reply_decisions_are_separate(tmp_path):
    async def run():
        m = ContentTasks(service(tmp_path))
        e = SimpleNamespace(unified_msg_origin="a")
        t = await m.begin(e, kind="post", content_id="123")
        with pytest.raises(PhoneControlError):
            await m.decide(e, "read_comments", "感兴趣")
        t["summaries"].append("已读正文")
        with pytest.raises(PhoneControlError):
            await m.decide(e, "reply_comment", "想回复", target="具体评论")
        decision = await m.decide(e, "read_comments", "对讨论有兴趣")
        assert decision["permission_granted"] is False
        assert [d["decision"] for d in t["decisions"]] == ["read_comments"]
        await m.close()

    asyncio.run(run())


def test_restart_reclaims_abandoned_tasks_and_preserves_completed(tmp_path):
    root = tmp_path / "content_tasks"
    a = root / ("a" * 32)
    b = root / ("b" * 32)
    for p, status in ((a, "active"), (b, "completed")):
        p.mkdir(parents=True)
        (p / "state.json").write_text(json.dumps({"status": status}))
        (p / "audio.wav").write_bytes(b"sound")
    ContentTasks(service(tmp_path, False))
    assert not a.exists()
    assert (b / "audio.wav").exists()


def test_only_same_content_original_images_are_used(tmp_path):
    (tmp_path / "post.body").write_text(
        json.dumps(
            {
                "data": {
                    "id": "123",
                    "title": "目标正文",
                    "images": ["https://img/target.png"],
                    "recommendations": [
                        {
                            "id": "456",
                            "content": "不要混入",
                            "images": ["https://img/other.png"],
                        }
                    ],
                }
            }
        )
    )
    for index, url in enumerate(
        ["https://api/posts/123", "https://img/target.png", "https://img/other.png"]
    ):
        record = {"url": url}
        if index == 0:
            record["text_path"] = "post.body"
        else:
            record["image_path"] = f"{index}.png"
            (tmp_path / f"{index}.png").write_bytes(b"png")
        (tmp_path / f"resource-{index}.json").write_text(json.dumps(record))
    text, images = load_post_material(tmp_path, "123")
    assert text == "目标正文"
    assert images == [tmp_path / "1.png"]


def test_continuous_audio_segment_cursor_has_no_gap_or_overlap(tmp_path):
    async def run():
        left, right = socket.socketpair()
        stream = AudioStream(None, tmp_path / "source.pcm", "output")
        # PCM 各秒有不同值，用实际 WAV 数据证明连续分段没有重放或跳过。
        pcm = b"".join(bytes([n]) * BYTES_PER_SECOND for n in range(1, 7))
        thread = threading.Thread(target=lambda: (right.sendall(pcm), right.close()))
        thread.start()
        read = asyncio.create_task(asyncio.to_thread(stream.read, left))
        while stream.size < len(pcm):
            await asyncio.sleep(0.01)
        stream.stop_event.set()
        try:
            await read
        except MediaCaptureError as exc:
            assert "提前断开" in str(exc)
        stream.task = asyncio.create_task(asyncio.sleep(0))
        p = await stream.segment(0, 3, tmp_path / "one.wav")
        q = await stream.segment(3, 6, tmp_path / "two.wav")
        with wave.open(str(p)) as f:
            a = f.readframes(f.getnframes())
        with wave.open(str(q)) as f:
            b = f.readframes(f.getnframes())
        assert a == normalize_pcm(pcm[: 3 * BYTES_PER_SECOND])
        assert b == normalize_pcm(pcm[3 * BYTES_PER_SECOND :])
        assert stream.path.read_bytes() == pcm
        left.close()
        thread.join()

    asyncio.run(run())


def test_failed_understanding_cleans_assets_preserves_summary_and_cursor(tmp_path):
    async def run():
        m = ContentTasks(service(tmp_path))
        e = SimpleNamespace(unified_msg_origin="a")
        t = await m.begin(e, kind="post", content_id="123")
        t["summaries"] = ["先前理解的摘要"]
        t["cursor"] = 1

        async def failure(*a, **k):
            raise RuntimeError("模型不可用")

        m.understand = failure
        m.post_material = lambda t: ("正文", [tmp_path / "image.png"])
        with pytest.raises(RuntimeError, match="模型不可用"):
            await m.read(e)
        assert t["cursor"] == 1 and not m.active
        assert not (m.root / t["id"]).exists()
        history = json.loads((m.root / "history.jsonl").read_text())
        assert history["summaries"] == ["先前理解的摘要"]

    asyncio.run(run())


def test_audio_placeholder_adapter_uses_real_stt_result_instead_of_claiming_audio(
    tmp_path,
):
    async def run():
        s = service(tmp_path)
        calls = []

        class ProviderAnthropic:
            pass

        class STT:
            async def get_text(self, path):
                return "实际转写文字"

        class Context:
            def get_provider_by_id(self, id):
                return ProviderAnthropic()

            async def llm_generate(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(completion_text="识别结果", usage=None)

        async def vision(*args):
            return "model", "model"

        async def stt(*args):
            return STT(), "STT"

        s.app_context = Context()
        s._resolve_media_vision_provider_id = vision
        s._resolve_media_stt_provider = stt
        s.record_internal_usage = lambda *a, **k: None
        m = ContentTasks(s)
        result = await m.understand(
            None,
            {"question": ""},
            "内容",
            [tmp_path / "image.png"],
            tmp_path / "audio.wav",
        )
        assert "实际转写文字" in calls[0]["prompt"]
        assert "audio_urls" not in calls[0]
        assert "已使用语音转写" in result

    asyncio.run(run())


def test_tool_task_return_does_not_end_content_capture(tmp_path):
    async def run():
        m = ContentTasks(service(tmp_path))
        e = SimpleNamespace(unified_msg_origin="owner")
        t = await asyncio.create_task(m.begin(e, kind="post", content_id="123"))
        await asyncio.sleep(0.05)
        assert m.current(e) is t
        assert t["status"] == "active"
        assert not t["_sampler"].done()
        await m.close()
        assert t["_sampler"].done()

    asyncio.run(run())


def test_slow_analysis_keeps_sampling_and_continues_from_cursor(tmp_path):
    async def run():
        s = service(tmp_path)
        s.config = PhoneConfig.from_mapping(
            {"media": {"audio_enabled": False, "frame_interval_seconds": 0.1}}
        )
        s.backends = SimpleNamespace(ensure=lambda: asyncio.sleep(0))
        count = []

        async def capture(**kw):
            count.append(time.monotonic())
            return b"frame", tmp_path / "screen.png", {}

        s.capture = capture
        m = ContentTasks(s)
        e = SimpleNamespace(unified_msg_origin="owner")
        t = await asyncio.create_task(m.begin(e, kind="video", content_id="video"))
        # 提前时间原点，真实采帧在分析阶段继续运行。
        t["_start"] -= 3
        segments = []

        async def understand(event, task, text, images, audio):
            segments.append((text, list(images)))
            before = len(count)
            await asyncio.sleep(1.2)
            assert len(count) > before
            assert m.status()[0]["analyzing"] is True
            return "内容摘要"

        m.understand = understand
        first = await asyncio.create_task(m.read(e, seconds=3))
        assert first["understood_until"] == 3
        assert first["captured_until"] > 3
        await m.decide(e, "continue", "想了解后续")
        t["_start"] -= 3
        second = await asyncio.create_task(m.read(e, seconds=3))
        assert second["understood_until"] == 6
        assert "3.0–6.0" in segments[1][0]
        await m.close()
        assert t["_sampler"].done()

    asyncio.run(run())

@pytest.mark.parametrize("stt", [True, False])
def test_explicit_audio_model_receives_audio_before_vision(tmp_path, stt):
    async def run():
        s = service(tmp_path)
        s.config = PhoneConfig.from_mapping({"model": {"asr_provider_id": "ears"}, "media": {"asr_provider_id": "legacy", "max_frames": 1}})
        calls = []
        class Ears:
            async def get_text(self, path):
                calls.append(("stt", path))
                return "先听见的原文"
        ears = Ears() if stt else SimpleNamespace()
        class Context:
            def get_provider_by_id(self, id):
                return ears if id == "ears" else SimpleNamespace()
            async def llm_generate(self, **kw):
                calls.append(kw)
                return SimpleNamespace(completion_text="先听见的原文" if kw['chat_provider_id'] == 'ears' else "画面理解", usage=None)
        async def vision(*args):
            return "eyes", ""
        s.app_context = Context()
        s._resolve_media_vision_provider_id = vision
        s.record_internal_usage = lambda *a, **k: None
        m = ContentTasks(s)
        result = await m.understand(None, {"question": ""}, "内容", [tmp_path/'1.png', tmp_path/'2.png'], tmp_path/'audio.wav')
        assert 'ears' in result
        assert len(calls) == 3
        if not stt:
            assert calls[0]['chat_provider_id'] == 'ears'
            assert calls[0]['audio_urls'] == [str(tmp_path/'audio.wav')]
        for call in calls[1:]:
            assert call['chat_provider_id'] == 'eyes'
            assert 'audio_urls' not in call
            assert '先听见的原文' in call['prompt']
    asyncio.run(run())


def test_missing_explicit_asr_does_not_send_audio_to_vision(tmp_path):
    async def run():
        s = service(tmp_path)
        s.config = PhoneConfig.from_mapping({"model": {"asr_provider_id": "deleted"}})
        s.app_context = SimpleNamespace(get_provider_by_id=lambda _: None)
        async def vision(*args): return "eyes", ""
        s._resolve_media_vision_provider_id = vision
        m = ContentTasks(s)
        with pytest.raises(PhoneControlError, match="未找到语音识别模型"):
            await m.understand(None, {"question": ""}, "内容", [], tmp_path/'audio.wav')
    asyncio.run(run())

@pytest.mark.parametrize('relaxed', [False, True])
def test_direct_continuation_is_optional_only_in_break_rule(tmp_path, relaxed):
    async def run():
        s = service(tmp_path)
        s.config = PhoneConfig.from_mapping({'auto': {'break_rule': relaxed}, 'media': {'audio_enabled': False}})
        s.backends = SimpleNamespace(ensure=lambda: asyncio.sleep(0))
        async def capture(**kw): return b'frame', tmp_path/'screen.png', {}
        s.capture = capture
        m = ContentTasks(s)
        e = SimpleNamespace(unified_msg_origin='owner')
        t = await m.begin(e, kind='video', content_id='live')
        t['_start'] -= 12
        t['frames'] = [[0, str(tmp_path/'0.jpg')], [3, str(tmp_path/'3.jpg')], [6, str(tmp_path/'6.jpg')]]
        async def understand(*a): return '真实片段摘要'
        m.understand = understand
        assert (await m.read(e, seconds=3))['understood_until'] == 3
        if not relaxed:
            with pytest.raises(PhoneControlError, match='continue'):
                await m.read(e, seconds=3)
            assert m.current(e) is t and (m.root/t['id']/'state.json').exists()
            assert t['cursor'] == 3 and not t.get('_reader')
            await m.decide(e, 'continue', '继续了解内容')
        assert (await m.read(e, seconds=3))['understood_until'] == 6
        assert len(t['summaries']) == 2
        await m.close()
    asyncio.run(run())


def test_invalid_segment_does_not_destroy_active_media(tmp_path):
    async def run():
        s = service(tmp_path)
        s.config = PhoneConfig.from_mapping({'media': {'audio_enabled': False}})
        s.backends = SimpleNamespace(ensure=lambda: asyncio.sleep(0))
        async def capture(**kw): return b'frame', tmp_path/'screen.png', {}
        s.capture = capture
        m = ContentTasks(s)
        e = SimpleNamespace(unified_msg_origin='owner')
        t = await m.begin(e, kind='video', content_id='live')
        path = m.root/t['id']/'protected.wav'
        path.write_bytes(b'capture')
        for seconds in (1, float('nan'), 'bad'):
            with pytest.raises(PhoneControlError):
                await m.read(e, seconds=seconds)
            assert m.current(e) is t and path.read_bytes() == b'capture'
        await m.close()
    asyncio.run(run())


def test_video_model_receives_original_audio_even_when_asr_is_configured(tmp_path, monkeypatch):
    async def run():
        s = service(tmp_path)
        s.config = PhoneConfig.from_mapping({'model': {'asr_provider_id': 'unused-asr', 'video_provider_id': 'video'}})
        s.app_context = SimpleNamespace(get_provider_by_id=lambda id: SimpleNamespace() if id=='video' else None)
        s.record_internal_usage = lambda *a, **k: None
        manager = ContentTasks(s)
        t = await manager.begin(SimpleNamespace(unified_msg_origin='owner'), kind='post', content_id='fixture')
        t['video_provider_id']='video'
        audio=tmp_path/'original.wav';audio.write_bytes(b'original-audio')
        calls=[]
        async def encode(frames,start,end,source,out):
            calls.append(('encode',source.read_bytes()))
            out.write_bytes(b'video-with-original-audio')
            return out
        async def generate(**kwargs):
            calls.append(('model', kwargs['chat_provider_id']))
            assert kwargs['video_path'].read_bytes() == b'video-with-original-audio'
            assert '语音转写' not in kwargs['prompt']
            return SimpleNamespace(completion_text='画面与声音',usage=None)
        monkeypatch.setattr('core.media_video.encode_segment',encode)
        manager.providers.generate=generate
        result=await manager.understand_video(None,t,0,3,audio)
        assert calls == [('encode',b'original-audio'),('model','video')]
        assert '画面与声音' in result
        assert not (manager.root/t['id']/'video-0-3.mp4').exists()
        await manager.close()
    asyncio.run(run())
