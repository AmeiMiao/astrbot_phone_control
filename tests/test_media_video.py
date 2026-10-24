"""视频文件在编码与发送边界限制体积，避免无界载入请求内存。"""
import asyncio
from pathlib import Path
import pytest
from core.media_video import MAX_VIDEO_BYTES, video_part, encode_segment
from core.errors import PhoneControlError


def test_video_rejects_oversized_sparse_file_before_base64(tmp_path):
    path = tmp_path/'large.mp4'
    with path.open('wb') as f:
        f.truncate(MAX_VIDEO_BYTES + 1)
    with pytest.raises(PhoneControlError, match='超过'):
        video_part(path)
    path.write_bytes(b'')
    with pytest.raises(PhoneControlError, match='为空'):
        video_part(path)


def test_missing_ffmpeg_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr('core.media_video.shutil.which', lambda _: None)
    with pytest.raises(PhoneControlError, match='FFmpeg'):
        asyncio.run(encode_segment([], 0, 3, None, tmp_path/'out.mp4'))


def test_video_duration_rejects_whole_large_input(tmp_path, monkeypatch):
    monkeypatch.setattr('core.media_video.shutil.which', lambda _: '/test/ffmpeg')
    with pytest.raises(PhoneControlError, match='60 秒'):
        asyncio.run(encode_segment([], 0, 120, None, tmp_path/'out.mp4'))


def test_mp4_round_trip_preserves_segment_color_time_and_silent_track(tmp_path):
    import json
    import shutil
    import subprocess
    from PIL import Image
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('真实编码回归需要 FFmpeg 与 ffprobe；在 AstrBot 容器执行')
    frames=[]
    for index in range(12):
        path=tmp_path/f'{index}.jpg'
        Image.new('RGB',(160,120),'red' if index<6 else 'blue').save(path)
        frames.append([index/2,str(path)])
    for start in (0,3):
        out=asyncio.run(encode_segment(frames,start,start+3,None,tmp_path/f'clip-{start}.mp4'))
        meta=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(out)]))
        assert {s['codec_type'] for s in meta['streams']} == {'video','audio'}
        assert 2.9 <= float(meta['format']['duration']) <= 3.1
        rgb=subprocess.check_output(['ffmpeg','-v','error','-i',str(out),'-frames:v','1','-vf','scale=1:1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
        assert rgb[0] > rgb[2] if start==0 else rgb[2] > rgb[0]
        pcm=subprocess.check_output(['ffmpeg','-v','error','-i',str(out),'-vn','-ac','1','-ar','16000','-f','s16le','pipe:1'])
        assert pcm and set(pcm) == {0}
        assert len(video_part(out)['video_url']['url']) < 8*1024*1024
        assert not out.with_suffix('.ffconcat').exists()
