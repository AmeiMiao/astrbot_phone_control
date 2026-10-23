"""真实 AstrBot 结果钩子向主人路由，失败通知不改变工具结果。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

astrbot = pytest.importorskip("astrbot")
if getattr(astrbot, "_acat_local_stub", False):
    pytest.skip("需要真实 AstrBot", allow_module_level=True)
from test_main_fake_enabled import _make_plugin


class Event:
    unified_msg_origin = "cron:OtherMessage:uat-job"

    def __init__(self):
        self.extra = {}

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_platform_name(self):
        return "cron"


def test_autonomous_tool_failure_is_sent_to_owner_private_session(
    tmp_path, monkeypatch
):
    async def run():
        p = _make_plugin(tmp_path, monkeypatch)
        p.raw_config.setdefault("security", {})["owner_ids"] = ["uat-owner"]
        p.service.remember_session("onebot:GroupMessage:uat-group")
        p.context.send_message = AsyncMock(return_value=True)
        result = "error: Error code: 500 - NoneType object has no attribute get"
        try:
            await p.track_phone_tool_result(
                Event(), SimpleNamespace(name="phone_watch_media"), {}, result
            )
            await p._tool_reports.task
            call = p.context.send_message.await_args
            assert str(call.args[0]) == "onebot:FriendMessage:uat-owner"
            text = str(call.args[1])
            assert "phone_watch_media" in text and "500" in text
            assert (
                result
                == "error: Error code: 500 - NoneType object has no attribute get"
            )
            assert p._tool_reports.status()["sent"] == 1
        finally:
            await p.terminate()

    asyncio.run(run())


def test_owner_change_prevents_delivery_to_previous_owner(tmp_path, monkeypatch):
    async def run():
        p = _make_plugin(tmp_path, monkeypatch)
        p.raw_config.setdefault("security", {})["owner_ids"] = ["current-owner"]
        p.context.send_message = AsyncMock(return_value=True)
        try:
            with pytest.raises(PermissionError):
                await p._send_tool_error_report(
                    "onebot:FriendMessage:old-owner", "test"
                )
            p.context.send_message.assert_not_awaited()
        finally:
            await p.terminate()

    asyncio.run(run())
