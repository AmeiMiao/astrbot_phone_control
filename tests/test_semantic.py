"""聊天语义找回纯逻辑测试：索引构建/增量/检索/渲染与只读 Reader、后台维护器。

不依赖真实 AstrBot（本地由 ``tests/conftest.py`` 提供 ``astrbot.api.logger`` 桩），
用假 Embedding Provider 与临时数据目录覆盖：

* 本地档案收集（学习/日记/App 经验、开关、去重、上限、截断、空数据）；
* 索引构建、增量（新增/修改/删除）、provider/维度变化、损坏重建、原子写失败；
* 余弦相似度检索排序、阈值、top_k、同 hash 去重与注入文本渲染；
* Embedding Provider 解析（显式/自动/默认标记/缺失告警一次）；
* Reader 只读路径（无索引/无 provider/超时/维度不符/损坏自愈/快照缓存）；
* Maintainer 后台维护（写入触发、周期/事件循环、失败退避、知识库后端跳过）。
"""

from __future__ import annotations

import asyncio
import json
import time
import types
from pathlib import Path

import pytest

from core.config import PhoneConfig
from core.semantic import (
    DEFAULT_DOC_CHARS,
    RECALL_HEADER,
    RECALL_MARKER,
    SOURCE_APP_EXPERIENCE,
    SOURCE_DIARY,
    SOURCE_LEARNING,
    BuildResult,
    SemanticBuildError,
    SemanticDoc,
    SemanticHit,
    SemanticIndex,
    SemanticSnapshot,
    build_index,
    collect_documents,
    format_hits,
    normalize_vector,
    provider_key,
    read_app_memory_entries,
    truncate_text,
)
from core.semantic_recall import (
    SemanticEmbeddingResolver,
    SemanticMaintainer,
    SemanticReader,
    _provider_id_of,
    _provider_model_of,
)


# ----------------------------------------------------------------------
# 测试夹具：假 Provider / Context / Embedding 函数
# ----------------------------------------------------------------------


class FakeEmbeddingProvider:
    """最小 Embedding Provider：显式向量映射 + 调用计数 + 可注入失败/延迟。"""

    provider_config = {"id": "fake-embed", "embedding_model": "fake-model"}
    is_default = False

    def __init__(
        self,
        *,
        dim: int = 3,
        vectors: dict[str, list[float]] | None = None,
        default_vector: list[float] | None = None,
        delay: float = 0.0,
        fail: Exception | None = None,
    ) -> None:
        self.dim = dim
        self.vectors = vectors or {}
        self.default_vector = default_vector or [1.0] * dim
        self.delay = delay
        self.fail = fail
        self.embedding_calls: list[str] = []
        self.batch_calls: list[list[str]] = []

    def get_dim(self) -> int:
        return self.dim

    def get_model(self) -> str:
        return "fake-model"

    async def get_embedding(self, text: str) -> list[float]:
        self.embedding_calls.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return list(self.vectors.get(text, self.default_vector))

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls.append(list(texts))
        return [await self.get_embedding(text) for text in texts]


class FakeContext:
    """最小 AstrBot Context：只提供 Provider 查询接口。"""

    def __init__(self, providers: list[FakeEmbeddingProvider] | None = None) -> None:
        self._providers = list(providers or [])
        self.provider_manager = types.SimpleNamespace(
            embedding_provider_insts=self._providers
        )

    def get_all_embedding_providers(self):
        return list(self._providers)

    def get_provider_by_id(self, provider_id: str):
        for provider in self._providers:
            if _provider_id_of(provider) == provider_id:
                return provider
        return None


class RecordingLogger:
    """记录日志调用的假 logger（替换 semantic_recall.logger）。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.debugs: list[str] = []

    def warning(self, message, *args) -> None:
        self.warnings.append(message % args if args else message)

    def info(self, message, *args) -> None:
        self.infos.append(message % args if args else message)

    def debug(self, message, *args) -> None:
        self.debugs.append(message % args if args else message)


class MappingEmbedder:
    """按文本精确映射向量的假 embedding 函数（同步，记录每批输入）。"""

    def __init__(self, mapping: dict[str, list[float]], default: list[float]) -> None:
        self.mapping = mapping
        self.default = default
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [list(self.mapping.get(text, self.default)) for text in texts]

    @property
    def total(self) -> int:
        return sum(len(batch) for batch in self.calls)


def _recall_config(**overrides) -> PhoneConfig:
    card = {"enabled": True}
    card.update(overrides)
    return PhoneConfig.from_mapping({"recall": card})


def _write_learning(root: Path, records: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "learning.jsonl").write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def _learning_records(count: int, *, start_ts: float = 1.0) -> list[dict]:
    return [
        {
            "id": f"l{index}",
            "ts": start_ts + index,
            "content": f"学习内容{index}",
            "source": "B站",
            "tags": ["AI"],
        }
        for index in range(count)
    ]


def _doc(doc_id: str, text: str, *, source: str = SOURCE_LEARNING, ts: float = 1.0) -> SemanticDoc:
    return SemanticDoc(
        id=doc_id,
        source=source,
        source_id=doc_id,
        text=text,
        display=text,
        ts=ts,
    )


async def _wait_until(predicate, *, timeout: float = 3.0) -> bool:
    """轮询等待异步条件成立（用于后台维护循环测试）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


