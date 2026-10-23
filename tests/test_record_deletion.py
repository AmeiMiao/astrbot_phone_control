"""日志/学习删除的持久化、缓存与并发索引构建回归。"""

import asyncio
import json

import pytest

from core.diary import DiaryError, PhoneDiary
from core.learning import LearningStore
from core.semantic import SemanticDoc, SemanticIndex, build_index


def test_legacy_diary_ids_survive_delete_and_reload(tmp_path):
    diary = PhoneDiary(tmp_path)
    diary.path.write_text(
        "\n".join(
            json.dumps({"ts": 1, "app": "测试", "summary": str(i)}) for i in range(3)
        )
        + "\n"
    )
    entries = list(diary.all_entries())
    ids = [e["id"] for e in entries]
    assert len(set(ids)) == 3
    diary.delete(ids[1])
    assert [e["id"] for e in PhoneDiary(tmp_path).all_entries()] == [ids[0], ids[2]]
    before = diary.path.read_bytes()
    with pytest.raises(DiaryError, match="不存在"):
        diary.delete(ids[1])
    assert diary.path.read_bytes() == before


def test_diary_delete_refuses_corrupt_file(tmp_path):
    diary = PhoneDiary(tmp_path)
    diary.path.write_text('{"id":"one"}\ninvalid\n')
    before = diary.path.read_bytes()
    with pytest.raises(DiaryError, match="损坏"):
        diary.delete("one")
    assert diary.path.read_bytes() == before


def test_source_delete_invalidates_cached_snapshot_and_rebuild(tmp_path):
    async def scenario():
        index = SemanticIndex(tmp_path / "semantic")
        docs = [
            SemanticDoc("diary:a:0", "diary", "a", "一", "一", 1),
            SemanticDoc("diary:b:0", "diary", "b", "二", "二", 2),
        ]
        await build_index(
            index,
            docs,
            embed=lambda texts: [[1, 0] for _ in texts],
            provider_id="p",
            model="m",
            dim=2,
        )
        cached = index.load_snapshot()
        assert len(cached.search([1, 0], min_score=0)) == 2
        index.deletions.commit_source_delete("diary", "a", lambda: None)
        assert [h.doc.source_id for h in cached.search([1, 0])] == ["b"]
        assert index.load_snapshot().count == 1
        await build_index(
            index,
            docs,
            embed=lambda texts: [[1, 0] for _ in texts],
            provider_id="p",
            model="m",
            dim=2,
            force=True,
        )
        assert [
            d.source_id for d in SemanticIndex(index.root).load_snapshot().docs
        ] == ["b"]

    asyncio.run(scenario())


def test_source_delete_during_embedding_cannot_reappear(tmp_path):
    async def scenario():
        index = SemanticIndex(tmp_path / "semantic")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def embed(texts):
            entered.set()
            await release.wait()
            return [[1, 0] for _ in texts]

        docs = [SemanticDoc("learning:a", "learning", "a", "一", "一", 1)]
        task = asyncio.create_task(
            build_index(index, docs, embed=embed, provider_id="p", model="m", dim=2)
        )
        await entered.wait()
        index.deletions.commit_source_delete("learning", "a", lambda: None)
        release.set()
        result = await task
        assert result.count == 0
        assert index.load_snapshot().count == 0
        assert index.read_manifest()["count"] == 0

    asyncio.run(scenario())


def test_source_write_failure_preserves_source_and_index_rules(tmp_path, monkeypatch):
    diary = PhoneDiary(tmp_path / "diary")
    diary.path.parent.mkdir()
    diary.path.write_text('{"id":"one","summary":"data"}\n')
    index = SemanticIndex(tmp_path / "semantic")

    def fail(records):
        raise OSError("disk full")

    monkeypatch.setattr(diary, "_write_entries_unlocked", fail)
    with pytest.raises(OSError):
        diary.delete(
            "one",
            commit=lambda writer: index.deletions.commit_source_delete(
                "diary", "one", writer
            ),
        )
    assert len(list(diary.all_entries())) == 1
    assert index.deletions.read()["sources"] == []


def test_learning_delete_commits_index_marker_and_preserves_other_entries(tmp_path):
    store = LearningStore(tmp_path)
    first = store.record(content="第一条")
    second = store.record(content="第二条")
    index = SemanticIndex(tmp_path / "semantic")
    store.delete(
        first["id"],
        commit=lambda writer: index.deletions.commit_source_delete(
            "learning", first["id"], writer
        ),
    )
    assert [e["id"] for e in LearningStore(tmp_path).entries()] == [second["id"]]
    assert index.deletions.read()["sources"] == [["learning", first["id"]]]
