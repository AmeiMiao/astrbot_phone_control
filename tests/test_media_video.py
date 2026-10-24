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
