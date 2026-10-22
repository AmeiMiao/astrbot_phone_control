"""聊天语义找回的本地向量索引（纯标准库，不依赖 AstrBot）。

索引目录结构（``<plugin_data>/semantic/``）::

    manifest.json            # 当前 generation 指针与索引元数据
    entries.<gen>.jsonl      # 文档元数据，一行一条
    vectors.<gen>.f32        # float32 小端向量，按 entries 顺序拼接并已 L2 归一化
    status.json              # 最近一次构建状态（构建方写入，便于排查）

设计要点：

* **读写分离**：普通聊天只读 ``manifest/entries/vectors`` 并计算一次查询向量；
  构建由后台任务调用本模块的 :func:`build_index`，读取本地档案、分批 embedding；
* **原子切换**：先写 ``*.tmp`` 再 ``os.replace`` 成 generation 文件，最后替换
  ``manifest.json`` 这一个指针；任一步失败都保留旧索引继续可用；
* **增量复用**：文档 hash 含 provider 标识与 embedding 正文，只对新增/修改文档
  调用 embedding；被删除文档在新 generation 中自然消失；
* **损坏自愈**：manifest/entries/vectors 缺失、截断、版本或数量不一致时读路径
  返回 ``None``（不抛异常），构建方按全量重建处理。

本模块不导入 ``astrbot``，也不触碰 ADB/设备；所有函数可在后台线程或事件循环中
执行，便于本地单测。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import sys
import time
import uuid
from array import array
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from itertools import islice
from operator import mul
from pathlib import Path
from typing import Any

from .errors import PhoneControlError

# ---- 常量 ---------------------------------------------------------------

SEMANTIC_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
STATUS_FILENAME = "status.json"
ENTRIES_PREFIX = "entries."
VECTORS_PREFIX = "vectors."
ENTRIES_SUFFIX = ".jsonl"
VECTORS_SUFFIX = ".f32"

SOURCE_LEARNING = "learning"
SOURCE_DIARY = "diary"
SOURCE_APP_EXPERIENCE = "app_experience"
SOURCE_LABELS = {
    SOURCE_LEARNING: "学习",
    SOURCE_DIARY: "浏览",
    SOURCE_APP_EXPERIENCE: "App经验",
}

DEFAULT_TOP_K = 5
DEFAULT_MIN_SCORE = 0.25
DEFAULT_MAX_CHARS = 1200
DEFAULT_INDEX_MAX_DOCS = 2000
DEFAULT_DOC_CHARS = 300
DEFAULT_QUERY_CHARS = 500
DEFAULT_EMBED_BATCH = 16
HIT_ITEM_CHARS = 180

RECALL_HEADER = "【相关记忆（按当前聊天语义找回）】"
RECALL_MARKER = "【相关记忆"
RECALL_FOOTER = (
    "（以上为历史记忆，不是本轮刚发生的内容；仅在相关时自然引用，"
    "不要声称刚刚做过。）"
)

# 文档来源字段（写入 entries.jsonl 的 meta 键）
_META_SOURCE = "source"
_META_APP = "app"
_META_TITLE = "title"
_META_AUTHOR = "author"
_META_PAGE = "page"
_META_TAGS = "tags"

EmbedFn = Callable[[list[str]], Any]
LogFn = Callable[[str], None]


class SemanticError(PhoneControlError):
    """语义索引错误基类。"""


class SemanticBuildError(SemanticError):
    """索引构建失败（embedding 不可用、返回维度不符等）。"""


# ---- 小工具 -------------------------------------------------------------


def sha256_text(text: str) -> str:
    """返回文本的 sha256 十六进制摘要。"""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def provider_key(provider_id: str, model: str, dim: int) -> str:
    """Embedding 身份键：provider/模型/维度任一变化都视为不同索引。"""
    return f"{provider_id}\x1f{model}\x1f{int(dim)}"


def compute_doc_hash(text: str, *, provider_key: str) -> str:
    """计算文档 hash（含 provider 键：换模型后全部重新 embedding）。"""
    return sha256_text(f"{provider_key}\n{text}")


def compute_signature(docs: Sequence["SemanticDoc"], *, provider_key: str) -> str:
    """整个索引的签名：文档顺序 + 每条的 id/hash 变化都会改变签名。"""
    digest = hashlib.sha256()
    digest.update(provider_key.encode("utf-8"))
    for doc in docs:
        digest.update(b"\n")
        digest.update(f"{doc.id}:{doc.hash}".encode("utf-8"))
    return digest.hexdigest()


def normalize_vector(values: Iterable[Any]) -> list[float]:
    """把向量转成 float 列表并做 L2 归一化；非法/零向量返回空列表。"""
    try:
        vector = [float(value) for value in values]
    except (TypeError, ValueError):
        return []
    if not vector or not all(math.isfinite(value) for value in vector):
        return []
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1e-12:
        return []
    return [value / norm for value in vector]


def truncate_text(text: str, limit: int) -> str:
    """把文本截断到 ``limit`` 字符（含省略号），limit<=0 时返回空串。"""
    value = str(text or "")
    try:
        max_chars = int(limit)
    except (TypeError, ValueError):
        max_chars = 0
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    if max_chars == 1:
        return value[:1]
    return value[: max_chars - 1] + "…"


def _clean_text(value: Any) -> str:
    """压缩空白并转成字符串（不截断）。"""
    return " ".join(str(value or "").split())


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _join_parts(parts: Iterable[Any], *, sep: str = "｜") -> str:
    return sep.join(item for item in (str(part).strip() for part in parts) if item)


# ---- 文档与检索结果 ------------------------------------------------------


@dataclass(frozen=True)
class SemanticDoc:
    """一条可召回的记忆文档。"""

    id: str
    source: str
    source_id: str
    text: str
    """供 embedding 的文本（含来源/标题线索，<= doc_chars）。"""
    display: str
    """供注入展示的文本（<= doc_chars）。"""
    ts: float
    meta: dict[str, Any] = field(default_factory=dict)
    hash: str = ""
    """文档 hash（含 provider 键），构建时写入。"""

    def with_hash(self, *, provider_key: str) -> "SemanticDoc":
        """返回带 hash 的副本（增量比较用）。"""
        return replace(self, hash=compute_doc_hash(self.text, provider_key=provider_key))


@dataclass(frozen=True)
class SemanticHit:
    """一次检索命中的文档与余弦相似度。"""

    doc: SemanticDoc
    score: float


@dataclass(frozen=True)
class BuildResult:
    """一次索引构建/刷新结果。"""

    generation: str
    count: int
    embedded: int
    reused: int
    changed: bool
    signature: str
    sources: dict[str, int]


# ---- 文档构建（纯函数，输入为已解析的本地记录） --------------------------


def build_learning_documents(
    entries: Iterable[dict[str, Any]], *, doc_chars: int = DEFAULT_DOC_CHARS
) -> list[SemanticDoc]:
    """把学习档案记录（``learning.jsonl``）转成索引文档。"""
    docs: list[SemanticDoc] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        content = _clean_text(raw.get("content"))
        if not content:
            continue
        ts = _as_float(raw.get("ts"))
        source = _clean_text(raw.get("source"))
        tags = [_clean_text(tag) for tag in raw.get("tags") or [] if _clean_text(tag)]
        raw_id = raw.get("id")
        source_id = str(raw_id) if isinstance(raw_id, str) and raw_id else sha256_text(
            f"{ts}:{content}"
        )[:16]
        head = _join_parts((source, " ".join(f"#{tag}" for tag in tags)))
        text = truncate_text(f"{head}\n{content}" if head else content, doc_chars)
        docs.append(
            SemanticDoc(
                id=f"{SOURCE_LEARNING}:{source_id}",
                source=SOURCE_LEARNING,
                source_id=source_id,
                text=text,
                display=truncate_text(content, doc_chars),
                ts=ts,
                meta={_META_SOURCE: source, _META_TAGS: tags},
            )
        )
    return docs


def build_diary_documents(
    entries: Iterable[dict[str, Any]], *, doc_chars: int = DEFAULT_DOC_CHARS
) -> list[SemanticDoc]:
    """把浏览日记条目（``diary/diary.jsonl``）按内容条目展开成索引文档。"""
    docs: list[SemanticDoc] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        ts = _as_float(raw.get("ts"))
        app = _clean_text(raw.get("app")) or "未知 App"
        entry_id = str(raw.get("id") or "") or sha256_text(f"{ts}:{app}")[:16]
        note = _clean_text(raw.get("note"))
        summary = _clean_text(raw.get("summary"))
        items = raw.get("items")
        item_list = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        if not item_list:
            body = _join_parts((summary, note), sep="\n")
            if not body:
                continue
            docs.append(
                SemanticDoc(
                    id=f"{SOURCE_DIARY}:{entry_id}:summary",
                    source=SOURCE_DIARY,
                    source_id=entry_id,
                    text=truncate_text(f"{app}\n{body}", doc_chars),
                    display=truncate_text(f"{app}：{summary or note}", doc_chars),
                    ts=ts,
                    meta={_META_APP: app},
                )
            )
            continue
        extra = _join_parts((summary, f"备注：{note}" if note else ""), sep="\n")
        for index, item in enumerate(item_list):
            title = _clean_text(item.get("title"))
            author = _clean_text(item.get("author"))
            body = _clean_text(item.get("text"))
            if not (title or body):
                continue
            embedding_text = _join_parts((app, title, author), sep="｜")
            embedding_text = f"{embedding_text}\n{body}" if body else embedding_text
            if extra:
                embedding_text = f"{embedding_text}\n{extra}"
            display = f"{app}：{title}" if title else f"{app}：{body}"
            if author:
                display += f"（{author}）"
            docs.append(
                SemanticDoc(
                    id=f"{SOURCE_DIARY}:{entry_id}:{index}",
                    source=SOURCE_DIARY,
                    source_id=entry_id,
                    text=truncate_text(embedding_text, doc_chars),
                    display=truncate_text(display, doc_chars),
                    ts=ts,
                    meta={_META_APP: app, _META_TITLE: title, _META_AUTHOR: author},
                )
            )
    return docs


def build_app_memory_documents(
    entries: Iterable[dict[str, Any]], *, doc_chars: int = DEFAULT_DOC_CHARS
) -> list[SemanticDoc]:
    """把 App 使用经验（``app_memory.json`` 的 entries）转成索引文档。"""
    docs: list[SemanticDoc] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        app = _clean_text(raw.get("app"))
        note = _clean_text(raw.get("note"))
        if not app or not note:
            continue
        label = _clean_text(raw.get("label")) or app
        page = _clean_text(raw.get("page"))
        steps = [_clean_text(step) for step in raw.get("steps") or [] if _clean_text(step)]
        ts = _as_float(raw.get("updated_at"))
        head = f"{label} / {page}" if page else label
        text = f"{head}\n{note}"
        if steps:
            text += "\n步骤：" + " → ".join(steps)
        display = f"{head}：{note}"
        docs.append(
            SemanticDoc(
                id=f"{SOURCE_APP_EXPERIENCE}:{app}:{page or '_'}",
                source=SOURCE_APP_EXPERIENCE,
                source_id=f"{app}:{page or '_'}",
                text=truncate_text(text, doc_chars),
                display=truncate_text(display, doc_chars),
                ts=ts,
                meta={_META_APP: label, _META_PAGE: page, _META_SOURCE: app},
            )
        )
    return docs


# ---- 本地档案读取（只读文件，不碰设备） ----------------------------------


def read_jsonl_entries(path: str | Path) -> list[dict[str, Any]]:
    """读取 JSONL 档案；文件不存在/不可读/坏行都跳过（索引可重建，不报错）。"""
    target = Path(path)
    entries: list[dict[str, Any]] = []
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    entries.append(record)
    except OSError:
        return []
    return entries


def read_app_memory_entries(path: str | Path) -> list[dict[str, Any]]:
    """读取插件本地 App 经验档案；格式非法/文件不存在时返回空列表。"""
    target = Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    entries = raw.get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def collect_documents(
    data_dir: str | Path,
    *,
    include_learning: bool = True,
    include_diary: bool = True,
    include_app_experience: bool = True,
    doc_chars: int = DEFAULT_DOC_CHARS,
    index_max_docs: int = DEFAULT_INDEX_MAX_DOCS,
) -> list[SemanticDoc]:
    """从插件本地档案收集索引文档（按 id 去重、按时间倒序、按上限截断）。

    Args:
        data_dir: 插件数据目录（``learning.jsonl``、``diary/diary.jsonl``、
            ``app_memory.json`` 所在目录）。
        include_learning: 是否索引学习档案。
        include_diary: 是否索引浏览日记。
        include_app_experience: 是否索引插件本地 App 经验（知识库后端不经由
            本函数读取；由调用方决定是否传入该项）。
        doc_chars: 单条文档字符上限。
        index_max_docs: 最多保留文档数（超出按时间淘汰最旧文档，<=0 表示不截断）。

    Returns:
        文档列表（时间新→旧）；没有任何本地档案时返回空列表。
    """
    root = Path(data_dir)
    docs: list[SemanticDoc] = []
    if include_learning:
        docs.extend(
            build_learning_documents(
                read_jsonl_entries(root / "learning.jsonl"), doc_chars=doc_chars
            )
        )
    if include_diary:
        docs.extend(
            build_diary_documents(
                read_jsonl_entries(root / "diary" / "diary.jsonl"),
                doc_chars=doc_chars,
            )
        )
    if include_app_experience:
        docs.extend(
            build_app_memory_documents(
                read_app_memory_entries(root / "app_memory.json"),
                doc_chars=doc_chars,
            )
        )
    # 同 id 只保留一条（同来源覆盖更新）；再按时间倒序截断，避免无限增长。
    newest: dict[str, SemanticDoc] = {}
    for doc in docs:
        previous = newest.get(doc.id)
        if previous is None or doc.ts >= previous.ts:
            newest[doc.id] = doc
    ordered = sorted(newest.values(), key=lambda item: (item.ts, item.id), reverse=True)
    try:
        limit = int(index_max_docs)
    except (TypeError, ValueError):
        limit = DEFAULT_INDEX_MAX_DOCS
    if limit > 0:
        ordered = ordered[:limit]
    return ordered


# ---- 索引文件读写 --------------------------------------------------------


def _doc_to_dict(doc: SemanticDoc, *, offset: int, dim: int) -> dict[str, Any]:
    return {
        "id": doc.id,
        "source": doc.source,
        "source_id": doc.source_id,
        "text": doc.text,
        "display": doc.display,
        "ts": doc.ts,
        "meta": doc.meta,
        "hash": doc.hash,
        "offset": int(offset),
        "dim": int(dim),
    }


def _doc_from_dict(raw: Any) -> SemanticDoc | None:
    if not isinstance(raw, dict):
        return None
    doc_id = raw.get("id")
    source = raw.get("source")
    text = raw.get("text")
    if not isinstance(doc_id, str) or not doc_id:
        return None
    if not isinstance(source, str) or not source:
        return None
    if not isinstance(text, str) or not text:
        return None
    display = raw.get("display")
    meta = raw.get("meta")
    return SemanticDoc(
        id=doc_id,
        source=source,
        source_id=str(raw.get("source_id") or ""),
        text=text,
        display=display if isinstance(display, str) and display else text,
        ts=_as_float(raw.get("ts")),
        meta=meta if isinstance(meta, dict) else {},
        hash=str(raw.get("hash") or ""),
    )


def _vectors_to_bytes(vectors: Sequence[Sequence[float]]) -> bytes:
    """把向量按 float32 小端拼接成字节串（向量已 L2 归一化）。"""
    data = array("f")
    for vector in vectors:
        data.extend(float(value) for value in vector)
    if sys.byteorder == "big":  # pragma: no cover - 小端宿主为常态
        data.byteswap()
    return data.tobytes()


def _vectors_from_bytes(raw: bytes, *, count: int, dim: int) -> array | None:
    """把 float32 小端字节串解析成数组；长度不符时返回 None。"""
    if count < 0 or dim <= 0:
        return None
    expected = count * dim * 4
    if len(raw) != expected:
        return None
    data = array("f")
    try:
        data.frombytes(raw)
    except ValueError:  # pragma: no cover - 长度已校验，防御性兜底
        return None
    if sys.byteorder == "big":  # pragma: no cover - 小端宿主为常态
        data.byteswap()
    return data


@dataclass(frozen=True)
class SemanticSnapshot:
    """某一代索引的只读快照（文档 + 归一化向量 + manifest 元数据）。"""

    manifest: dict[str, Any]
    docs: tuple[SemanticDoc, ...]
    vectors: array
    dim: int
    provider_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_id", {doc.id: doc for doc in self.docs})
        object.__setattr__(
            self, "_offset_by_id", {doc.id: index for index, doc in enumerate(self.docs)}
        )

    @property
    def generation(self) -> str:
        return str(self.manifest.get("generation") or "")

    @property
    def count(self) -> int:
        return len(self.docs)

    @property
    def signature(self) -> str:
        return str(self.manifest.get("signature") or "")

    def doc_of(self, doc_id: str) -> SemanticDoc | None:
        return self._by_id.get(doc_id)  # type: ignore[attr-defined]

    def vector_of(self, doc_id: str) -> list[float] | None:
        index = self._offset_by_id.get(doc_id)  # type: ignore[attr-defined]
        if index is None:
            return None
        start = index * self.dim
        return list(self.vectors[start : start + self.dim])

    def search(
        self,
        query_vector: Sequence[float],
        *,
        top_k: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
    ) -> list[SemanticHit]:
        """按余弦相似度检索：向量已归一化，点积即余弦相似度。"""
        try:
            limit = int(top_k)
        except (TypeError, ValueError):
            limit = DEFAULT_TOP_K
        if limit <= 0 or not self.docs:
            return []
        query = normalize_vector(query_vector)
        if not query or len(query) != self.dim:
            return []
        try:
            threshold = float(min_score)
        except (TypeError, ValueError):
            threshold = DEFAULT_MIN_SCORE
        scored: list[tuple[float, int]] = []
        vectors = self.vectors
        for index in range(len(self.docs)):
            start = index * self.dim
            score = sum(
                map(mul, query, islice(vectors, start, start + self.dim))
            )
            if score >= threshold:
                scored.append((float(score), index))
        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        hits: list[SemanticHit] = []
        seen_hashes: set[str] = set()
        for score, index in scored:
            doc = self.docs[index]
            key = doc.hash or doc.id
            if key in seen_hashes:
                continue
            seen_hashes.add(key)
            hits.append(SemanticHit(doc=doc, score=score))
            if len(hits) >= limit:
                break
        return hits


class SemanticIndex:
    """``<plugin_data>/semantic/`` 索引目录的读写与原子切换。"""

    def __init__(self, root: str | Path) -> None:
        """初始化索引目录（不创建，写入时才建目录）。

        Args:
            root: 索引目录（``<plugin_data>/semantic``）。
        """
        self.root = Path(root)

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_FILENAME

    @property
    def status_path(self) -> Path:
        return self.root / STATUS_FILENAME

    def read_manifest(self) -> dict[str, Any] | None:
        """读取 manifest；不存在或 JSON 非法时返回 None（视为无索引）。"""
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def has_generation(self, manifest: dict[str, Any] | None) -> bool:
        """manifest 指向的 entries/vectors 文件是否都存在。"""
        if not isinstance(manifest, dict):
            return False
        entries = manifest.get("entries_file")
        vectors = manifest.get("vectors_file")
        if not isinstance(entries, str) or not isinstance(vectors, str):
            return False
        return (self.root / entries).is_file() and (self.root / vectors).is_file()

    def load_snapshot(
        self,
        *,
        provider_key: str | None = None,
        allow_incompatible: bool = False,
    ) -> SemanticSnapshot | None:
        """加载当前索引快照；缺失/损坏/不兼容时返回 None 而不抛异常。

        Args:
            provider_key: 调用方的 Embedding 身份键；索引与此不一致时返回 None
                （避免拿另一个模型的向量算相似度）。
            allow_incompatible: 为 True 时忽略 provider 不一致（增量复用旧向量时
                仍会自行校验文档 hash）。
        """
        manifest = self.read_manifest()
        if manifest is None:
            return None
        try:
            if int(manifest.get("version") or 0) != SEMANTIC_VERSION:
                return None
            count = int(manifest.get("count"))
            dim = int(manifest.get("dim"))
            stored_provider_key = str(manifest.get("provider_key") or "")
            generation = str(manifest.get("generation") or "")
            if count < 0 or dim <= 0 or not generation:
                return None
            if (
                provider_key is not None
                and stored_provider_key != provider_key
                and not allow_incompatible
            ):
                return None
            entries_name = str(manifest.get("entries_file") or "")
            vectors_name = str(manifest.get("vectors_file") or "")
            for name in (entries_name, vectors_name):
                if not name or Path(name).name != name or name in {".", ".."}:
                    return None
            entries_path = self.root / entries_name
            vectors_path = self.root / vectors_name
            docs: list[SemanticDoc] = []
            try:
                with entries_path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            return None
                        doc = _doc_from_dict(raw)
                        if doc is None:
                            return None
                        docs.append(doc)
            except OSError:
                return None
            if len(docs) != count:
                return None
            vectors_raw = vectors_path.read_bytes()
            vectors = _vectors_from_bytes(vectors_raw, count=count, dim=dim)
            if vectors is None:
                return None
        except (OSError, TypeError, ValueError):
            return None
        return SemanticSnapshot(
            manifest=manifest,
            docs=tuple(docs),
            vectors=vectors,
            dim=dim,
            provider_key=stored_provider_key,
        )

    def write_status(self, payload: dict[str, Any]) -> None:
        """原子写 ``status.json``（可观测用途）；失败静默忽略。"""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.status_path.with_name(self.status_path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.status_path)
        except (OSError, TypeError, ValueError):
            return

    def write_generation(
        self,
        generation: str,
        docs: Sequence[SemanticDoc],
        vectors: Sequence[Sequence[float]],
        manifest: dict[str, Any],
    ) -> None:
        """原子写入一代索引，最后一步替换 manifest 指针。

        Args:
            generation: generation 标识（``<秒>-<随机>``）。
            docs: 文档列表（顺序与 ``vectors`` 一致）。
            vectors: 已归一化向量列表。
            manifest: 完整 manifest（由构建方提供 generation/count/dim 等字段）。

        Raises:
            OSError: 磁盘写入失败；此时旧 manifest 保持不变，旧索引继续可用。
        """
        entries_name = f"{ENTRIES_PREFIX}{generation}{ENTRIES_SUFFIX}"
        vectors_name = f"{VECTORS_PREFIX}{generation}{VECTORS_SUFFIX}"
        entries_path = self.root / entries_name
        vectors_path = self.root / vectors_name
        self.root.mkdir(parents=True, exist_ok=True)
        entries_tmp = entries_path.with_name(entries_path.name + ".tmp")
        vectors_tmp = vectors_path.with_name(vectors_path.name + ".tmp")
        manifest_tmp = self.manifest_path.with_name(self.manifest_path.name + ".tmp")
        doc_dim = len(vectors[0]) if vectors else int(manifest.get("dim") or 0)
        with entries_tmp.open("w", encoding="utf-8", newline="\n") as handle:
            offset = 0
            for doc in docs:
                handle.write(
                    json.dumps(
                        _doc_to_dict(doc, offset=offset, dim=doc_dim),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                offset += doc_dim
        vectors_tmp.write_bytes(_vectors_to_bytes(vectors))
        payload = dict(manifest)
        payload.update(
            {
                "version": SEMANTIC_VERSION,
                "generation": generation,
                "count": len(docs),
                "dim": doc_dim,
                "entries_file": entries_name,
                "vectors_file": vectors_name,
            }
        )
        manifest_tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        os.replace(entries_tmp, entries_path)
        os.replace(vectors_tmp, vectors_path)
        os.replace(manifest_tmp, self.manifest_path)

    def cleanup(self, *, keep_generations: set[str] | None = None) -> None:
        """清理旧 generation 与残留临时文件（保留传入的 generation）。"""
        if not self.root.is_dir():
            return
        keep = {name for name in (keep_generations or set()) if name}
        manifest = self.read_manifest()
        if manifest is not None:
            keep.add(str(manifest.get("generation") or ""))
        for path in self.root.iterdir():
            try:
                if not path.is_file():
                    continue
                name = path.name
                if name.endswith(".tmp"):
                    path.unlink(missing_ok=True)
                    continue
                if name.startswith(ENTRIES_PREFIX) and name.endswith(ENTRIES_SUFFIX):
                    generation = name[len(ENTRIES_PREFIX) : -len(ENTRIES_SUFFIX)]
                elif name.startswith(VECTORS_PREFIX) and name.endswith(VECTORS_SUFFIX):
                    generation = name[len(VECTORS_PREFIX) : -len(VECTORS_SUFFIX)]
                else:
                    continue
                if generation not in keep:
                    path.unlink(missing_ok=True)
            except OSError:
                continue


# ---- 构建 ---------------------------------------------------------------


def _call_embed(embed: EmbedFn, texts: list[str]) -> Any:
    """调用 embedding 函数；同步函数直接返回，异步函数返回 awaitable。"""
    return embed(list(texts))


async def _embed_batches(
    embed: EmbedFn,
    texts: list[str],
    *,
    batch_size: int,
) -> list[list[float]]:
    """分批调用 embedding（兼容同步/异步实现）。"""
    size = max(1, int(batch_size))
    vectors: list[list[float]] = []
    for start in range(0, len(texts), size):
        batch = texts[start : start + size]
        result = _call_embed(embed, batch)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, (list, tuple)) or len(result) != len(batch):
            raise SemanticBuildError(
                f"embedding 返回数量不符：期望 {len(batch)}，实际 "
                f"{len(result) if isinstance(result, (list, tuple)) else type(result).__name__}"
            )
        for vector in result:
            normalized = normalize_vector(vector)
            if not normalized:
                raise SemanticBuildError("embedding 返回空向量或含非法数值")
            vectors.append(normalized)
    return vectors


async def build_index(
    index: SemanticIndex,
    docs: Sequence[SemanticDoc],
    *,
    embed: EmbedFn,
    provider_id: str,
    model: str,
    dim: int,
    doc_chars: int = DEFAULT_DOC_CHARS,
    index_max_docs: int = DEFAULT_INDEX_MAX_DOCS,
    batch_size: int = DEFAULT_EMBED_BATCH,
    force: bool = False,
    clock: Callable[[], float] = time.time,
) -> BuildResult:
    """构建/增量刷新一代索引（可在后台任务中执行）。

    只对新增/修改文档调用 ``embed``；未变化文档复用上一代（同 provider）向量。
    文档 hash 与签名都未变化且未指定 ``force`` 时不写盘。

    Args:
        index: 索引目录。
        docs: 最终文档列表（已去重/截断/排序）。
        embed: 批量 embedding 函数（同步或异步；输入文本列表，返回向量列表）。
        provider_id: Embedding Provider ID。
        model: 模型名。
        dim: 向量维度（需与 embedding 返回一致）。
        doc_chars: 单条文档字符上限（仅记录到 manifest）。
        index_max_docs: 索引文档上限（仅记录到 manifest）。
        batch_size: 每批 embedding 的文本数。
        force: 为 True 时即使签名一致也重写 generation（可复用未变向量）。
        clock: 时间戳函数（测试可注入）。

    Returns:
        :class:`BuildResult`。

    Raises:
        SemanticBuildError: embedding 失败或返回维度不符。
        OSError: 索引文件写入失败（旧索引保持可用）。
    """
    try:
        expected_dim = int(dim)
    except (TypeError, ValueError) as exc:
        raise SemanticBuildError("向量维度非法") from exc
    if expected_dim <= 0:
        raise SemanticBuildError("向量维度非法")
    key = provider_key(provider_id, model, expected_dim)
    prepared = [doc.with_hash(provider_key=key) for doc in docs]
    signature = compute_signature(prepared, provider_key=key)
    sources: dict[str, int] = {}
    for doc in prepared:
        sources[doc.source] = sources.get(doc.source, 0) + 1

    previous: SemanticSnapshot | None = None
    if not force:
        try:
            previous = await asyncio.to_thread(
                index.load_snapshot, allow_incompatible=True
            )
        except Exception:  # noqa: BLE001 - 旧索引损坏时按全量重建
            previous = None
    same_provider = previous is not None and previous.provider_key == key
    if (
        previous is not None
        and same_provider
        and previous.signature == signature
        and int(previous.manifest.get("doc_chars") or 0) == int(doc_chars)
        and int(previous.manifest.get("index_max_docs") or 0) == int(index_max_docs)
        and index.has_generation(previous.manifest)
    ):
        return BuildResult(
            generation=previous.generation,
            count=len(prepared),
            embedded=0,
            reused=len(prepared),
            changed=False,
            signature=signature,
            sources=sources,
        )

    vectors: list[list[float] | None] = [None] * len(prepared)
    changed_indexes: list[int] = []
    for position, doc in enumerate(prepared):
        old_doc = previous.doc_of(doc.id) if same_provider and previous else None
        if old_doc is not None and old_doc.hash == doc.hash:
            vector = previous.vector_of(doc.id) if previous else None
            if vector is not None:
                vectors[position] = vector
                continue
        changed_indexes.append(position)

    embedded_texts = [prepared[position].text for position in changed_indexes]
    embedded_vectors = await _embed_batches(
        embed, embedded_texts, batch_size=batch_size
    )
    for position, vector in zip(changed_indexes, embedded_vectors):
        if len(vector) != expected_dim:
            raise SemanticBuildError(
                f"embedding 维度不符：期望 {expected_dim}，实际 {len(vector)}"
            )
        vectors[position] = vector
    if any(vector is None for vector in vectors):
        raise SemanticBuildError("向量数量与文档数量不一致")
    filled = [vector for vector in vectors if vector is not None]

    generation = f"{int(clock())}-{uuid.uuid4().hex[:8]}"
    now = float(clock())
    manifest = {
        "version": SEMANTIC_VERSION,
        "generation": generation,
        "created_at": now,
        "updated_at": now,
        "provider_id": provider_id,
        "model": model,
        "dim": expected_dim,
        "provider_key": key,
        "count": len(prepared),
        "doc_chars": int(doc_chars),
        "index_max_docs": int(index_max_docs),
        "sources": sources,
        "signature": signature,
    }
    if previous is not None:
        manifest["replaced_generation"] = previous.generation
    await asyncio.to_thread(
        index.write_generation, generation, prepared, filled, manifest
    )
    keep = {generation}
    if previous is not None and previous.generation:
        keep.add(previous.generation)
    try:
        index.cleanup(keep_generations=keep)
    except OSError:  # pragma: no cover - 清理失败不影响新索引
        pass
    return BuildResult(
        generation=generation,
        count=len(prepared),
        embedded=len(changed_indexes),
        reused=len(prepared) - len(changed_indexes),
        changed=True,
        signature=signature,
        sources=sources,
    )


# ---- 注入文本渲染 --------------------------------------------------------


def _format_hit_line(doc: SemanticDoc) -> str:
    label = SOURCE_LABELS.get(doc.source, doc.source or "记忆")
    when = ""
    try:
        if float(doc.ts) > 0:
            when = time.strftime("%m-%d %H:%M", time.localtime(float(doc.ts)))
    except (TypeError, ValueError, OSError):
        when = ""
    head = f"[{label}｜{when}]" if when else f"[{label}]"
    display = truncate_text(_clean_text(doc.display), HIT_ITEM_CHARS)
    if not display:
        display = truncate_text(_clean_text(doc.text), HIT_ITEM_CHARS)
    if not display:
        return ""
    line = f"- {head} {display}"
    source = _clean_text(doc.meta.get(_META_SOURCE))
    if doc.source == SOURCE_LEARNING and source and source not in display:
        line += f"（来源：{source}）"
    return line


def format_hits(
    hits: Sequence[SemanticHit], *, max_chars: int = DEFAULT_MAX_CHARS
) -> str:
    """把检索结果渲染成中文注入文本；无命中或放不下时返回空串。

    Args:
        hits: 检索命中的文档（按相关度倒序）。
        max_chars: 整段注入文本字符上限（含标题与结尾提示）。

    Returns:
        以「【相关记忆（按当前聊天语义找回）】」开头的多行文本；为空串表示本轮
        不注入（无命中/超出上限）。
    """
    if not hits:
        return ""
    try:
        budget_total = int(max_chars)
    except (TypeError, ValueError):
        budget_total = DEFAULT_MAX_CHARS
    budget = budget_total - len(RECALL_HEADER) - len(RECALL_FOOTER) - 2
    if budget <= 0:
        return ""
    lines: list[str] = []
    used = 0
    for hit in hits:
        line = _format_hit_line(hit.doc)
        if not line:
            continue
        cost = len(line) + 1
        if used + cost > budget:
            break
        lines.append(line)
        used += cost
    if not lines:
        return ""
    return "\n".join([RECALL_HEADER, *lines, RECALL_FOOTER])
