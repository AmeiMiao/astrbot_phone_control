"""独立子进程监护器：插件通过 stdin 心跳保活，失联回收整个子进程组。"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--ttl", type=float, default=15)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("缺少子进程命令")
    # 收养管道孙进程，退出时一并 wait，防止它们被留给容器 PID 1。
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "无法启用子进程回收")
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin, selectors.EVENT_READ)
    deadline = time.monotonic() + args.ttl
    active = True

    def stop(*_args):
        nonlocal active
        active = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def heartbeat() -> bool:
        nonlocal deadline
        if selector.select(0.1):
            if not os.read(sys.stdin.fileno(), 4096):
                return False
            deadline = time.monotonic() + args.ttl
        return active and time.monotonic() < deadline

    resource = Path(args.resource)
    resource.parent.mkdir(parents=True, exist_ok=True)
    # 文件锁由监护器持有；候选进程只能等前任完成退出和资源回收后启动。
    with resource.open("a+") as lock:
        wait_until = time.monotonic() + args.ttl + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= wait_until or not heartbeat():
                    return 75
        lock.seek(0)
        previous = lock.read()
        if previous:
            print("phone guard: 检测到前任租约，已等待其释放资源", file=sys.stderr)
        lock.seek(0)
        lock.truncate()
        json.dump({"pid": os.getpid(), "revision": args.revision}, lock)
        lock.flush()
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True,
                                 close_fds=False)
        try:
            while child.poll() is None and heartbeat():
                pass
        finally:
            # 子进程本身退出后，其管道孙进程仍可能存活，始终回收进程组。
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            while True:
                try:
                    os.waitpid(-1, 0)
                except ChildProcessError:
                    break
        return child.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
