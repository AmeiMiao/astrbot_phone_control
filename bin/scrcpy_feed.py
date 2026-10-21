#!/usr/bin/env python3
"""scrcpy 解码管道助手：把指定文件描述符中的字节流转发到 stdout。

插件在解码时使用 shell 管道形式：::

    python3 bin/scrcpy_feed.py <fd> | ffmpeg ... -f image2pipe -vcodec mjpeg pipe:1 |
        python3 bin/scrcpy_frame_writer.py <data_dir>

``<fd>`` 是插件进程通过 ``subprocess.Popen(pass_fds=...)`` 传入的 video
socket 文件描述符。本脚本只做阻塞读 + 写出，不做任何协议解析，避免 Python
与 ffmpeg 之间的直接 Popen 管道路径在容器环境中的兼容性问题。
"""

from __future__ import annotations

import os
import select
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: scrcpy_feed.py <fd>", file=sys.stderr)
        return 2
    try:
        fd = int(argv[1])
    except ValueError:
        print(f"invalid fd: {argv[1]!r}", file=sys.stderr)
        return 2

    out = sys.stdout.buffer
    try:
        while True:
            try:
                if not select.select([fd], [], [], 1)[0]:
                    continue
                chunk = os.read(fd, 65536)
            except (InterruptedError, BlockingIOError):
                continue
            except OSError:
                break
            if not chunk:
                break
            try:
                out.write(chunk)
                out.flush()
            except (BrokenPipeError, OSError):
                return 0
    except KeyboardInterrupt:  # pragma: no cover - 交互式中止
        return 0
    finally:
        try:
            out.flush()
        except (BrokenPipeError, OSError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
