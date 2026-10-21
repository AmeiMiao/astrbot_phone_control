"""后台清理保护常驻网络应用，并覆盖手工、前台和抓包入口。"""
import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.background import clean_background
from core.config import PhoneConfig
from core.errors import ActionFailedError, AdbCommandError
from core.traffic_capture import TrafficCapture
from core.service import PhoneService
from types import SimpleNamespace


class Adb:
    def __init__(self): self.commands=[]
    async def shell(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith('cmd package'):return 'com.example.launcher/.Home'
        if command.startswith('settings get'):return 'com.android.adbkeyboard/.Input'
        if command=='pm list packages -3':
            return '\n'.join('package:'+p for p in ('com.wireguard.android','com.tailscale.ipn','com.example.app','com.example.keep','com.example.launcher','com.android.adbkeyboard'))
        if command=='dumpsys activity recents':return 'realActivity=com.android.settings/.Settings'
        return ''


def test_cleaner_preserves_default_vpn_and_custom_keep_foreground():
    async def run():
        adb=Adb()
        config=PhoneConfig.from_mapping({'security':{'background_keep_packages':['com.wireguard.android','com.tailscale.ipn','com.example.keep']}})
        report=await clean_background(adb,whitelist=config.security.background_keep_packages,foreground='com.example.app')
        stopped=[c for c in adb.commands if c.startswith('am force-stop')]
        assert stopped==['am force-stop --user current com.android.settings']
        assert report['stopped']==1 and not report['errors']
        assert 'com.wireguard.android' in report['preserved']
        assert 'am kill-all' not in adb.commands
    asyncio.run(run())


def test_explicit_clean_cannot_stop_preserved_package():
    async def run():
        adb=Adb()
        with pytest.raises(ActionFailedError,match='保留'):
            await clean_background(adb,package='com.wireguard.android')
        assert not any(c.startswith('am force-stop') for c in adb.commands)
    asyncio.run(run())


def test_cleanup_failure_does_not_hide_other_package_results():
    async def run():
        adb=Adb(); original=adb.shell
        async def shell(command,**kwargs):
            if command=='am force-stop --user current com.example.app':raise AdbCommandError('denied')
            return await original(command,**kwargs)
        adb.shell=shell
        report=await clean_background(adb)
        assert report['stopped']==2
        assert any('com.example.app' in e for e in report['errors'])
    asyncio.run(run())


def test_capture_initialization_uses_live_keep_provider(tmp_path):
    async def run():
        adb=Adb(); keep=['com.wireguard.android','com.tailscale.ipn','com.example.keep']
        capture=TrafficCapture(adb=adb,data_dir=tmp_path,keep_packages_provider=lambda:keep)
        assert await capture._force_stop_apps()==2
        assert not any(c.startswith('am force-stop') and ('wireguard' in c or 'com.example.keep' in c) for c in adb.commands)
    asyncio.run(run())


def test_keep_defaults_and_explicit_empty_list():
    assert 'com.wireguard.android' in PhoneConfig.from_mapping({}).security.background_keep_packages
    assert PhoneConfig.from_mapping({'security':{'background_keep_packages':[]}}).security.background_keep_packages==()


def test_manual_cleanup_rechecks_gate_between_packages():
    async def run():
        adb=Adb()
        async def foreground():return 'com.example.current','Activity'
        def guard(op):
            if any(c.startswith('am force-stop') for c in adb.commands):
                raise ActionFailedError('new task owns device')
        service=SimpleNamespace(adb=adb,config=PhoneConfig.from_mapping({}),guard_device=guard,actions=SimpleNamespace(foreground=foreground))
        with pytest.raises(ActionFailedError,match='new task'):
            await PhoneService.clean_background(service)
        assert len([c for c in adb.commands if c.startswith('am force-stop')])==1
    asyncio.run(run())
