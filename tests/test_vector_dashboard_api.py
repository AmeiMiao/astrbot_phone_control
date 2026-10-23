"""真实 AstrBot 的向量查看 API 注册、索引选择和参数验证。"""

import asyncio
from types import SimpleNamespace

import pytest

astrbot = pytest.importorskip("astrbot")
if getattr(astrbot, "_acat_local_stub", False):
    pytest.skip("需要真实 AstrBot Web API", allow_module_level=True)

from test_main_fake_enabled import _make_plugin, _json, plugin_main


def test_vector_dashboard_api(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            assert any(route.endswith("/dashboard/vectors") for route, _ in plugin.context.web_routes)
            for kind in ("app", "chat"):
                monkeypatch.setattr(plugin_main, "request", SimpleNamespace(query={"kind": kind}))
                data = _json(await plugin.dashboard_vectors())
                assert data["kind"] == kind and data["state"] == "missing"
                assert data["entries"] == []
            for query in ({"kind": "../other"}, {"offset": "bad"}):
                monkeypatch.setattr(plugin_main, "request", SimpleNamespace(query=query))
                response = await plugin.dashboard_vectors()
                assert response.status_code == 400
        finally:
            await plugin.terminate()
    asyncio.run(scenario())
