"""将连续屏幕采样与同一时间窗音频编码为有界 MP4 片段。"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path
import shutil

from .errors import PhoneControlError

VIDEO_INTERVAL = 0.5
MAX_VIDEO_SECONDS = 60
MAX_VIDEO_BYTES = 6 * 1024 * 1024
MAX_BUFFER_BYTES = 128 * 1024 * 1024
MAX_CAPTURE_SECONDS = 900


def compact_frame(data: bytes) -> bytes:
    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        image.thumbnail((480, 480))
        out = BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=65)
        return out.getvalue()


def video_part(path: Path) -> dict:
    """编码前后均限制大小，保持 Base64 视频内容小于 8 MiB。"""
    with path.open("rb") as handle:
        data = handle.read(MAX_VIDEO_BYTES + 1)
    if not data or len(data) > MAX_VIDEO_BYTES:
        raise PhoneControlError("视频片段为空或超过 6 MiB，请缩短单段观看时长")
    return {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(data).decode("ascii")}}


async def encode_segment(frames, start: float, end: float, audio: Path | None, output: Path) -> Path:
    """按实际采样时间保留帧间停留，音频取相同游标范围。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PhoneControlError("视频回传需要宿主安装 FFmpeg 并加入 PATH")
    duration = end - start
    if not 0 < duration <= MAX_VIDEO_SECONDS:
        raise PhoneControlError("视频回传单段时长需大于 0 且不超过 60 秒")
    chosen = [(float(t), Path(p)) for t, p in frames if t < end]
    previous = [(t, p) for t, p in chosen if t <= start]
    chosen = (previous[-1:] + [(t, p) for t, p in chosen if t > start])
    if not chosen:
        raise PhoneControlError("本段没有可用的视频画面")
    manifest = output.with_suffix(".ffconcat")
    lines = ["ffconcat version 1.0"]
    for index, (timestamp, path) in enumerate(chosen):
        next_at = chosen[index + 1][0] if index + 1 < len(chosen) else end
        at = start if index == 0 else max(start, timestamp)
        name = str(path.resolve()).replace("'", "'\\''")
        lines.extend([f"file '{name}'", f"duration {max(0.001, next_at-at):.6f}"])
    lines.append(lines[-2])
    manifest.write_text("\n".join(lines) + "\n")
    args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(manifest)]
    if audio is not None:
        args += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "48k"]
    else:
        args += ["-an"]
    args += ["-vf", "scale=480:480:force_original_aspect_ratio=decrease,pad=480:480:(ow-iw)/2:(oh-ih)/2", "-r", "2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p", "-threads", "1", "-t", f"{duration:.6f}", "-movflags", "+faststart", "-fs", str(MAX_VIDEO_BYTES + 65536), str(output)]
    process = None
    succeeded = False
    try:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        if process.returncode:
            raise PhoneControlError("视频片段编码失败", detail=stderr.decode("utf-8", "replace")[-500:])
        if not output.is_file() or not 0 < output.stat().st_size <= MAX_VIDEO_BYTES:
            raise PhoneControlError("视频片段超过 6 MiB，请缩短单段观看时长")
        succeeded = True
        return output
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if not succeeded:
            output.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
