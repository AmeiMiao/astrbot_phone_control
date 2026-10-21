#!/usr/bin/env python3
"""把 ffmpeg image2pipe 输出的 MJPEG 流拆成最新一帧 JPEG。

解码管道：``python scrcpy_feed.py <video_fd> | ffmpeg ... -f image2pipe -vcodec mjpeg pipe:1 | python scrcpy_frame_writer.py <dir>``

image2 muxer 的 ``-update 1`` 在部分 ffmpeg 版本/输入下会把首帧缓冲到流结束才落盘，
导致实时监控拿不到画面；image2pipe 输出到本脚本后逐帧原子替换 latest 文件可稳定实时。
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: scrcpy_frame_writer.py <output_dir>", file=sys.stderr)
        return 2
    out_dir = sys.argv[1]
    out_path = os.path.join(out_dir, "scrcpy_live.jpg")
    tmp_path = os.path.join(out_dir, ".scrcpy_live.jpg.tmp")
    os.makedirs(out_dir, exist_ok=True)
    if not hasattr(sys.stdin.buffer, "read"):
        return 2
    buf = b""
    while True:
        try:
            chunk = sys.stdin.buffer.read1(65536)
        except (KeyboardInterrupt, OSError):
            break
        if not chunk:
            break
        buf += chunk
        while True:
            start = buf.find(b"\xff\xd8")
            if start < 0:
                if len(buf) > 4 * 1024 * 1024:
                    buf = buf[-2:]
                break
            end = buf.find(b"\xff\xd9", start + 2)
            if end < 0:
                if start > 0:
                    buf = buf[start:]
                break
            frame = buf[start : end + 2]
            buf = buf[end + 2 :]
            try:
                with open(tmp_path, "wb") as handle:
                    handle.write(frame)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, out_path)
            except OSError:
                break
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
