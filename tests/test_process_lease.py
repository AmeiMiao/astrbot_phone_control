"""用真实进程验证心跳失联、进程组回收与资源互斥。"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from core.process_lease import ROOT


def wait_for(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("等待状态变化超时")


def spawn(tmp_path, name, rev="first"):
    pidfile = tmp_path / name
    script = ("import subprocess,time; from pathlib import Path; "
              "p=subprocess.Popen(['sleep','60']); "
              f"Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "bin/phone_process_guard.py"),
         "--resource", str(tmp_path / "owned.lock"), "--revision", rev,
         "--ttl", "0.5", "--", sys.executable, "-c", script],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return proc, pidfile


@pytest.mark.parametrize("disconnect", [False, True])
def test_lost_plugin_reaps_process_group(tmp_path, disconnect):
    proc, pidfile = spawn(tmp_path, "child.pid")
    try:
        wait_for(pidfile.exists)
        child = int(pidfile.read_text())
        proc.stdin.write(b"heartbeat\n")
        proc.stdin.flush()
        if disconnect:
            proc.stdin.close()
        proc.wait(timeout=5)
        # 监护器负责回收管道孙进程，退出后连 defunct 记录也应消失。
        state = Path(f"/proc/{child}/stat")
        wait_for(lambda: not state.exists())
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_updated_code_waits_for_predecessor_before_start(tmp_path):
    old, first = spawn(tmp_path, "first.pid", "old-code")
    new = None
    try:
        wait_for(first.exists)
        new, second = spawn(tmp_path, "second.pid", "new-code")
        time.sleep(0.1)
        assert not second.exists()
        old.stdin.close()
        old.wait(timeout=5)
        wait_for(second.exists)
        manifest = json.loads((tmp_path / "owned.lock").read_text())
        assert manifest["revision"] == "new-code"
        new.stdin.close()
        new.wait(timeout=5)
    finally:
        for process in (old, new):
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_android_launch_embeds_watchdog_before_return():
    from core.process_lease import DeviceLease
    lease = DeviceLease(None, "scrcpy")
    command = lease.wrap_launch("app_process / Example < /dev/null > /dev/null 2>&1 & echo PID=$!")
    assert "GUARD_PID=$!" in command
    assert "/proc/$1/stat" in command
    assert "date +%s" in command
    assert lease.path in command
    assert command.index("P=$!") < command.index("GUARD_PID=$!")


def test_reclaim_untended_process_uses_exact_data_directory(tmp_path):
    from core.process_lease import reclaim_legacy_processes
    helper = tmp_path / "mitmdump"
    helper.write_text("import time; time.sleep(60)\n")
    marker = tmp_path / "owned"
    owned = subprocess.Popen([sys.executable, str(helper), f"confdir={marker}"])
    other = subprocess.Popen([sys.executable, str(helper), f"confdir={marker}-other"])
    try:
        assert asyncio.run(reclaim_legacy_processes([marker])) == 1
        owned.wait(timeout=3)
        assert other.poll() is None
    finally:
        for proc in (owned, other):
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=3)


def test_device_heartbeat_retries_transient_failure_without_extending_expiry(monkeypatch):
    import asyncio
    import core.process_lease as leases
    async def run():
        calls=[];recovered=asyncio.Event()
        class Adb:
            async def shell(self,command,**kwargs):
                calls.append(command)
                if 'case "$E"' in command:
                    if len([x for x in calls if 'case "$E"' in x])==1:
                        raise TimeoutError('临时ADB延迟')
                    recovered.set()
                return ''
        sleep=asyncio.sleep
        async def quick_sleep(seconds):await sleep(.001)
        monkeypatch.setattr(leases.asyncio,'sleep',quick_sleep)
        lease=leases.DeviceLease(Adb(),'audio');lease.observe('GUARD_PID=123')
        await asyncio.wait_for(recovered.wait(),1)
        await leases.stop_heartbeat(lease.task)
        beats=[x for x in calls if 'case "$E"' in x]
        assert len(beats)>=2
        assert all('[ "$E" -gt "$N" ] || exit 75' in x for x in beats)
        assert leases.TTL==15
    asyncio.run(run())
