"""记忆本地存储开关：配置解析、schema 条件显隐与本地写入门禁。"""

from __future__ import annotations

import json
from pathlib import Path

from core.config import PhoneConfig
from core.semantic import SemanticIndex
from core.semantic_recall import SemanticMaintainer
from core.service import PhoneService

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _schema() -> dict:
    return json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def _service(tmp_path: Path, *, local_enabled: bool) -> PhoneService:
    """构造只覆盖 learn 所需依赖的 PhoneService（不连接设备）。"""
    return PhoneService(
        raw_config={"memory": {"local_enabled": local_enabled}},
        adb=None,
        actions=None,
        diary=None,  # type: ignore[arg-type]
        data_dir=tmp_path,
    )


def test_memory_local_defaults_and_parsing():
    """本地存储默认开启；显式关闭时解析为 False。"""
    assert PhoneConfig.from_mapping({}).memory.local_enabled is True
    off = PhoneConfig.from_mapping({"memory": {"local_enabled": False}})
    assert off.memory.local_enabled is False
    # 旧版 storage backend 字段不影响本地存储开关
    legacy = PhoneConfig.from_mapping({"memory": {"backend": "livingmemory"}})
    assert legacy.memory.local_enabled is True


def test_conf_schema_memory_local_and_recall_condition():
    """schema：本地存储开关默认开启；聊天语义找回整卡随本地存储条件显隐。"""
    schema = _schema()
    memory = schema["memory"]["items"]
    assert memory["local_enabled"]["type"] == "bool"
    assert memory["local_enabled"]["default"] is True
    assert schema["recall"]["condition"] == {"memory.local_enabled": True}


def test_service_learn_writes_local_archive_when_enabled(tmp_path):
    """本地存储开启时学习记录照常写入 learning.jsonl。"""
    service = _service(tmp_path, local_enabled=True)
    refreshed: list[bool] = []
    service.semantic_refresh_hook = lambda: refreshed.append(True)

    import asyncio

    text = asyncio.run(service.learn("今天看懂了向量检索", source="测试"))

    assert "已记录学习内容" in text
    assert (tmp_path / "learning.jsonl").is_file()
    assert refreshed == [True]


def test_service_learn_skips_local_archive_when_disabled(tmp_path):
    """本地存储关闭时不再写 learning.jsonl，也不触发语义索引刷新。"""
    service = _service(tmp_path, local_enabled=False)
    refreshed: list[bool] = []
    service.semantic_refresh_hook = lambda: refreshed.append(True)

    import asyncio

    text = asyncio.run(service.learn("今天看懂了向量检索", source="测试"))

    assert "本地存储已关闭" in text
    assert not (tmp_path / "learning.jsonl").exists()
    assert refreshed == []


class _ConfigWithMemory:
    """最小配置桩：只包含 recall 与 memory 两个分组。"""

    class _Recall:
        enabled = True
        embedding_provider_id = ""
        include_learning = True
        include_diary = True
        include_app_experience = True
        doc_chars = 300
        index_max_docs = 2000

    class _Memory:
        def __init__(self, local_enabled: bool) -> None:
            self.local_enabled = local_enabled

    def __init__(self, local_enabled: bool) -> None:
        self.recall = self._Recall()
        self.memory = self._Memory(local_enabled)


class _NoopResolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, provider_id: str = ""):
        self.calls += 1
        raise AssertionError("本地存储关闭时不应解析 Embedding Provider")


def test_semantic_maintainer_skips_when_local_disabled(tmp_path):
    """本地存储关闭时语义索引维护直接跳过（不解析 Provider、不建索引）。"""
    resolver = _NoopResolver()
    maintainer = SemanticMaintainer(
        index=SemanticIndex(tmp_path / "semantic"),
        resolver=resolver,  # type: ignore[arg-type]
        config_getter=lambda: _ConfigWithMemory(local_enabled=False),
        data_dir=tmp_path,
    )

    import asyncio

    assert asyncio.run(maintainer.refresh()) is False
    assert resolver.calls == 0
    assert not (tmp_path / "semantic").exists()
