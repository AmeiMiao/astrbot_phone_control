"""持久化资料合并、路径安全、备份回滚与幂等验证。"""

import hashlib
import json
from pathlib import Path

import pytest

from core import data_migration as migration
from core.data_migration import (
    DataMigrationError,
    apply_data_migration,
    prepare_data_migration,
)

NAME = "astrbot_phone_control_mirror"


def write(root, rel, value):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    return path


def rows(root, rel, values):
    return write(root, rel, b"".join((json.dumps(x) + "\n").encode() for x in values))


def fixture(tmp_path):
    source = tmp_path / "astrbot_phone_control"
    target = tmp_path / NAME
    source.mkdir()
    target.mkdir()
    return source, target


def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_all_records_images_conflicts_paths_and_repeated_import(tmp_path):
    source, target = fixture(tmp_path)
    shot = write(source, "screenshots/shot.jpg", b"SOURCE_IMAGE")
    write(target, "screenshots/shot.jpg", b"CURRENT_IMAGE")
    for rel in migration.JSONL_FILES:
        rows(
            source,
            rel,
            [
                {"id": "shared", "ts": 1, "value": "source"},
                {"id": "source", "ts": 2, "screenshot": str(shot)},
            ],
        )
        rows(
            target,
            rel,
            [{"id": "shared", "ts": 1, "value": "current"}, {"id": "target", "ts": 3}],
        )
    for rel, entries in [
        (
            "app_memory.json",
            [
                {"app": "app", "page": "page", "note": "source"},
                {"app": "unique", "note": "new"},
            ],
        ),
        (
            "novelty.json",
            [{"key": "shared", "fatigue": 10}, {"key": "new", "fatigue": 20}],
        ),
    ]:
        write(source, rel, {"version": 1, "entries": entries})
        write(
            target, rel, {"version": 1, "entries": [dict(entries[0], preserved=True)]}
        )
    write(source, "experience_pending.json", [{"app": "one"}, {"app": "shared"}])
    write(target, "experience_pending.json", [{"app": "shared"}])
    image = b"PERSISTENT_IMAGE"
    digest = hashlib.sha256(image).hexdigest()
    file = write(source, "saved_images/files/" + digest + ".png", image)
    write(
        source,
        "saved_images/index.json",
        {
            digest: {
                "id": digest,
                "server_path": str(file),
                "phone_path": "/storage/emulated/0/Pictures/AstrBot/file.png",
            }
        },
    )
    # 运行期文件、任意未知文件保持隔离。
    for rel in [
        "verification.json",
        "interaction_reservations.json",
        "wireguard/phone-wg.json",
        "adbhome/.android/adb.5037",
        "media/cache.wav",
        "other.json",
    ]:
        write(source, rel, {"runtime": "SECRET"})
    before_source, before_target = snapshot(source), snapshot(target)
    plan = prepare_data_migration(tmp_path, NAME)
    assert plan.writes and "SECRET" not in repr(plan) + json.dumps(plan.summary())
    assert snapshot(target) == before_target
    apply_data_migration(tmp_path, NAME, plan.token)
    assert snapshot(source) == before_source
    renamed = next(p for p in (target / "screenshots").glob("*-migrated-*.jpg"))
    assert renamed.read_bytes() == b"SOURCE_IMAGE"
    assert (target / "screenshots/shot.jpg").read_bytes() == b"CURRENT_IMAGE"
    for rel in migration.JSONL_FILES:
        merged = [json.loads(line) for line in (target / rel).read_text().splitlines()]
        assert len(merged) == 3
        assert next(x for x in merged if x["id"] == "shared")["value"] == "current"
        assert next(x for x in merged if x["id"] == "source")["screenshot"] == str(
            renamed
        )
    for rel in ["app_memory.json", "novelty.json"]:
        entries = json.loads((target / rel).read_text())["entries"]
        assert len(entries) == 2 and entries[0]["preserved"]
    catalog = json.loads((target / "saved_images/index.json").read_text())
    assert catalog[digest]["server_path"] == str(target / file.relative_to(source))
    assert Path(catalog[digest]["server_path"]).read_bytes() == image
    assert catalog[digest]["phone_path"].startswith("/storage/")
    assert not (target / "verification.json").exists()
    backups = list((tmp_path / (NAME + "_migration-backups")).glob("*/manifest.json"))
    assert (
        len(backups) == 1 and json.loads(backups[0].read_text())["state"] == "complete"
    )
    for rel, data in before_target.items():
        assert (backups[0].parent / rel).read_bytes() == data
    assert all(
        p.stat().st_mode & 0o777 == 0o600
        for p in backups[0].parent.rglob("*")
        if p.is_file()
    )
    assert prepare_data_migration(tmp_path, NAME).summary()["count"] == 0
    final = snapshot(target)
    again = prepare_data_migration(tmp_path, NAME)
    apply_data_migration(tmp_path, NAME, again.token)
    assert snapshot(target) == final


