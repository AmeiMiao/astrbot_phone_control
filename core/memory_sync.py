"""记忆/学习内容的存储去向与同步载荷（纯逻辑，便于本地单测）。

插件的学习档案存入本地，并可按配置同步到
AstrBot 的 MemoryCompanion / LivingMemory 记忆插件（``astrbot_plugin_memory_companion``
/ ``astrbot_plugin_livingmemory``）。本模块只负责：

* 归一化配置里的存储去向（``local`` / ``companion`` / ``livingmemory`` /
  ``companion+livingmemory``；``both``/``all`` 归一化为 ``companion+livingmemory``）；
* 判断是否需要写本地 / 同步各记忆插件；
* 把学习记录转换成记忆插件写入所需的载荷（摘要正文、类型、幂等键、
  标签与结构化 payload）。

实际桥接调用（跨插件）在 ``core/plugin/observability.py`` 完成；本模块不导入 astrbot。
"""

from __future__ import annotations

from typing import Any

# 存储去向：local=仅插件本地；companion=同步 MemoryCompanion；
# livingmemory=同步 LivingMemory；companion+livingmemory=两个记忆插件都同步。
# 插件本地档案始终保留（作为查找缓存与插件不可用时的回退）。
MEMORY_BACKENDS = ("local", "companion", "livingmemory", "companion+livingmemory")
LEGACY_MEMORY_BACKENDS = {"both": "companion+livingmemory", "all": "companion+livingmemory"}
DEFAULT_MEMORY_BACKEND = "local"
LIVINGMEMORY_PLUGIN_NAME = "astrbot_plugin_livingmemory"
COMPANION_PLUGIN_NAME = "astrbot_plugin_memory_companion"
COMPANION_SOURCE_PLUGIN = "astrbot_phone_control"
LEARNING_MEMORY_TYPE = "phone_learning"


def normalize_memory_backend(raw: Any) -> str:
    """归一化存储去向：``both``/``all`` 归一化为 ``companion+livingmemory``；非法/空值回退 ``local``。"""
    text = str(raw or "").strip().lower()
    if text in MEMORY_BACKENDS:
        return text
    legacy = LEGACY_MEMORY_BACKENDS.get(text)
    if legacy:
        return legacy
    return DEFAULT_MEMORY_BACKEND


def wants_local(backend: Any) -> bool:
    """插件本地档案始终写入（查找缓存与回退）。"""
    normalize_memory_backend(backend)
    return True


def wants_companion(backend: Any) -> bool:
    """是否需要同步写入 MemoryCompanion。"""
    return normalize_memory_backend(backend) in {"companion", "companion+livingmemory"}


def wants_livingmemory(backend: Any) -> bool:
    """是否需要同步写入 LivingMemory。"""
    return normalize_memory_backend(backend) in {"livingmemory", "companion+livingmemory"}


def build_bot_action_payload(*, content: Any, source: Any = "") -> str:
    """构造 MemoryCompanion「个人记忆-行动」文本（自行刷手机的一轮记忆）。"""
    text = _clean(content)
    if not text:
        return ""
    src = _clean(source)
    prefix = f"【自行刷手机】{src + '：' if src else ''}"
    return (prefix + text)[:4000]


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def build_learning_payload(
    *,
    content: Any,
    source: Any = "",
    tags: Any = None,
) -> dict[str, Any]:
    """把一条学习记录转成 MemoryCompanion 外部记忆载荷。

    Returns:
        ``{"content": "...", "memory_type": "phone_learning", "idempotency_key": ...,
        "tags": [...], "payload": {...}}``；content 为空时返回空 dict。
    """
    text = _clean(content)
    if not text:
        return {}
    src = _clean(source)
    tag_list = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
    summary = f"【手机学习】{src + '：' if src else ''}{text}"
    return {
        "content": summary[:4000],
        "memory_type": LEARNING_MEMORY_TYPE,
        "idempotency_key": f"phone-learning:{src or '-'}:{text[:120]}",
        "tags": ["phone", "learning", *tag_list],
        "payload": {"kind": "learning", "source": src, "tags": tag_list},
    }
