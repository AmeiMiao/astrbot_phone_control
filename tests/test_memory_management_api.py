"""真实 AstrBot 的日志删除和索引变更端点。"""

import asyncio
from types import SimpleNamespace

import pytest

astrbot = pytest.importorskip("astrbot")
if getattr(astrbot, "_acat_local_stub", False):
    pytest.skip("需要真实 AstrBot Web API", allow_module_level=True)

from test_main_fake_enabled import _make_plugin, _json, plugin_main


def test_diary_delete_updates_source_and_index_rules(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        diary = plugin.service.diary
        diary._write_entries_unlocked([{"id": "uat-log", "summary": "待删除"}])

        class Body:
            async def json(self):
                return {"id": "uat-log"}

        monkeypatch.setattr(plugin_main, "request", Body())
        try:
            response = await plugin.dashboard_diary_delete()
            assert _json(response) == {"deleted": "uat-log"}
            assert list(diary.all_entries()) == []
            assert plugin.semantic_index.deletions.read()["sources"] == [
                ["diary", "uat-log"]
            ]
            assert (await plugin.dashboard_diary_delete()).status_code == 400
        finally:
            await plugin.terminate()

    asyncio.run(scenario())
