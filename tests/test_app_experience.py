"""经验后端二选一及知识库失败语义。"""

import asyncio
from types import SimpleNamespace

import pytest

from core.app_experience import AppExperience
from core.app_memory import AppMemoryError, AppMemoryStore
from core.config import PhoneConfig


class KnowledgeBase:
    init_error = ""

    def __init__(self):
        self.docs = {}
        self.counter = 0
        self.fail_upload = False

    async def list_documents(self, offset=0, limit=100, search=None):
        return [d for d in self.docs.values() if not search or search in d.doc_name][offset:offset + limit]

    async def get_chunks_by_doc_id(self, doc_id, **_kwargs):
        return [{"content": self.docs[doc_id].text}]

    async def upload_document(self, file_name, pre_chunked_text, **_kwargs):
        if self.fail_upload:
            raise RuntimeError("Embedding 服务不可用")
        self.counter += 1
        doc = SimpleNamespace(doc_id=str(self.counter), doc_name=file_name, text=pre_chunked_text[0])
        self.docs[doc.doc_id] = doc
        return doc

    async def delete_document(self, doc_id):
        del self.docs[doc_id]


def setup(tmp_path, backend="knowledge_base", enabled=True):
    helper = KnowledgeBase()
    class Manager:
        async def get_kb_by_name(self, name):
            assert name == "手机经验"
            return helper
    service = SimpleNamespace(
        config=PhoneConfig.from_mapping({"prompt": {"app_memory_enabled": enabled,
            "app_memory_backend": backend, "app_memory_kb_name": "手机经验"}}),
        app_memory=AppMemoryStore(tmp_path), app_context=SimpleNamespace(kb_manager=Manager()))
    return AppExperience(service), service, helper


def test_knowledge_base_is_the_only_read_write_destination(tmp_path):
    async def run():
        repo, service, helper = setup(tmp_path)
        service.app_memory.remember(app="B站", note="插件中的经验")
        first = await repo.remember(app="B站", page="首页", note="知识库经验")
        assert first.version == 1
        second = await repo.remember(app="哔哩哔哩", page="首页", note="更新经验")
        assert second.version == 2
        assert len(helper.docs) == 1
        found = await repo.lookup("B站")
        assert [e.note for e in found] == ["更新经验"]
        assert service.app_memory.lookup("B站")[0].note == "插件中的经验"
        assert "更新经验" in await repo.render_app("B站")
    asyncio.run(run())


def test_failed_kb_write_preserves_previous_and_never_writes_plugin(tmp_path):
    async def run():
        repo, service, helper = setup(tmp_path)
        await repo.remember(app="B站", note="有效经验")
        helper.fail_upload = True
        with pytest.raises(RuntimeError, match="Embedding"):
            await repo.remember(app="B站", note="写入失败")
        assert [e.note for e in await repo.lookup("B站")] == ["有效经验"]
        assert service.app_memory.entries() == []
    asyncio.run(run())


def test_plugin_backend_uses_local_store_only(tmp_path):
    async def run():
        repo, service, helper = setup(tmp_path, backend="plugin")
        await repo.remember(app="B站", note="本地经验")
        assert [e.note for e in await repo.lookup("B站")] == ["本地经验"]
        assert not helper.docs
    asyncio.run(run())


def test_disabled_memory_blocks_record_and_query(tmp_path):
    async def run():
        repo, service, helper = setup(tmp_path, enabled=False)
        with pytest.raises(AppMemoryError, match="未开启"):
            await repo.remember(app="B站", note="经验")
        with pytest.raises(AppMemoryError, match="未开启"):
            await repo.lookup("B站")
        assert service.app_memory.entries() == []
        assert not helper.docs
    asyncio.run(run())
