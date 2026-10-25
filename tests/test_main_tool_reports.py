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


@pytest.mark.parametrize("tool_name", ["phone_tap", "phone_long_press"])
@pytest.mark.parametrize("failure", ["recalled_target", "unknown_target", "ui_dump"])
def test_target_mismatch_maintains_experience_without_owner_notification(
    tmp_path, monkeypatch, tool_name, failure
):
    from core.app_experience import AppExperience
    from core.app_memory import AppMemoryStore
    from core.errors import UiDumpError
    from core.tool_reports import experience_mismatch, result_failed, result_text
    from core.tools.device import PhoneLongPressTool, PhoneTapTool
    from test_tools_gate import FakeService, FakeToolContext

    async def run():
        plugin = _make_plugin(tmp_path / "plugin", monkeypatch)
        plugin.raw_config.setdefault("security", {})["owner_ids"] = ["uat-owner"]
        plugin.service.remember_session("onebot:GroupMessage:uat-group")
        plugin.context.send_message = AsyncMock(return_value=True)
        plugin._tool_reports.batch_delay = 0
        service = FakeService()
        service.app_memory = AppMemoryStore(tmp_path / "experience")
        service.app_memory.remember(app="com.example.app", page="首页", note="点击评论")
        service.experiences = AppExperience(service)
        service.tap = AsyncMock()
        service.long_press = AsyncMock()
        event = Event()
        if failure != "unknown_target":
            event.set_extra("phone_app_recall_state", {
                "pages": [], "entries": [{"app": "com.example.app", "page": "首页"}],
            })

        async def find_node(**_kwargs):
            if failure == "ui_dump":
                raise UiDumpError("当前节点采集失败")
            return None

        service.find_node = find_node
        tool_type = PhoneTapTool if tool_name == "phone_tap" else PhoneLongPressTool
        tool = tool_type(service=service)
        try:
            result = await tool.call(FakeToolContext(event), text="评论")
            assert result_failed(result)
            assert experience_mismatch(result) == (failure == "recalled_target")
            service.tap.assert_not_awaited()
            service.long_press.assert_not_awaited()
            await plugin.track_phone_tool_result(event, tool, {"text": "评论"}, result)
            if failure == "recalled_target":
                assert "重新探索" in result_text(result)
                assert plugin._tool_reports.task is None
                plugin.context.send_message.assert_not_awaited()
                assert service.app_memory.entries()[0].failures == 1
                # MCP JSON 序列化后仍保留内部分类。
                assert experience_mismatch(result.model_dump(by_alias=True))
            else:
                await plugin._tool_reports.task
                plugin.context.send_message.assert_awaited_once()
                assert service.app_memory.entries()[0].failures == 0
            assert not event.get_extra("phone_control_used")
        finally:
            await plugin.terminate()

    asyncio.run(run())


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
