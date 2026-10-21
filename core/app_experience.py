"""App 经验的唯一存储入口：插件档案或 AstrBot 知识库。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import time

from .app_memory import (
    AppMemoryEntry, AppMemoryError, _entry_from_dict, _normalize_app,
    _normalize_note, _normalize_page, _normalize_steps,
)


class AppExperience:
    def __init__(self, service):
        self.service = service
        self.lock = asyncio.Lock()

    @property
    def settings(self):
        return self.service.config.prompt

    def require_enabled(self):
        if not self.settings.app_memory_enabled:
            raise AppMemoryError("App 使用经验记录未开启")

    async def helper(self):
        self.require_enabled()
        name = self.settings.app_memory_kb_name.strip()
        if not name:
            raise AppMemoryError("请在配置中选择 App 经验使用的 AstrBot 知识库")
        manager = getattr(self.service.app_context, "kb_manager", None)
        if manager is None:
            raise AppMemoryError("AstrBot 知识库管理器不可用")
        helper = await manager.get_kb_by_name(name)
        if helper is None:
            raise AppMemoryError(f"AstrBot 知识库不存在：{name}")
        if getattr(helper, "init_error", ""):
            raise AppMemoryError(f"AstrBot 知识库未就绪：{helper.init_error}")
        return helper

    @staticmethod
    def prefix(app=None):
        return "phone-app-" + (hashlib.sha256(app.encode()).hexdigest()[:20] + "-" if app else "")

    async def documents(self, helper, app=None):
        prefix = self.prefix(app)
        result = []
        offset = 0
        while True:
            docs = await helper.list_documents(offset=offset, limit=100, search=prefix)
            result.extend(doc for doc in docs if doc.doc_name.startswith(prefix))
            if len(docs) < 100:
                return result
            offset += len(docs)

    async def read_entry(self, helper, doc):
        chunks = await helper.get_chunks_by_doc_id(doc.doc_id, offset=0, limit=2)
        if len(chunks) != 1:
            raise AppMemoryError(f"App 经验文档结构异常：{doc.doc_name}")
        entry = _entry_from_dict(json.loads(chunks[0]["content"]))
        if entry is None:
            raise AppMemoryError(f"App 经验文档内容异常：{doc.doc_name}")
        return entry

    async def remember(self, **kwargs):
        self.require_enabled()
        if self.settings.app_memory_backend == "plugin":
            return self.service.app_memory.remember(**kwargs)
        resolved = _normalize_app(kwargs.get("app"))
        if resolved is None:
            raise AppMemoryError("App 名称不能为空")
        app, label = resolved
        page = _normalize_page(kwargs.get("page"))
        entry = AppMemoryEntry(app=app, label=label, page=page,
                               note=_normalize_note(kwargs.get("note")),
                               steps=_normalize_steps(kwargs.get("steps")), updated_at=time.time())
        name = self.prefix(app) + hashlib.sha256(page.encode()).hexdigest()[:20] + ".json"
        async with self.lock:
            helper = await self.helper()
            old = [doc for doc in await self.documents(helper, app) if doc.doc_name == name]
            entries = [await self.read_entry(helper, doc) for doc in old]
            entry = replace(entry, version=max((e.version for e in entries), default=0) + 1)
            content = json.dumps(entry.as_dict(), ensure_ascii=False)
            await helper.upload_document(file_name=name, file_content=None,
                                         file_type="json", pre_chunked_text=[content])
            for doc in old:
                await helper.delete_document(doc.doc_id)
            return entry

    async def lookup(self, app, *, page="", limit=5):
        self.require_enabled()
        if self.settings.app_memory_backend == "plugin":
            return self.service.app_memory.lookup(app, page=page, limit=limit)
        resolved = _normalize_app(app)
        if resolved is None:
            raise AppMemoryError("App 名称不能为空")
        async with self.lock:
            helper = await self.helper()
            entries = [await self.read_entry(helper, doc) for doc in await self.documents(helper, resolved[0])]
        latest = {}
        for entry in sorted(entries, key=lambda e: (e.version, e.updated_at), reverse=True):
            if entry.app == resolved[0] and (not page or page in entry.page):
                latest.setdefault(entry.page, entry)
        return list(latest.values())[:limit]

    @staticmethod
    def render(entries, max_chars=900):
        if not entries:
            return ""
        return ("【App 使用经验】\n" + "\n".join(
            f"- {e.label} / {e.page or '通用'}：{e.note}；步骤：{' → '.join(e.steps)}" for e in entries))[:max_chars]

    async def render_app(self, app, *, limit=2, max_chars=500):
        if not self.settings.app_memory_enabled:
            return ""
        if self.settings.app_memory_backend == "plugin":
            return self.service.app_memory.render_app(app, limit=limit, max_chars=max_chars)
        return self.render(await self.lookup(app, limit=limit), max_chars)

    async def render_for_prompt(self, *, limit=5, max_chars=900):
        if not self.settings.app_memory_enabled:
            return ""
        if self.settings.app_memory_backend == "plugin":
            return self.service.app_memory.render_for_prompt(limit=limit, max_chars=max_chars)
        helper = await self.helper()
        docs = await self.documents(helper)
        entries = [await self.read_entry(helper, doc) for doc in docs]
        entries.sort(key=lambda e: (e.updated_at, e.version), reverse=True)
        unique = {}
        for entry in entries:
            unique.setdefault((entry.app, entry.page), entry)
        return self.render(list(unique.values())[:limit], max_chars)
