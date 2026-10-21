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
