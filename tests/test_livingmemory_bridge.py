"""LivingMemory 热重载与规则拒绝下的实际写入门禁。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

astrbot = pytest.importorskip("astrbot")
if getattr(astrbot, "_acat_local_stub", False):
    pytest.skip("需要真实 AstrBot 插件装配", allow_module_level=True)

from test_main_fake_enabled import _make_plugin


def test_livingmemory_lookup_follows_reload_and_activation(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        metadata = SimpleNamespace(star_cls=object(), activated=True)
        plugin.context.get_registered_star = lambda name: metadata
        try:
            assert plugin._get_livingmemory_plugin() is metadata.star_cls
            metadata.star_cls = object()
            assert plugin._get_livingmemory_plugin() is metadata.star_cls
            metadata.activated = False
            assert plugin._get_livingmemory_plugin() is None
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


def test_livingmemory_scope_failure_does_not_write(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        engine = SimpleNamespace(add_memory=AsyncMock())
        lm = SimpleNamespace(initializer=SimpleNamespace(memory_engine=engine))
        monkeypatch.setattr(plugin, "_get_livingmemory_plugin", lambda: lm)
        try:
            ok, reason = await plugin._sync_memory_to_livingmemory(SimpleNamespace(), {"content": "test"})
            assert not ok and "作用域配置未就绪" in reason
            engine.add_memory.assert_not_awaited()
        finally:
            await plugin.terminate()
    asyncio.run(scenario())
