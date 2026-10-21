"""短 JPEG 在输入仍开启时也必须立即输出，避免等待大缓冲区填满。"""

from pathlib import Path
import subprocess
import sys
import time


def test_small_frame_is_written_before_input_eof(tmp_path):
    script = Path(__file__).resolve().parents[1] / "bin/scrcpy_frame_writer.py"
    process = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path)], stdin=subprocess.PIPE
    )
    frame = b"\xff\xd8small-frame\xff\xd9"
    try:
        process.stdin.write(frame)
        process.stdin.flush()
        deadline = time.monotonic() + 3
        output = tmp_path / "scrcpy_live.jpg"
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert output.read_bytes() == frame
        assert process.poll() is None
    finally:
        process.stdin.close()
        process.wait(timeout=3)


def test_nonblocking_video_socket_waits_between_packets(tmp_path):
    import os
    import select
    import socket
    left,right=socket.socketpair();left.setblocking(False)
    script=Path(__file__).resolve().parents[1]/'bin/scrcpy_feed.py'
    process=subprocess.Popen([sys.executable,str(script),str(left.fileno())],pass_fds=(left.fileno(),),stdout=subprocess.PIPE)
    try:
        right.sendall(b'first')
        assert select.select([process.stdout],[],[],3)[0]
        assert os.read(process.stdout.fileno(),5)==b'first'
        time.sleep(.1)
        assert process.poll() is None
        right.sendall(b'second')
        assert select.select([process.stdout],[],[],3)[0]
        assert os.read(process.stdout.fileno(),6)==b'second'
        right.close();process.wait(timeout=3);assert process.returncode==0
    finally:
        left.close();right.close()
        if process.poll() is None:process.terminate();process.wait(timeout=3)
        process.stdout.close()
