"""图谱的真实余弦相似度、过滤范围和删除一致性。"""

import asyncio

import pytest

from core.semantic import SemanticDoc, SemanticIndex, build_index
from core.vector_dashboard import vector_graph


def make_index(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")
    docs = [SemanticDoc("a", "learning", "a", "主题A", "主题A", 3),
            SemanticDoc("b", "learning", "b", "主题B", "主题B", 2),
            SemanticDoc("c", "diary", "c", "主题C", "主题C", 1)]
    asyncio.run(build_index(index, docs, embed=lambda texts: [[1, 0], [.8, .6], [0, 1]], provider_id="p", model="m", dim=2))
    return index


def test_graph_uses_actual_similarity_and_filters(tmp_path):
    index = make_index(tmp_path)
    graph = vector_graph(index, min_score=.7)
    assert [n["id"] for n in graph["nodes"]] == ["a", "b", "c"]
    assert len(graph["edges"]) == 1
    edge = graph["edges"][0]
    assert edge["source"] == "a" and edge["target"] == "b"
    assert edge["score"] == pytest.approx(.8, abs=1e-6)
    assert not vector_graph(index, min_score=.9)["edges"]
    assert [n["id"] for n in vector_graph(index, source="diary")["nodes"]] == ["c"]
    assert [n["id"] for n in vector_graph(index, query="主题B")["nodes"]] == ["b"]
    limited = vector_graph(index, limit=1)
    assert len(limited["nodes"]) == 1 and limited["truncated"] and limited["total"] == 3


def test_graph_excludes_deleted_nodes_and_edges(tmp_path):
    index = make_index(tmp_path)
    index.delete_document("b")
    graph = vector_graph(index, min_score=.7)
    assert {node["id"] for node in graph["nodes"]} == {"a", "c"}
    assert graph["edges"] == []
    assert vector_graph(index, query="无结果")["nodes"] == []


@pytest.mark.parametrize("threshold", ["nan", "inf", -.1, 1.1])
def test_graph_rejects_invalid_threshold(tmp_path, threshold):
    with pytest.raises(ValueError):
        vector_graph(SemanticIndex(tmp_path), min_score=threshold)


def test_graph_missing_state_does_not_write(tmp_path):
    root = tmp_path / "none"
    assert vector_graph(SemanticIndex(root))["state"] == "missing"
    assert not root.exists()
