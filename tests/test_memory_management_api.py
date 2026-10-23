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


def test_vector_delete_validates_kind_and_preserves_source(tmp_path, monkeypatch):
    from core.semantic import SemanticDoc, build_index
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        values = {"id": "learning:a", "kind": "chat"}
        class Body:
            async def json(self):
                return values
        monkeypatch.setattr(plugin_main, "request", Body())
        try:
            await build_index(plugin.semantic_index, [SemanticDoc("learning:a", "learning", "a", "一", "一", 1)], embed=lambda texts: [[1, 0]], provider_id="p", model="m", dim=2)
            result = _json(await plugin.dashboard_vector_delete())
            assert result == {"deleted": "learning:a", "source_preserved": True}
            assert plugin.semantic_index.load_snapshot().count == 0
            assert (await plugin.dashboard_vector_delete()).status_code == 400
            values["kind"] = "../other"
            assert (await plugin.dashboard_vector_delete()).status_code == 400
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


def test_graph_api_filters_and_rejects_invalid_parameters(tmp_path, monkeypatch):
    from core.semantic import SemanticDoc, build_index
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        query = {"kind": "chat", "min_score": "0.7"}
        monkeypatch.setattr(plugin_main, "request", SimpleNamespace(query=query))
        try:
            await build_index(plugin.semantic_index, [SemanticDoc("a", "learning", "a", "一", "一", 1), SemanticDoc("b", "diary", "b", "二", "二", 2)], embed=lambda texts: [[1, 0], [.8, .6]], provider_id="p", model="m", dim=2)
            graph = _json(await plugin.dashboard_vector_graph())
            assert len(graph["nodes"]) == 2 and len(graph["edges"]) == 1
            plugin.semantic_index.delete_document("a")
            graph = _json(await plugin.dashboard_vector_graph())
            assert len(graph["nodes"]) == 1 and not graph["edges"]
            for value in ["nan", "-1", "bad"]:
                query["min_score"] = value
                assert (await plugin.dashboard_vector_graph()).status_code == 400
        finally:
            await plugin.terminate()
    asyncio.run(scenario())
