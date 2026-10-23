"""向量查看使用当前代一致快照，覆盖筛选、分页与损坏状态。"""

import asyncio
import math

from core.semantic import SemanticDoc, SemanticIndex, build_index
from core.vector_dashboard import vector_page


def _index(tmp_path):
    index = SemanticIndex(tmp_path / "semantic")
    docs = [SemanticDoc(
        id=f"doc-{i}", source="learning" if i % 2 else "app_experience",
        source_id=str(i), text=f"手机经验 {i}", display=f"经验 {i}", ts=i,
        meta={"app": "测试 App"},
    ) for i in range(25)]
    asyncio.run(build_index(index, docs, embed=lambda texts: [[3, 4] for _ in texts],
                            provider_id="test-provider", model="model", dim=2))
    return index


def test_vector_page_filters_and_paginates_current_generation(tmp_path):
    index = _index(tmp_path)
    page = vector_page(index, offset=0)
    assert page["state"] == "ready" and page["total"] == 25
    assert len(page["entries"]) == 20 and page["entries"][0]["id"] == "doc-24"
    assert page["manifest"]["dim"] == 2
    assert page["manifest"]["provider_id"] == "test-provider"
    entry = page["entries"][0]
    assert math.isclose(entry["vector"][0], .6, abs_tol=1e-6)
    assert math.isclose(entry["norm"], 1, abs_tol=1e-6)
    assert len(vector_page(index, offset=20)["entries"]) == 5
    assert vector_page(index, query="手机经验 24")["total"] == 1
    assert vector_page(index, source="learning")["total"] == 12
    assert vector_page(index, query="不存在")["entries"] == []
    assert vector_page(index, offset=100)["entries"] == []


def test_vector_page_distinguishes_missing_and_invalid_without_writes(tmp_path):
    index = SemanticIndex(tmp_path / "missing")
    assert vector_page(index)["state"] == "missing"
    assert not index.root.exists()
    index = _index(tmp_path)
    manifest = index.read_manifest()
    (index.root / manifest["vectors_file"]).write_bytes(b"bad")
    assert vector_page(index)["state"] == "invalid"
    assert vector_page(index)["entries"] == []
