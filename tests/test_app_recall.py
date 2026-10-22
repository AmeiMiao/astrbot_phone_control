"""App 使用经验独立召回：独立索引目录、向量配置、语义排序与档案兜底。"""

from __future__ import annotations

import asyncio

from core.app_memory import AppMemoryStore
from core.app_recall import (
    APP_RECALL_HEADER,
    AppRecallMaintainer,
    AppRecallReader,
    app_index_root,
    build_query,
    render_entries,
)
from core.config import PhoneConfig
from core.semantic import SemanticIndex
from core.semantic_recall import SemanticEmbeddingResolver


class _Provider:
    provider_config = {"id": "fake-embed", "embedding_model": "fake-model"}
    is_default = False

    def get_dim(self) -> int:
        return 3

    def get_model(self) -> str:
        return "fake-model"

    @staticmethod
    def _vector(text: str) -> list[float]:
        if "发布" in text:
            return [1.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0]

    async def get_embedding(self, text: str) -> list[float]:
        return self._vector(text)

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


class _Context:
    def __init__(self, providers=None) -> None:
        self._providers = list(providers or [])

    def get_all_embedding_providers(self):
        return list(self._providers)

    def get_provider_by_id(self, provider_id: str):
        for provider in self._providers:
            if provider.provider_config["id"] == provider_id:
                return provider
        return None


def _config(*, enabled: bool = True, provider_id: str = "fake-embed") -> PhoneConfig:
    return PhoneConfig.from_mapping(
        {
            "prompt": {
                "app_memory_enabled": enabled,
                "app_memory_embedding_provider_id": provider_id,
            }
        }
    )


def _store(tmp_path) -> AppMemoryStore:
    store = AppMemoryStore(tmp_path)
    store.remember(
        app="抖音",
        page="发布",
        note="右下角 + 号进入发布页，相册在第一行第二个格子",
        steps=["点击右下角 + 号", "选择相册"],
        ts=20.0,
    )
    store.remember(
        app="抖音",
        page="首页",
        note="首页顶部是搜索入口，下面是推荐信息流",
        ts=10.0,
    )
    return store


def test_app_index_is_separate_from_chat_index(tmp_path):
    assert app_index_root(tmp_path) == tmp_path / "semantic_app"
    assert app_index_root(tmp_path) != tmp_path / "semantic"


def test_maintainer_builds_separate_app_index(tmp_path):
    config = _config()
    provider = _Provider()
    index = SemanticIndex(app_index_root(tmp_path))
    maintainer = AppRecallMaintainer(
        index=index,
        resolver=SemanticEmbeddingResolver(
            _Context([provider]), label="App 经验召回"
        ),
        config_getter=lambda: config,
        data_dir=tmp_path,
        startup_delay_seconds=0,
        debounce_seconds=0,
    )
    _store(tmp_path)

    assert asyncio.run(maintainer.refresh()) is True

    snapshot = index.load_snapshot(allow_incompatible=True)
    assert snapshot is not None
    assert snapshot.count == 2
    assert (index.root / "status.json").is_file()
    # 聊天找回索引目录不受影响
    assert not (tmp_path / "semantic").exists()


def test_maintainer_skips_when_disabled(tmp_path):
    provider = _Provider()
    index = SemanticIndex(app_index_root(tmp_path))
    maintainer = AppRecallMaintainer(
        index=index,
        resolver=SemanticEmbeddingResolver(_Context([provider])),
        config_getter=lambda: _config(enabled=False),
        data_dir=tmp_path,
        startup_delay_seconds=0,
        debounce_seconds=0,
    )
    _store(tmp_path)
    assert asyncio.run(maintainer.refresh()) is False
    assert index.read_manifest() is None


def test_reader_semantic_order_prefers_matching_page(tmp_path):
    store = _store(tmp_path)
    index = SemanticIndex(app_index_root(tmp_path))
    provider = _Provider()
    maintainer = AppRecallMaintainer(
        index=index,
        resolver=SemanticEmbeddingResolver(_Context([provider])),
        config_getter=lambda: _config(),
        data_dir=tmp_path,
        startup_delay_seconds=0,
        debounce_seconds=0,
    )
    assert asyncio.run(maintainer.refresh()) is True
    reader = AppRecallReader(
        index=index,
        resolver=SemanticEmbeddingResolver(_Context([provider])),
        config_getter=lambda: _config().prompt,
        entry_provider=store.entries,
    )
    text = asyncio.run(
        reader.render(
            app_key="com.ss.android.ugc.aweme",
            label="抖音",
            page_hint="发布页",
            context="页面有 + 号与相册入口",
            limit=1,
        )
    )
    assert APP_RECALL_HEADER in text
    assert "右下角 + 号进入发布页" in text
    assert "步骤：点击右下角 + 号" in text
    assert "首页顶部是搜索入口" not in text


def test_reader_falls_back_to_local_archive_without_provider(tmp_path):
    store = _store(tmp_path)
    index = SemanticIndex(app_index_root(tmp_path))
    reader = AppRecallReader(
        index=index,
        resolver=SemanticEmbeddingResolver(_Context([])),
        config_getter=lambda: _config().prompt,
        entry_provider=store.entries,
    )
    text = asyncio.run(
        reader.render(
            app_key="com.ss.android.ugc.aweme",
            label="抖音",
            page_hint="首页",
            limit=1,
        )
    )
    assert "首页顶部是搜索入口" in text


def test_reader_returns_empty_when_disabled_or_unknown_app(tmp_path):
    store = _store(tmp_path)
    reader = AppRecallReader(
        index=SemanticIndex(app_index_root(tmp_path)),
        resolver=SemanticEmbeddingResolver(_Context([])),
        config_getter=lambda: _config(enabled=False).prompt,
        entry_provider=store.entries,
    )
    assert asyncio.run(reader.render(app_key="com.ss.android.ugc.aweme")) == ""
    reader_enabled = AppRecallReader(
        index=SemanticIndex(app_index_root(tmp_path)),
        resolver=SemanticEmbeddingResolver(_Context([])),
        config_getter=lambda: _config().prompt,
        entry_provider=store.entries,
    )
    assert asyncio.run(reader_enabled.render(app_key="unknown.app")) == ""


def test_build_query_and_render_entries_limits():
    query = build_query(
        label="抖音", app_key="com.ss.android.ugc.aweme", page_hint="发布", context="按钮"
    )
    assert "抖音" in query and "发布" in query and "按钮" in query

    store_entries = [
        type(
            "Entry",
            (),
            {
                "app": "com.x",
                "label": "X",
                "page": "发布",
                "note": "经验正文",
                "steps": ("步骤一", "步骤二"),
                "updated_at": 1_760_000_000.0,
                "hits": 1,
                "version": 1,
            },
        )()
    ]
    text = render_entries(store_entries, limit=1, max_chars=300)
    assert APP_RECALL_HEADER in text
    assert "经验正文" in text
    assert "步骤一" in text
