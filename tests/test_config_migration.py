"""配置迁移的许可边界、幂等和持久化失败回归。"""

from __future__ import annotations

import copy
import json

import pytest

from core.config_migration import (
    ConfigMigrationError,
    SOURCE_PLUGIN,
    apply_migration,
    prepare_migration,
)
from core.config import PhoneConfig
from core.identity import PLUGIN_NAME, plugin_name

SCHEMA = {
    "adb": {
        "type": "object",
        "items": {
            "host": {"type": "string", "default": "127.0.0.1"},
            "port": {"type": "int", "default": 5555},
        },
    },
    "security": {
        "type": "object",
        "items": {
            "owner_ids": {"type": "list", "default": []},
            "allow_shell_tool": {"type": "bool", "default": False},
        },
    },
    "wireguard": {
        "type": "object",
        "items": {"preshared_key": {"type": "string", "default": ""}},
    },
}
TARGET = "astrbot_phone_control_mirror"


def setup_configs(root, source=None, target=None):
    src = root / f"{SOURCE_PLUGIN}_config.json"
    dst = root / f"{TARGET}_config.json"
    source = (
        source
        if source is not None
        else {
            "adb": {"host": "192.0.2.10", "port": 16512},
            "security": {"owner_ids": ["owner"], "allow_shell_tool": True},
            "wireguard": {"preshared_key": "SECRET_NOT_FOR_PREVIEW"},
            "unknown": "skip",
        }
    )
    target = (
        target
        if target is not None
        else {
            "adb": {"host": "127.0.0.1", "port": 5555},
            "security": {"owner_ids": ["custom"]},
        }
    )
    src.write_text(json.dumps(source))
    dst.write_text(json.dumps(target))
    return src, dst