# ----------------------------------------------------------------------
# 本地档案收集
# ----------------------------------------------------------------------


def test_collect_documents_reads_all_sources_and_sorts_by_time(tmp_path):
    _write_learning(
        tmp_path,
        [
            {
                "id": "learn-1",
                "ts": 10.0,
                "content": "B站转正答题要 60 分",
                "source": "哔哩哔哩",
                "tags": ["AI", "答题"],
            }
        ],
    )
    diary_dir = tmp_path / "diary"
    diary_dir.mkdir()
    (diary_dir / "diary.jsonl").write_text(
        json.dumps(
            {
                "id": "diary-1",
                "ts": 20.0,
                "app": "抖音",
                "items": [
                    {"title": "猫咪视频", "author": "小明", "text": "猫在跳舞"}
                ],
                "note": "注意评论区",
                "summary": "看了猫咪视频",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "app_memory.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "app": "com.ss.android.ugc.aweme",
                        "label": "抖音",
                        "page": "发布",
                        "note": "右下角 + 号进入发布页",
                        "steps": ["点击 +", "选择相册"],
                        "updated_at": 30.0,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    docs = collect_documents(tmp_path)
    ids = [doc.id for doc in docs]
    assert ids == [
        f"{SOURCE_APP_EXPERIENCE}:com.ss.android.ugc.aweme:发布",
        f"{SOURCE_DIARY}:diary-1:0",
        f"{SOURCE_LEARNING}:learn-1",
    ]
    by_id = {doc.id: doc for doc in docs}
    learning = by_id[f"{SOURCE_LEARNING}:learn-1"]
    assert "B站转正答题要 60 分" in learning.text
    assert "哔哩哔哩" in learning.text
    assert "AI" in learning.text
    assert learning.display == "B站转正答题要 60 分"
    diary = by_id[f"{SOURCE_DIARY}:diary-1:0"]
    assert "抖音" in diary.text and "猫咪视频" in diary.text
    assert "看了猫咪视频" in diary.text
    assert diary.display == "抖音：猫咪视频（小明）"
    app = by_id[f"{SOURCE_APP_EXPERIENCE}:com.ss.android.ugc.aweme:发布"]
    assert "右下角 + 号进入发布页" in app.text
    assert "步骤：点击 + → 选择相册" in app.text
    assert app.display.startswith("抖音 / 发布：")
    # id 稳定：同输入重复收集结果一致
    assert [doc.id for doc in collect_documents(tmp_path)] == ids


def test_collect_documents_include_flags(tmp_path):
    _write_learning(tmp_path, _learning_records(1))
    diary_dir = tmp_path / "diary"
    diary_dir.mkdir()
    (diary_dir / "diary.jsonl").write_text(
        json.dumps(
            {"id": "d1", "ts": 2.0, "app": "抖音", "items": [], "summary": "看视频"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "app_memory.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {"app": "x", "label": "X", "page": "", "note": "经验", "updated_at": 3.0}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    only_learning = collect_documents(
        tmp_path,
        include_learning=True,
        include_diary=False,
        include_app_experience=False,
    )
    assert [doc.source for doc in only_learning] == [SOURCE_LEARNING]
    only_diary = collect_documents(
        tmp_path,
        include_learning=False,
        include_diary=True,
        include_app_experience=False,
    )
    assert [doc.source for doc in only_diary] == [SOURCE_DIARY]
    none = collect_documents(
        tmp_path,
        include_learning=False,
        include_diary=False,
        include_app_experience=False,
    )
    assert none == []


def test_collect_documents_dedupes_by_id_and_truncates_max_docs(tmp_path):
    records = _learning_records(5)
    # 同 id 重复写入时保留时间较新的内容
    records.append(
        {"id": "l0", "ts": 99.0, "content": "覆盖后的内容", "source": "B站", "tags": []}
    )
    _write_learning(tmp_path, records)
    docs = collect_documents(tmp_path, index_max_docs=2)
    assert len(docs) == 2
    assert docs[0].id == f"{SOURCE_LEARNING}:l0"
    assert docs[0].display == "覆盖后的内容"
    assert docs[1].id == f"{SOURCE_LEARNING}:l4"


def test_collect_documents_truncates_doc_chars(tmp_path):
    _write_learning(
        tmp_path,
        [
            {
                "id": "long",
                "ts": 1.0,
                "content": "字" * 500,
                "source": "来源" * 50,
                "tags": ["标签" * 30],
            }
        ],
    )
    docs = collect_documents(tmp_path, doc_chars=40)
    assert len(docs) == 1
    assert len(docs[0].text) == 40
    assert len(docs[0].display) == 40
    assert docs[0].text.endswith("…")


def test_collect_documents_empty_data(tmp_path):
    assert collect_documents(tmp_path) == []
    # 坏 JSON/坏 JSONL 全部跳过，不抛异常
    (tmp_path / "learning.jsonl").write_text('{"id": 1, bad json\n\n', encoding="utf-8")
    (tmp_path / "app_memory.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "diary").mkdir()
    (tmp_path / "diary" / "diary.jsonl").write_text("[1,2,3]\n", encoding="utf-8")
    assert collect_documents(tmp_path) == []
    assert read_app_memory_entries(tmp_path / "app_memory.json") == []


def test_default_id_is_stable_for_legacy_records(tmp_path):
    """旧学习记录没有 id 时按内容+时间生成稳定 id（重建索引不会重复）。"""
    record = {"ts": 5.0, "content": "旧记录", "source": "B站", "tags": []}
    _write_learning(tmp_path, [record])
    first = collect_documents(tmp_path)
    second = collect_documents(tmp_path)
    assert first[0].id == second[0].id
    assert first[0].id.startswith(f"{SOURCE_LEARNING}:")


# ----------------------------------------------------------------------
# 索引构建与增量
# ----------------------------------------------------------------------


async def _build(
    index: SemanticIndex,
    docs: list[SemanticDoc],
    embed,
    *,
    dim: int = 3,
    provider_id: str = "fake-embed",
    model: str = "fake-model",
    doc_chars: int = DEFAULT_DOC_CHARS,
    index_max_docs: int = 2000,
    force: bool = False,
) -> BuildResult:
    return await build_index(
        index,
        docs,
        embed=embed,
        provider_id=provider_id,
        model=model,
        dim=dim,
        doc_chars=doc_chars,
        index_max_docs=index_max_docs,
        force=force,
    )


def test_normalize_vector_and_truncate_helpers():
    assert normalize_vector([3.0, 4.0]) == pytest.approx([0.6, 0.8])
    assert normalize_vector([0.0, 0.0]) == []
    assert normalize_vector(["bad"]) == []
    assert truncate_text("abcdef", 3) == "ab…"
    assert truncate_text("abc", 3) == "abc"
    assert truncate_text("abc", 0) == ""


def test_build_index_writes_manifest_entries_vectors(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")
    docs = [_doc("learning:a", "甲"), _doc("learning:b", "乙")]
    embed = MappingEmbedder({"甲": [1.0, 0.0, 0.0], "乙": [0.0, 1.0, 0.0]}, [1.0, 0.0, 0.0])
    result = asyncio.run(_build(index, docs, embed))
    assert result.changed is True
    assert result.count == 2
    assert result.embedded == 2
    manifest = index.read_manifest()
    assert manifest["version"] == 1
    assert manifest["count"] == 2
    assert manifest["dim"] == 3
    assert manifest["provider_key"] == provider_key("fake-embed", "fake-model", 3)
    assert manifest["sources"] == {SOURCE_LEARNING: 2}
    assert index.manifest_path.is_file()
    assert (index.root / manifest["entries_file"]).is_file()
    assert (index.root / manifest["vectors_file"]).is_file()
    lines = (index.root / manifest["entries_file"]).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["offset"] == 0 and first["dim"] == 3
    assert json.loads(lines[1])["offset"] == 3

    snapshot = index.load_snapshot(provider_key=manifest["provider_key"])
    assert snapshot is not None
    assert snapshot.count == 2
    assert snapshot.dim == 3
    # 写入前已 L2 归一化
    vector = snapshot.vector_of("learning:a")
    assert vector is not None
    assert sum(value * value for value in vector) == pytest.approx(1.0)


def test_build_index_incremental_add_modify_delete(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")
    mapping = {"甲": [1.0, 0.0, 0.0], "乙": [0.0, 1.0, 0.0], "甲二": [1.0, 0.0, 0.0], "丙": [0.0, 0.0, 1.0]}
    embed = MappingEmbedder(mapping, [1.0, 0.0, 0.0])
    docs = [_doc("learning:a", "甲", ts=2.0), _doc("learning:b", "乙", ts=1.0)]
    asyncio.run(_build(index, docs, embed))
    assert embed.total == 2

    # 无变化：不写盘也不重新 embedding
    second = asyncio.run(_build(index, docs, embed))
    assert second.changed is False
    assert second.embedded == 0
    assert embed.total == 2

    # 修改 a、新增 c、删除 b：只 embedding 变化的 2 条
    updated = [_doc("learning:a", "甲二", ts=2.0), _doc("learning:c", "丙", ts=3.0)]
    third = asyncio.run(_build(index, updated, embed))
    assert third.changed is True
    assert third.embedded == 2
    assert third.reused == 0
    assert embed.total == 4
    snapshot = index.load_snapshot(allow_incompatible=True)
    assert snapshot is not None
    assert [doc.id for doc in snapshot.docs] == ["learning:a", "learning:c"]
    assert snapshot.vector_of("learning:a") == pytest.approx([1.0, 0.0, 0.0])

    # 仅删除：无新增/修改时不重新 embedding，但会写新 generation
    only_a = [_doc("learning:a", "甲二", ts=2.0)]
    fourth = asyncio.run(_build(index, only_a, embed))
    assert fourth.changed is True
    assert fourth.embedded == 0
    assert embed.total == 4
    snapshot = index.load_snapshot(allow_incompatible=True)
    assert [doc.id for doc in snapshot.docs] == ["learning:a"]


def test_build_index_provider_change_reembeds_all(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")
    docs = [_doc("learning:a", "甲"), _doc("learning:b", "乙")]
    embed = MappingEmbedder({"甲": [1.0, 0.0, 0.0], "乙": [0.0, 1.0, 0.0]}, [1.0, 0.0, 0.0])
    asyncio.run(_build(index, docs, embed, provider_id="p1", model="m1", dim=3))
    assert embed.total == 2
    result = asyncio.run(_build(index, docs, embed, provider_id="p2", model="m2", dim=3))
    assert result.embedded == 2
    assert embed.total == 4
    # 用旧 provider 无法读取新索引（避免跨模型算相似度）
    assert index.load_snapshot(provider_key=provider_key("p1", "m1", 3)) is None
    assert index.load_snapshot(provider_key=provider_key("p2", "m2", 3)) is not None


def test_build_index_rebuilds_corrupted_index(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")
    embed = MappingEmbedder({"甲": [1.0, 0.0, 0.0]}, [1.0, 0.0, 0.0])
    docs = [_doc("learning:a", "甲")]
    asyncio.run(_build(index, docs, embed))
    manifest = index.read_manifest()
    assert manifest is not None
    # 截断向量文件 → 读取失败，构建按全量重建
    (index.root / manifest["vectors_file"]).write_bytes(b"\x00\x01")
    assert index.load_snapshot(allow_incompatible=True) is None
    result = asyncio.run(_build(index, docs, embed))
    assert result.changed is True
    assert result.embedded == 1
    assert index.load_snapshot(allow_incompatible=True) is not None


@pytest.mark.parametrize("broken", ["manifest", "entries", "vectors"])
def test_load_snapshot_rejects_corrupted_files(tmp_path, broken):
    index = SemanticIndex(tmp_path / "semantic")
    embed = MappingEmbedder({"甲": [1.0, 0.0, 0.0]}, [1.0, 0.0, 0.0])
    asyncio.run(_build(index, [_doc("learning:a", "甲")], embed))
    manifest = index.read_manifest()
    assert manifest is not None
    if broken == "manifest":
        index.manifest_path.write_text("{bad json", encoding="utf-8")
    elif broken == "entries":
        (index.root / manifest["entries_file"]).write_text(
            '{"id": "x"}\n', encoding="utf-8"
        )
    else:
        (index.root / manifest["vectors_file"]).write_bytes(b"\x00" * 7)
    assert index.load_snapshot(allow_incompatible=True) is None


def test_build_index_embedding_dim_mismatch_keeps_old_index(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")
    embed = MappingEmbedder({"甲": [1.0, 0.0, 0.0]}, [1.0, 0.0, 0.0])
    docs = [_doc("learning:a", "甲")]
    asyncio.run(_build(index, docs, embed))
    old_manifest = index.read_manifest()
    with pytest.raises(SemanticBuildError):
        asyncio.run(_build(index, docs, embed, dim=4))
    assert index.read_manifest() == old_manifest
    assert index.load_snapshot(allow_incompatible=True) is not None


def test_build_index_write_failure_keeps_old_index(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")
    embed = MappingEmbedder({"甲": [1.0, 0.0, 0.0], "乙": [0.0, 1.0, 0.0]}, [1.0, 0.0, 0.0])
    asyncio.run(_build(index, [_doc("learning:a", "甲")], embed))
    old_manifest = index.read_manifest()

    def _fail(*_args, **_kwargs):
        raise OSError("磁盘已满")

    index.write_generation = _fail  # type: ignore[method-assign]
    with pytest.raises(OSError):
        asyncio.run(_build(index, [_doc("learning:a", "甲"), _doc("learning:b", "乙")], embed))
    index.write_generation = SemanticIndex.write_generation.__get__(index)  # type: ignore[method-assign]
    assert index.read_manifest() == old_manifest
    assert index.load_snapshot(allow_incompatible=True) is not None


def test_build_empty_index(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")

    def _must_not_embed(texts):
        raise AssertionError(f"空索引不应调用 embedding：{texts}")

    result = asyncio.run(
        _build(index, [], _must_not_embed, dim=3)
    )
    assert result.count == 0
    assert result.changed is True
    snapshot = index.load_snapshot(allow_incompatible=True)
    assert snapshot is not None
    assert snapshot.count == 0
    assert snapshot.search([1.0, 0.0, 0.0]) == []


def test_build_index_cleanup_keeps_previous_generation(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")
    embed = MappingEmbedder({"甲": [1.0, 0.0, 0.0]}, [1.0, 0.0, 0.0])
    asyncio.run(_build(index, [_doc("learning:a", "甲")], embed))
    first = index.read_manifest()
    asyncio.run(_build(index, [_doc("learning:a", "甲2")], MappingEmbedder({"甲2": [1.0, 0.0, 0.0]}, [1.0, 0.0, 0.0])))
    second = index.read_manifest()
    names = {path.name for path in index.root.iterdir()}
    assert second["generation"] != first["generation"]
    assert first["entries_file"] in names
    assert first["vectors_file"] in names
    assert second["entries_file"] in names
    assert not any(name.endswith(".tmp") for name in names)


def test_build_index_force_rewrites_generation(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")
    embed = MappingEmbedder({"甲": [1.0, 0.0, 0.0]}, [1.0, 0.0, 0.0])
    docs = [_doc("learning:a", "甲")]
    first = asyncio.run(_build(index, docs, embed))
    forced = asyncio.run(_build(index, docs, embed, force=True))
    assert forced.changed is True
    assert forced.generation != first.generation


# ----------------------------------------------------------------------
# 检索与渲染
# ----------------------------------------------------------------------


def _searchable_snapshot(tmp_path) -> SemanticSnapshot:
    index = SemanticIndex(tmp_path / "semantic")
    mapping = {
        "甲": [1.0, 0.0, 0.0],
        "乙": [0.8, 0.6, 0.0],
        "丙": [0.0, 1.0, 0.0],
    }
    asyncio.run(
        _build(
            index,
            [
                _doc("learning:a", "甲", ts=1.0),
                _doc("learning:b", "乙", ts=2.0),
                _doc("learning:c", "丙", ts=3.0),
            ],
            MappingEmbedder(mapping, [1.0, 0.0, 0.0]),
        )
    )
    snapshot = index.load_snapshot(allow_incompatible=True)
    assert snapshot is not None
    return snapshot


def test_search_sorts_by_score_and_applies_top_k(tmp_path):
    snapshot = _searchable_snapshot(tmp_path)
    hits = snapshot.search([1.0, 0.0, 0.0], top_k=2, min_score=0.0)
    assert [hit.doc.id for hit in hits] == ["learning:a", "learning:b"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(0.8)
    assert snapshot.search([1.0, 0.0, 0.0], top_k=2, min_score=0.85)[0].doc.id == "learning:a"
    assert len(snapshot.search([1.0, 0.0, 0.0], top_k=2, min_score=0.85)) == 1
    assert snapshot.search([1.0, 0.0, 0.0], top_k=0) == []
    assert snapshot.search([0.0, 0.0, 0.0]) == []


def test_search_dedupes_same_document_hash(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")
    # 两条文档正文相同 → hash 相同，只保留最高分一条
    docs = [_doc("learning:a", "甲", ts=2.0), _doc("learning:b", "甲", ts=1.0)]
    asyncio.run(_build(index, docs, MappingEmbedder({"甲": [1.0, 0.0, 0.0]}, [1.0, 0.0, 0.0])))
    snapshot = index.load_snapshot(allow_incompatible=True)
    assert snapshot is not None
    hits = snapshot.search([1.0, 0.0, 0.0], top_k=5, min_score=0.0)
    assert len(hits) == 1


def test_format_hits_renders_chinese_section_with_limits():
    learning = SemanticDoc(
        id="learning:a",
        source=SOURCE_LEARNING,
        source_id="a",
        text="B站｜#AI\nB站转正答题要 60 分",
        display="B站转正答题要 60 分",
        ts=1_760_000_000.0,
        meta={"source": "哔哩哔哩"},
    )
    docs = [
        learning,
        SemanticDoc(
            id="diary:1:0",
            source=SOURCE_DIARY,
            source_id="1",
            text="抖音：猫咪视频",
            display="抖音：猫咪视频",
            ts=1_760_000_100.0,
        ),
        SemanticDoc(
            id=f"{SOURCE_APP_EXPERIENCE}:x:_",
            source=SOURCE_APP_EXPERIENCE,
            source_id="x",
            text="抖音 / 发布：右下角 + 号",
            display="抖音 / 发布：右下角 + 号",
            ts=1_760_000_200.0,
        ),
    ]
    hits = [SemanticHit(doc=doc, score=0.9) for doc in docs]
    text = format_hits(hits, max_chars=1200)
    assert text.startswith(RECALL_HEADER)
    assert RECALL_MARKER in text
    assert "[学习｜" in text and "（来源：哔哩哔哩）" in text
    assert "[浏览｜" in text and "猫咪视频" in text
    assert "[App经验｜" in text
    assert text.endswith("。）")
    assert len(text) <= 1200
    assert format_hits([], max_chars=1200) == ""
    # 上限过小放不下正文时不注入空 section
    assert format_hits(hits, max_chars=60) == ""


def test_format_hits_truncates_to_max_chars():
    docs = [
        _doc(f"learning:{index}", "内容" * 100, ts=1_760_000_000.0 + index)
        for index in range(10)
    ]
    hits = [SemanticHit(doc=doc, score=0.9) for doc in docs]
    text = format_hits(hits, max_chars=400)
    assert 0 < len(text) <= 400
    assert text.startswith(RECALL_HEADER)
    assert text.endswith("。）")


# ----------------------------------------------------------------------
# Embedding Provider 解析
# ----------------------------------------------------------------------


def test_provider_helpers_extract_identity():
    provider = FakeEmbeddingProvider()
    assert _provider_id_of(provider) == "fake-embed"
    assert _provider_model_of(provider) == "fake-model"

    class Bare:
        pass

    assert _provider_id_of(Bare()).endswith("Bare")


def test_resolver_auto_selects_sorted_first(monkeypatch):
    from core import semantic_recall as semantic_recall

    fake_logger = RecordingLogger()
    monkeypatch.setattr(semantic_recall, "logger", fake_logger)
    first = FakeEmbeddingProvider()
    first.provider_config = {"id": "b-embed", "embedding_model": "m"}
    second = FakeEmbeddingProvider()
    second.provider_config = {"id": "a-embed", "embedding_model": "m"}
    resolver = SemanticEmbeddingResolver(FakeContext([first, second]))
    resolved = resolver.resolve("")
    assert resolved is not None
    assert resolved.provider_id == "a-embed"
    assert resolved.dim == 3
    assert resolved.key == provider_key("a-embed", "fake-model", 3)
    assert any("自动选择" in message for message in fake_logger.infos)


def test_resolver_prefers_default_marker(monkeypatch):
    from core import semantic_recall as semantic_recall

    monkeypatch.setattr(semantic_recall, "logger", RecordingLogger())
    first = FakeEmbeddingProvider()
    first.provider_config = {"id": "a-embed", "embedding_model": "m"}
    second = FakeEmbeddingProvider()
    second.provider_config = {"id": "b-embed", "embedding_model": "m"}
    second.is_default = True
    resolver = SemanticEmbeddingResolver(FakeContext([first, second]))
    resolved = resolver.resolve("")
    assert resolved is not None and resolved.provider_id == "b-embed"


def test_resolver_warns_once_for_missing_provider(monkeypatch):
    from core import semantic_recall as semantic_recall

    fake_logger = RecordingLogger()
    monkeypatch.setattr(semantic_recall, "logger", fake_logger)
    resolver = SemanticEmbeddingResolver(FakeContext([]))
    assert resolver.resolve("nope") is None
    assert resolver.resolve("nope") is None
    assert len(fake_logger.warnings) == 1
    assert "nope" in fake_logger.warnings[0]
    # 无任何 Embedding Provider 时也只警告一次
    assert resolver.resolve("") is None
    assert resolver.resolve("") is None
    assert len(fake_logger.warnings) == 2


def test_resolver_rejects_non_embedding_provider(monkeypatch):
    from core import semantic_recall as semantic_recall

    fake_logger = RecordingLogger()
    monkeypatch.setattr(semantic_recall, "logger", fake_logger)
    context = FakeContext([])
    context.get_provider_by_id = lambda _pid: object()  # 不是 Embedding Provider
    resolver = SemanticEmbeddingResolver(context)
    assert resolver.resolve("chat-model") is None
    assert any("不是 Embedding 类型" in message for message in fake_logger.warnings)


# ----------------------------------------------------------------------
# Reader（只读检索路径）
# ----------------------------------------------------------------------


def _reader_fixture(tmp_path, provider, *, enabled=True, **recall_overrides):
    index = SemanticIndex(tmp_path / "semantic")
    resolver = SemanticEmbeddingResolver(FakeContext([provider]))
    config = _recall_config(**recall_overrides) if enabled else PhoneConfig.from_mapping(
        {"recall": {"enabled": False}}
    )
    reader = SemanticReader(index, resolver, lambda: config.recall)
    return index, resolver, config, reader


def test_reader_returns_empty_without_index(tmp_path, monkeypatch):
    from core import semantic_recall as semantic_recall

    fake_logger = RecordingLogger()
    monkeypatch.setattr(semantic_recall, "logger", fake_logger)
    provider = FakeEmbeddingProvider()
    index, _resolver, _config, reader = _reader_fixture(tmp_path, provider)
    assert asyncio.run(reader.render("B站答题")) == ""
    assert provider.embedding_calls == []


def test_reader_returns_empty_when_disabled(tmp_path):
    provider = FakeEmbeddingProvider()
    index, _resolver, _config, reader = _reader_fixture(
        tmp_path, provider, enabled=False
    )
    assert asyncio.run(reader.render("B站答题")) == ""
    assert provider.embedding_calls == []


def test_reader_returns_empty_without_provider(tmp_path, monkeypatch):
    from core import semantic_recall as semantic_recall

    fake_logger = RecordingLogger()
    monkeypatch.setattr(semantic_recall, "logger", fake_logger)
    index = SemanticIndex(tmp_path / "semantic")
    resolver = SemanticEmbeddingResolver(FakeContext([]))
    config = _recall_config()
    reader = SemanticReader(index, resolver, lambda: config.recall)
    assert asyncio.run(reader.render("B站答题")) == ""
    assert len(fake_logger.warnings) == 1


def test_reader_injects_after_index_built(tmp_path):
    provider = FakeEmbeddingProvider(vectors={"甲": [1.0, 0.0, 0.0]})
    index, resolver, config, reader = _reader_fixture(tmp_path, provider)
    docs = [_doc("learning:a", "甲", ts=1.0)]
    asyncio.run(build_index(index, docs, embed=provider.get_embeddings, provider_id="fake-embed", model="fake-model", dim=3))
    text = asyncio.run(reader.render("甲"))
    assert text.startswith(RECALL_HEADER)
    assert "甲" in text
    assert provider.embedding_calls[-1] == "甲"


def test_reader_returns_empty_on_dim_mismatch_and_timeout(tmp_path):
    # 维度不一致
    provider = FakeEmbeddingProvider(vectors={"甲": [1.0, 0.0, 0.0]})
    index, _resolver, _config, reader = _reader_fixture(tmp_path, provider)
    asyncio.run(build_index(index, [_doc("learning:a", "甲")], embed=provider.get_embeddings, provider_id="fake-embed", model="fake-model", dim=3))
    provider.default_vector = [1.0, 0.0, 0.0, 0.0]  # 查询向量维度变了
    assert asyncio.run(reader.render("未知查询")) == ""

    # 查询超时（配置最小 1 秒）
    slow = FakeEmbeddingProvider(vectors={"甲": [1.0, 0.0, 0.0]}, delay=2.0)
    index2 = SemanticIndex(tmp_path / "semantic2")
    asyncio.run(build_index(index2, [_doc("learning:a", "甲")], embed=slow.get_embeddings, provider_id="fake-embed", model="fake-model", dim=3))
    config = _recall_config(timeout_seconds=1)
    reader2 = SemanticReader(
        index2, SemanticEmbeddingResolver(FakeContext([slow])), lambda: config.recall
    )
    started = time.monotonic()
    assert asyncio.run(reader2.render("甲")) == ""
    assert time.monotonic() - started < 2.5


def test_reader_recovers_after_corrupted_index_rebuilt(tmp_path):
    provider = FakeEmbeddingProvider(vectors={"甲": [1.0, 0.0, 0.0]})
    index, resolver, config, reader = _reader_fixture(tmp_path, provider)
    _write_learning(
        tmp_path,
        [{"id": "a", "ts": 1.0, "content": "甲", "source": "", "tags": []}],
    )
    asyncio.run(build_index(index, [_doc("learning:a", "甲")], embed=provider.get_embeddings, provider_id="fake-embed", model="fake-model", dim=3))
    assert asyncio.run(reader.render("甲")).startswith(RECALL_HEADER)
    manifest = index.read_manifest()
    assert manifest is not None
    # 破坏索引 → 聊天不注入（不抛异常）
    index.manifest_path.write_text("{bad", encoding="utf-8")
    assert asyncio.run(reader.render("甲")) == ""
    # 后台重建后恢复注入（失败缓存按 manifest 文件标识失效）
    maintainer = SemanticMaintainer(
        index=index,
        resolver=resolver,
        config_getter=lambda: config,
        data_dir=tmp_path,
        startup_delay_seconds=0.0,
        debounce_seconds=0.0,
    )
    assert asyncio.run(maintainer.refresh()) is True
    assert asyncio.run(reader.render("甲")).startswith(RECALL_HEADER)


def test_reader_caches_snapshot_across_turns(tmp_path):
    provider = FakeEmbeddingProvider(vectors={"甲": [1.0, 0.0, 0.0]})
    index, resolver, config, reader = _reader_fixture(tmp_path, provider)
    asyncio.run(build_index(index, [_doc("learning:a", "甲")], embed=provider.get_embeddings, provider_id="fake-embed", model="fake-model", dim=3))
    loads = {"count": 0}
    original = index.load_snapshot

    def counting_load(*args, **kwargs):
        loads["count"] += 1
        return original(*args, **kwargs)

    index.load_snapshot = counting_load  # type: ignore[method-assign]
    provider.embedding_calls.clear()  # 构建阶段的调用不计入查询次数
    assert asyncio.run(reader.render("甲")).startswith(RECALL_HEADER)
    assert asyncio.run(reader.render("甲")).startswith(RECALL_HEADER)
    # 每轮都会调用一次查询 embedding，但索引文件只读取一次
    assert loads["count"] == 1
    assert len(provider.embedding_calls) == 2


# ----------------------------------------------------------------------
# Maintainer（后台索引维护）
# ----------------------------------------------------------------------


def _maintainer(tmp_path, provider, config, *, index=None, startup_delay=0.0, debounce=0.0):
    return SemanticMaintainer(
        index=index or SemanticIndex(tmp_path / "semantic"),
        resolver=SemanticEmbeddingResolver(FakeContext([provider])),
        config_getter=lambda: config,
        data_dir=tmp_path,
        startup_delay_seconds=startup_delay,
        debounce_seconds=debounce,
    )


def test_maintainer_refresh_builds_and_applies_increments(tmp_path, monkeypatch):
    from core import semantic_recall as semantic_recall

    fake_logger = RecordingLogger()
    monkeypatch.setattr(semantic_recall, "logger", fake_logger)
    provider = FakeEmbeddingProvider(vectors={"学习内容0": [1.0, 0.0, 0.0]})
    config = _recall_config()
    maintainer = _maintainer(tmp_path, provider, config)
    _write_learning(tmp_path, _learning_records(1))
    assert asyncio.run(maintainer.refresh()) is True
    snapshot = maintainer._index.load_snapshot(allow_incompatible=True)
    assert snapshot is not None and snapshot.count == 1
    assert (maintainer._index.root / "status.json").is_file()
    assert any("语义索引已更新" in message for message in fake_logger.infos)

    # 追加学习记录后再次刷新（增量）
    _write_learning(tmp_path, _learning_records(2))
    assert asyncio.run(maintainer.refresh()) is True
    snapshot = maintainer._index.load_snapshot(allow_incompatible=True)
    assert snapshot is not None and snapshot.count == 2


def test_maintainer_refresh_skips_when_disabled(tmp_path):
    provider = FakeEmbeddingProvider()
    config = PhoneConfig.from_mapping({"recall": {"enabled": False}})
    maintainer = _maintainer(tmp_path, provider, config)
    _write_learning(tmp_path, _learning_records(1))
    assert asyncio.run(maintainer.refresh()) is False
    assert provider.batch_calls == []
    assert maintainer._index.read_manifest() is None


def test_maintainer_skips_knowledge_base_app_experience(tmp_path):
    provider = FakeEmbeddingProvider(default_vector=[1.0, 0.0, 0.0])
    config = PhoneConfig.from_mapping(
        {
            "recall": {"enabled": True},
            "prompt": {"app_memory_backend": "knowledge_base"},
        }
    )
    maintainer = _maintainer(tmp_path, provider, config)
    (tmp_path / "app_memory.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {"app": "x", "label": "X", "page": "", "note": "经验", "updated_at": 3.0}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert asyncio.run(maintainer.refresh()) is True
    snapshot = maintainer._index.load_snapshot(allow_incompatible=True)
    assert snapshot is not None
    assert snapshot.count == 0


def test_maintainer_failure_backoff_and_recovery(tmp_path, monkeypatch):
    from core import semantic_recall as semantic_recall

    fake_logger = RecordingLogger()
    monkeypatch.setattr(semantic_recall, "logger", fake_logger)
    provider = FakeEmbeddingProvider(fail=RuntimeError("供应商不可用"))
    config = _recall_config()
    maintainer = _maintainer(tmp_path, provider, config)
    _write_learning(tmp_path, _learning_records(1))
    assert asyncio.run(maintainer.refresh()) is False
    assert len(fake_logger.warnings) == 1
    status = json.loads((maintainer._index.root / "status.json").read_text(encoding="utf-8"))
    assert status["ok"] is False
    assert status["failures"] == 1
    calls_before = len(provider.batch_calls)
    # 退避期内再次刷新不会重复调用 embedding，也不重复告警
    assert asyncio.run(maintainer.refresh()) is False
    assert len(provider.batch_calls) == calls_before
    assert len(fake_logger.warnings) == 1
    # 修复后强制刷新恢复
    provider.fail = None
    assert asyncio.run(maintainer.refresh(force=True)) is True
    # 恢复后重建成功（本次有新增文档，记录“已更新”），失败计数清零
    assert maintainer._failures == 0
    status = json.loads((maintainer._index.root / "status.json").read_text(encoding="utf-8"))
    assert status["ok"] is True
    assert any("语义索引已更新" in message for message in fake_logger.infos)


def test_maintainer_backoff_bounds():
    from core.semantic_recall import (
        FAILURE_BASE_BACKOFF_SECONDS,
        FAILURE_MAX_BACKOFF_SECONDS,
        SemanticMaintainer,
    )

    maintainer = SemanticMaintainer(
        index=SemanticIndex(Path("/tmp/unused-semantic-index")),
        resolver=SemanticEmbeddingResolver(FakeContext([])),
        config_getter=lambda: PhoneConfig.from_mapping(None),
        data_dir=Path("/tmp/unused-semantic-data"),
    )
    maintainer._failures = 1
    assert maintainer._backoff_seconds() == FAILURE_BASE_BACKOFF_SECONDS
    maintainer._failures = 2
    assert maintainer._backoff_seconds() == FAILURE_BASE_BACKOFF_SECONDS * 2
    maintainer._failures = 100
    assert maintainer._backoff_seconds() == FAILURE_MAX_BACKOFF_SECONDS


def test_maintainer_loop_builds_on_event_and_stops(tmp_path):
    provider = FakeEmbeddingProvider(default_vector=[1.0, 0.0, 0.0])
    config = _recall_config(refresh_minutes=0)
    maintainer = _maintainer(tmp_path, provider, config, startup_delay=0.0, debounce=0.0)

    async def scenario():
        _write_learning(tmp_path, _learning_records(1))
        task = maintainer.start()
        assert task is not None
        assert task.get_name().startswith("astrbot_phone_control")
        assert await _wait_until(
            lambda: (maintainer._index.load_snapshot(allow_incompatible=True) or None) is not None
        )
        snapshot = maintainer._index.load_snapshot(allow_incompatible=True)
        assert snapshot is not None and snapshot.count == 1
        # refresh_minutes=0：只在事件触发时增量构建
        _write_learning(tmp_path, _learning_records(2))
        maintainer.request_refresh()
        assert await _wait_until(
            lambda: (
                maintainer._index.load_snapshot(allow_incompatible=True) is not None
                and maintainer._index.load_snapshot(allow_incompatible=True).count == 2
            )
        )
        await maintainer.stop()
        assert maintainer.running is False
        assert task.done()

    asyncio.run(scenario())


def test_maintainer_writes_empty_index_when_no_local_records(tmp_path):
    provider = FakeEmbeddingProvider()
    config = _recall_config()
    maintainer = _maintainer(tmp_path, provider, config)
    assert asyncio.run(maintainer.refresh()) is True
    snapshot = maintainer._index.load_snapshot(allow_incompatible=True)
    assert snapshot is not None
    assert snapshot.count == 0
    assert provider.batch_calls == []


def test_reader_supports_batch_only_provider(tmp_path):
    """Provider 只实现 get_embeddings 时，Reader 仍可查询（逐条包装）。"""

    class BatchOnlyProvider(FakeEmbeddingProvider):
        get_embedding = None  # 显式去掉单条接口

        async def get_embeddings(self, texts):
            self.batch_calls.append(list(texts))
            return [list(self.vectors.get(text, self.default_vector)) for text in texts]

    provider = BatchOnlyProvider(vectors={"甲": [1.0, 0.0, 0.0]})
    index, resolver, config, reader = _reader_fixture(tmp_path, provider)
    asyncio.run(
        build_index(
            index,
            [_doc("learning:a", "甲")],
            embed=provider.get_embeddings,
            provider_id="fake-embed",
            model="fake-model",
            dim=3,
        )
    )
    text = asyncio.run(reader.render("甲"))
    assert text.startswith(RECALL_HEADER)
    assert provider.batch_calls[-1] == ["甲"]
