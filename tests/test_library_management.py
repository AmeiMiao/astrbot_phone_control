"""图片与学习资料管理的持久化、精确删除及失败恢复。"""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from core.config import PhoneConfig
from core.errors import PhoneControlError
from core.learning import LearningError, LearningStore
from core.saved_images import SavedImages, private_write


def test_delete_learning_migrates_ids_and_keeps_identical_neighbor(tmp_path):
    row = {"ts": 10, "content": "相同内容", "source": "测试", "tags": []}
    path = tmp_path / "learning.jsonl"
    path.write_text((json.dumps(row) + "\n") * 2)
    store = LearningStore(tmp_path)
    first, second = store.entries()
    assert first["id"] != second["id"]
    store.delete(first["id"])
    restored = LearningStore(tmp_path)
    assert restored.entries() == [second]
    with pytest.raises(LearningError, match="不存在"):
        restored.delete(first["id"])
    assert restored.stats()["entries"] == 1
    assert restored.search("相同")[0]["id"] == second["id"]


def test_learning_delete_failure_preserves_memory_and_disk(tmp_path, monkeypatch):
    store = LearningStore(tmp_path)
    entry = store.record(content="保留数据")
    before = store.path.read_bytes()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("core.learning.os.replace", fail)
    with pytest.raises(LearningError, match="未保存"):
        store.delete(entry["id"])
    assert store.entries() == [entry]
    assert store.path.read_bytes() == before


def make_library(tmp_path):
    library = SavedImages(SimpleNamespace(data_dir=tmp_path))
    data = b"GIF89a" + b"synthetic-sticker"
    digest = hashlib.sha256(data).hexdigest()
    path = library.root / "files" / f"{digest}.gif"
    private_write(path, data)
    record = {
        "id": digest,
        "sha256": digest,
        "server_path": str(path),
        "description": "合成表情",
        "reason": "喜欢",
        "tags": ["测试"],
        "kind": "sticker",
    }
    library.write({digest: record})
    return library, record, path


def test_image_edit_preview_delete_survive_reload(tmp_path):
    async def run():
        library, record, path = make_library(tmp_path)
        updated = await library.update_semantics(
            record["id"],
            description="新的语义",
            reason="新的理由",
            tags=["开心", "开心"],
            kind="image",
        )
        assert updated["tags"] == ["开心"]
        reloaded = SavedImages(library.service)
        assert reloaded.find("新的语义")[0]["reason"] == "新的理由"
        assert await reloaded.preview(record["id"]) == (path.read_bytes(), ".gif")
        await reloaded.delete(record["id"])
        assert not path.exists() and reloaded.load() == {}
        with pytest.raises(PhoneControlError, match="不存在"):
            await reloaded.delete(record["id"])

    asyncio.run(run())


def test_image_delete_catalog_failure_restores_original(tmp_path, monkeypatch):
    async def run():
        library, record, path = make_library(tmp_path)
        before = library.catalog.read_bytes()

        def fail(_):
            raise OSError("disk full")

        monkeypatch.setattr(library, "write", fail)
        with pytest.raises(OSError):
            await library.delete(record["id"])
        assert path.is_file() and library.catalog.read_bytes() == before
        assert not path.with_suffix(".gif.deleting").exists()

    asyncio.run(run())


def test_image_paths_cannot_read_or_delete_outside_library(tmp_path):
    async def run():
        library, record, path = make_library(tmp_path)
        outside = tmp_path / "private.gif"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with pytest.raises(PhoneControlError, match="没有可用"):
            await library.preview(record["id"])
        with pytest.raises(PhoneControlError, match="没有可用"):
            await library.delete(record["id"])
        assert outside.is_file() and library.load()

    asyncio.run(run())


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", ""),
        ("description", "x" * 501),
        ("tags", "bad"),
        ("tags", ["x" * 41]),
        ("kind", "bad"),
    ],
)
def test_invalid_semantics_preserves_catalog(tmp_path, field, value):
    async def run():
        library, record, _ = make_library(tmp_path)
        before = library.catalog.read_bytes()
        values = dict(description="有效语义", reason="理由", tags=[], kind="image")
        values[field] = value
        with pytest.raises(PhoneControlError):
            await library.update_semantics(record["id"], **values)
        assert library.catalog.read_bytes() == before

    asyncio.run(run())


def test_default_keep_list_upgrades_saved_defaults_and_retains_custom_settings():
    relay = "fun.acat.phonerelay"
    assert relay in PhoneConfig.from_mapping({}).security.background_keep_packages
    for value in [
        ["com.wireguard.android", "com.tailscale.ipn"],
        ["com.tailscale.ipn", "com.wireguard.android"],
    ]:
        assert (
            relay
            in PhoneConfig.from_mapping(
                {"security": {"background_keep_packages": value}}
            ).security.background_keep_packages
        )
    for value in [
        [],
        ["com.example.keep"],
        ["com.wireguard.android", "com.example.keep"],
    ]:
        assert PhoneConfig.from_mapping(
            {"security": {"background_keep_packages": value}}
        ).security.background_keep_packages == tuple(value)


def test_phone_only_preview_reads_matching_device_and_checks_hash(tmp_path):
    async def run():
        library, record, path = make_library(tmp_path)
        data = path.read_bytes()
        record.update(
            server_path="",
            phone_path=f"/storage/emulated/0/Pictures/AstrBot/{record['id']}.gif",
            phone_device="test-phone",
        )
        library.write({record["id"]: record})
        calls = []

        async def exec_out(*args):
            calls.append(args)
            return data

        library.service.adb = SimpleNamespace(serial="other-phone", exec_out=exec_out)
        library.service.guard_device = lambda _: None
        with pytest.raises(PhoneControlError, match="保存这张图片"):
            await library.preview(record["id"])
        assert not calls
        library.service.adb.serial = "test-phone"
        assert await library.preview(record["id"]) == (data, ".gif")
        assert calls[0] == (
            "head",
            "-c",
            str(20 * 1024 * 1024 + 1),
            record["phone_path"],
        )
        assert not path.with_suffix(".gif.tmp").exists()
        library.service.gate = SimpleNamespace(busy=True)
        with pytest.raises(PhoneControlError, match="正在执行任务"):
            await library.preview(record["id"])

    asyncio.run(run())