def test_legacy_learning_ids_survive_merge_and_reload(tmp_path):
    source, target = fixture(tmp_path)
    rows(source, "learning.jsonl", [{"ts": 1, "content": "source"}])
    original_line = json.dumps({"ts": 2, "content": "current"})
    rows(target, "learning.jsonl", [{"ts": 2, "content": "current"}])
    plan = prepare_data_migration(tmp_path, NAME)
    apply_data_migration(tmp_path, NAME, plan.token)
    values = [
        json.loads(x) for x in (target / "learning.jsonl").read_text().splitlines()
    ]
    expected = hashlib.sha256(("0:" + original_line).encode()).hexdigest()
    assert values[1]["id"] == expected
    assert not prepare_data_migration(tmp_path, NAME).writes


def test_optional_keys_preserve_existing_pairs_and_do_not_expose_values(tmp_path):
    source, target = fixture(tmp_path)
    for rel in migration.KEY_FILES:
        write(
            source,
            rel,
            {"keys": {"SECRET": "value"}} if rel.endswith(".json") else b"SECRET_KEY",
        )
    assert not prepare_data_migration(tmp_path, NAME).writes
    plan = prepare_data_migration(tmp_path, NAME, include_keys=True)
    assert set(plan.writes) == migration.KEY_FILES
    assert "SECRET" not in repr(plan) + json.dumps(plan.summary())
    with pytest.raises(DataMigrationError, match="已变化"):
        apply_data_migration(tmp_path, NAME, plan.token)
    apply_data_migration(tmp_path, NAME, plan.token, include_keys=True)
    assert not prepare_data_migration(tmp_path, NAME, include_keys=True).writes
    write(source, "adbhome/.android/adbkey", b"DIFFERENT")
    assert not prepare_data_migration(tmp_path, NAME, include_keys=True).writes
    assert (target / "adbhome/.android/adbkey").read_bytes() == b"SECRET_KEY"


def test_incomplete_adb_key_pair_fails_before_any_write(tmp_path):
    source, target = fixture(tmp_path)
    write(source, "adbhome/.android/adbkey", b"PRIVATE")
    with pytest.raises(DataMigrationError, match="同时存在"):
        prepare_data_migration(tmp_path, NAME, include_keys=True)
    assert not snapshot(target)


@pytest.mark.parametrize("side", ["source", "target"])
def test_stale_preview_rejected_with_no_writes(tmp_path, side):
    source, target = fixture(tmp_path)
    rows(source, "diary/diary.jsonl", [{"id": "one"}])
    plan = prepare_data_migration(tmp_path, NAME)
    rows(source if side == "source" else target, "diary/diary.jsonl", [{"id": "new"}])
    before = snapshot(target)
    with pytest.raises(DataMigrationError, match="已变化"):
        apply_data_migration(tmp_path, NAME, plan.token)
    assert snapshot(target) == before


@pytest.mark.parametrize(
    "where",
    [
        "source",
        "target",
        "source-child",
        "target-child",
        "source-file",
        "target-file",
        "backup",
    ],
)
def test_symlink_escape_rejected(tmp_path, where):
    source, target = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    rows(source, "learning.jsonl", [{"id": "entry"}])
    if where == "backup":
        (tmp_path / (NAME + "_migration-backups")).symlink_to(outside)
        plan = prepare_data_migration(tmp_path, NAME)
        with pytest.raises(DataMigrationError, match="符号链接"):
            apply_data_migration(tmp_path, NAME, plan.token)
    else:
        root = source if where.startswith("source") else target
        if "child" in where:
            (root / "screenshots").symlink_to(outside)
        elif "file" in where:
            file = write(outside, "file", b"{}")
            (root / "novelty.json").symlink_to(file)
        else:
            for p in root.iterdir():
                p.unlink()
            root.rmdir()
            root.symlink_to(outside)
        with pytest.raises(DataMigrationError, match="符号链接"):
            prepare_data_migration(tmp_path, NAME)
    assert not snapshot(outside) or snapshot(outside) == {"file": b"{}"}


