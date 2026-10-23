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


def vector_graph(index, *, query="", source="", limit=80, min_score=0.45):
    """展示同一向量空间内的近邻相似关系，每个节点最多选择三个近邻。"""
    import math

    count = max(1, min(150, int(limit)))
    threshold = float(min_score)
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("相似度阈值需要是 0 到 1 之间的数值")
    snapshot = index.load_snapshot()
    result = {"state": "missing", "manifest": {}, "nodes": [], "edges": [],
              "total": 0, "limit": count, "min_score": threshold, "truncated": False}
    if snapshot is None:
        result["state"] = "invalid" if index.manifest_path.exists() else "missing"
        return result
    result.update(state="ready", manifest={k: snapshot.manifest.get(k) for k in (
        "provider_id", "model", "dim", "count", "updated_at", "generation",
    )})
    needle = str(query or "").strip()[:100].casefold()
    matched = [doc for doc in snapshot.docs if (not source or source == doc.source) and (
        not needle or needle in f"{doc.id} {doc.text} {doc.display} {doc.meta}".casefold()
    )]
    matched.sort(key=lambda doc: (doc.ts, doc.id), reverse=True)
    result["total"] = len(matched)
    result["truncated"] = len(matched) > count
    docs = matched[:count]
    result["nodes"] = [{"id": d.id, "label": d.display, "text": d.text, "source": d.source,
                         "source_id": d.source_id, "ts": d.ts, "meta": d.meta} for d in docs]
    vectors = [snapshot.vector_of(doc.id) for doc in docs]
    neighbors = [[] for _ in docs]
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            score = max(-1.0, min(1.0, sum(a * b for a, b in zip(vectors[i], vectors[j]))))
            if score >= threshold:
                neighbors[i].append((score, j)); neighbors[j].append((score, i))
    edges = {}
    for i, matches in enumerate(neighbors):
        for score, j in sorted(matches, key=lambda pair: (-pair[0], docs[pair[1]].id))[:3]:
            key = (min(i, j), max(i, j))
            edges[key] = {"source": docs[key[0]].id, "target": docs[key[1]].id, "score": score}
    result["edges"] = list(edges.values())
    return result