def test_preview_apply_preserves_target_custom_values_source_and_private_backup(
    tmp_path,
):
    source, target = setup_configs(tmp_path)
    before_source, before_target = source.read_bytes(), target.read_bytes()
    plan = prepare_migration(tmp_path, TARGET, SCHEMA)
    assert plan.changed == (
        "adb.host",
        "adb.port",
        "security.allow_shell_tool",
        "wireguard.preshared_key",
    )
    assert plan.kept == ("security.owner_ids",)
    assert "SECRET_NOT_FOR_PREVIEW" not in repr(plan)
    assert "SECRET_NOT_FOR_PREVIEW" not in json.dumps(plan.summary())
    assert source.read_bytes() == before_source and target.read_bytes() == before_target
    result = apply_migration(tmp_path, TARGET, SCHEMA, plan.token)
    assert result.merged["security"] == {
        "owner_ids": ["custom"],
        "allow_shell_tool": True,
    }
    assert "unknown" not in result.merged
    assert json.loads(target.read_text())["adb"]["port"] == 16512
    assert source.read_bytes() == before_source
    backups = list(tmp_path.glob(f"{TARGET}_migration-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == before_target
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert target.stat().st_mode & 0o777 == 0o600
    again = prepare_migration(tmp_path, TARGET, SCHEMA)
    assert not again.changed
    assert apply_migration(tmp_path, TARGET, SCHEMA, again.token).changed == ()
    assert len(list(tmp_path.glob(f"{TARGET}_migration-*.json"))) == 1


@pytest.mark.parametrize("changed_file", ["source", "target"])
def test_changed_after_preview_requires_new_preview(tmp_path, changed_file):
    source, target = setup_configs(tmp_path)
    plan = prepare_migration(tmp_path, TARGET, SCHEMA)
    path = source if changed_file == "source" else target
    path.write_text(path.read_text() + "\n")
    before = target.read_bytes()
    with pytest.raises(ConfigMigrationError, match="重新预览"):
        apply_migration(tmp_path, TARGET, SCHEMA, plan.token)
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    "bad",
    [
        {"adb": []},
        {"adb": {"port": "5555"}},
        {"security": {"owner_ids": [1]}},
        {"security": {"allow_shell_tool": "false"}},
    ],
)
def test_invalid_source_shape_rejected(tmp_path, bad):
    setup_configs(tmp_path, source=bad)
    with pytest.raises(ConfigMigrationError):
        prepare_migration(tmp_path, TARGET, SCHEMA)


def test_missing_corrupt_oversize_and_same_target_are_explicit(tmp_path):
    with pytest.raises(ConfigMigrationError, match="未找到"):
        prepare_migration(tmp_path, TARGET, SCHEMA)
    source, _ = setup_configs(tmp_path)
    for content in ("{broken", "[]", " " * (1024 * 1024 + 1)):
        source.write_text(content)
        with pytest.raises(ConfigMigrationError):
            prepare_migration(tmp_path, TARGET, SCHEMA)
    with pytest.raises(ConfigMigrationError, match="相同"):
        prepare_migration(tmp_path, SOURCE_PLUGIN, SCHEMA)
    with pytest.raises(ConfigMigrationError, match="标识"):
        prepare_migration(tmp_path, "../outside", SCHEMA)


def test_symlink_source_rejected(tmp_path):
    source, target = setup_configs(tmp_path)
    source.unlink()
    source.symlink_to(target)
    with pytest.raises(ConfigMigrationError, match="符号链接"):
        prepare_migration(tmp_path, TARGET, SCHEMA)


def test_missing_target_can_be_created_and_blank_source_adb_uses_defaults(tmp_path):
    source, target = setup_configs(
        tmp_path,
        source={"adb": {"host": "", "port": 0}, "security": {"allow_shell_tool": True}},
    )
    target.unlink()
    plan = prepare_migration(tmp_path, TARGET, SCHEMA)
    assert plan.merged["adb"] == {"host": "127.0.0.1", "port": 5555}
    apply_migration(tmp_path, TARGET, SCHEMA, plan.token)
    assert target.is_file()


def test_failed_persist_keeps_source_and_current_target(tmp_path, monkeypatch):
    source, target = setup_configs(tmp_path)
    plan = prepare_migration(tmp_path, TARGET, SCHEMA)
    originals = source.read_bytes(), target.read_bytes()

    def fail(_):
        raise OSError("disk full")

    with pytest.raises(ConfigMigrationError, match="保存"):
        apply_migration(tmp_path, TARGET, SCHEMA, plan.token, persist=fail)
    assert (source.read_bytes(), target.read_bytes()) == originals

    def replace_fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("core.config_migration.os.replace", replace_fail)
    with pytest.raises(ConfigMigrationError):
        apply_migration(tmp_path, TARGET, SCHEMA, plan.token)
    assert (source.read_bytes(), target.read_bytes()) == originals
    assert not list(tmp_path.glob(".*.tmp"))


def test_metadata_name_controls_namespace(tmp_path):
    path = tmp_path / "metadata.yaml"
    for text in [
        "name: custom_plugin",
        "name: 'custom_plugin'",
        'name: "custom_plugin"',
    ]:
        path.write_text(text)
        assert plugin_name(path) == "custom_plugin"
    for text in ["name: ../outside", "display_name: absent", "name: bad-name"]:
        path.write_text(text)
        with pytest.raises(ValueError):
            plugin_name(path)
    assert PLUGIN_NAME == TARGET


def test_empty_adb_config_parses_as_unconfigured():
    raw = {"adb": {"host": "", "port": 0}, "security": {"owner_ids": ["private"]}}
    config = PhoneConfig.from_mapping(raw)
    assert config.adb.configured is False
    assert config.adb.target == ""
    assert config.security.owner_ids == ("private",)
    custom = PhoneConfig.from_mapping({"adb": {"host": "192.0.2.10", "port": 16512}})
    assert custom.adb.configured is True
    assert custom.adb.target == "192.0.2.10:16512"
