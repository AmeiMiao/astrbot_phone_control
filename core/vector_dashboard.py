"""学习与 App 操控经验向量的只读分页快照。"""

from collections import Counter
from math import sqrt

from .semantic import SemanticIndex


def vector_page(index: SemanticIndex, *, query="", source="", offset=0, limit=20):
    """读取一致的当前代向量；查看不会创建索引或调用模型。"""
    query = str(query or "").strip()[:100]
    offset = max(0, int(offset))
    limit = max(1, min(50, int(limit)))
    snapshot = index.load_snapshot()
    result = {
        "state": "missing", "query": query, "source": source,
        "offset": offset, "limit": limit, "total": 0,
        "manifest": {}, "sources": {}, "entries": [],
    }
    if snapshot is None:
        if index.manifest_path.exists():
            result["state"] = "invalid"
        return result
    result.update(
        state="ready", manifest={k: snapshot.manifest.get(k) for k in (
            "provider_id", "model", "dim", "count", "updated_at", "generation",
        )}, sources=dict(Counter(d.source for d in snapshot.docs)),
    )
    docs = [d for d in snapshot.docs if (not source or d.source == source) and (
        not query or query.casefold() in f"{d.id} {d.text} {d.display} {d.meta}".casefold()
    )]
    docs.sort(key=lambda d: (d.ts, d.id), reverse=True)
    result["total"] = len(docs)
    for doc in docs[offset:offset + limit]:
        vector = snapshot.vector_of(doc.id)
        result["entries"].append({
            "id": doc.id, "source": doc.source, "source_id": doc.source_id,
            "text": doc.text, "display": doc.display, "ts": doc.ts,
            "meta": doc.meta, "dim": snapshot.dim, "vector": vector,
            "norm": sqrt(sum(v * v for v in vector)),
        })
    return result