@pytest.mark.parametrize(
    "rel,value",
    [
        ("learning.jsonl", b"not-json\n"),
        ("learning.jsonl", b"[]\n"),
        ("app_memory.json", b'{"entries":{}}'),
        ("novelty.json", b'{"entries":[{}]}'),
        ("experience_pending.json", b"{}"),
        ("saved_images/index.json", b"[]"),
        ("saved_images/index.json", b'{"bad":{}}'),
    ],
)
def test_invalid_structures_are_not_silently_skipped(tmp_path, rel, value):
    source, target = fixture(tmp_path)
    write(source, rel, value)
    with pytest.raises(DataMigrationError):
        prepare_data_migration(tmp_path, NAME)
    assert not snapshot(target)


def test_size_limits(tmp_path, monkeypatch):
    source, target = fixture(tmp_path)
    write(source, "screenshots/a.jpg", b"123456")
    monkeypatch.setattr(migration, "MAX_FILE_BYTES", 5)
    with pytest.raises(DataMigrationError, match="限制"):
        prepare_data_migration(tmp_path, NAME)


def test_bad_image_hash_or_external_index_path_rejected(tmp_path):
    source, target = fixture(tmp_path)
    digest = "a" * 64
    file = write(source, "saved_images/files/" + digest + ".jpg", b"bad")
    write(source, "saved_images/index.json", {digest: {"server_path": str(file)}})
    with pytest.raises(DataMigrationError, match="哈希"):
        prepare_data_migration(tmp_path, NAME)
    write(source, "saved_images/index.json", {digest: {"server_path": "/etc/passwd"}})
    with pytest.raises(DataMigrationError, match="路径"):
        prepare_data_migration(tmp_path, NAME)


def test_mid_write_failure_rolls_back_existing_and_new_files(tmp_path, monkeypatch):
    source, target = fixture(tmp_path)
    rows(source, "learning.jsonl", [{"id": "source"}])
    rows(target, "learning.jsonl", [{"id": "target"}])
    rows(source, "token_usage.jsonl", [{"tokens": 4}])
    write(source, "novelty.json", {"entries": []})
    before = snapshot(target)
    plan = prepare_data_migration(tmp_path, NAME)
    actual = migration._atomic_write

    def fail(path, data):
        if path == target / "token_usage.jsonl":
            raise OSError("SECRET_WRITE_ERROR")
        actual(path, data)

    monkeypatch.setattr(migration, "_atomic_write", fail)
    with pytest.raises(DataMigrationError, match="已恢复") as error:
        apply_data_migration(tmp_path, NAME, plan.token)
    assert "SECRET_WRITE_ERROR" not in str(error.value)
    assert snapshot(target) == before
    assert (
        len(list((tmp_path / (NAME + "_migration-backups")).glob("*/manifest.json")))
        == 1
    )


def test_runtime_reload_failure_restores_disk_and_cache(tmp_path):
    source, target = fixture(tmp_path)
    rows(source, "learning.jsonl", [{"id": "source"}])
    before = snapshot(target)
    calls = []

    def reload():
        calls.append(snapshot(target))
        if len(calls) == 1:
            raise RuntimeError("RELOAD_FAIL")

    plan = prepare_data_migration(tmp_path, NAME)
    with pytest.raises(DataMigrationError, match="已恢复"):
        apply_data_migration(tmp_path, NAME, plan.token, reload=reload)
    assert len(calls) == 2 and calls[-1] == before and snapshot(target) == before


@pytest.mark.parametrize("name", ["astrbot_phone_control", "../outside", "", "a/b"])
def test_identity_boundaries(tmp_path, name):
    fixture(tmp_path)
    with pytest.raises(DataMigrationError):
        prepare_data_migration(tmp_path, name)


def test_source_must_exist(tmp_path):
    with pytest.raises(DataMigrationError, match="未找到"):
        prepare_data_migration(tmp_path, NAME)
