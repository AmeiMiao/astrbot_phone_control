"""统一组网的互斥、迁移和保存边界。"""

import copy
import json
from pathlib import Path

import pytest

from core.config import PhoneConfig
from core.config_apply import build_apply_plan
from core.config_migration import ConfigMigrationError, prepare_migration
from core.network_config import (
    migrate_network,
    migrate_network_file,
    normalize_network_patch,
)


@pytest.mark.parametrize("provider", ["wireguard", "tailscale", "zerotier"])
@pytest.mark.parametrize("enabled", [True, False])
def test_only_selected_provider_can_run(provider, enabled):
    config = PhoneConfig.from_mapping(
        {
            "network": {
                "enabled": enabled,
                "provider": provider,
                "wireguard": {"auto_adb": True, "adb_local_port": 18800},
                "tailscale": {"phone_ip": "100.64.0.3"},
                "zerotier": {"phone_ip": "10.0.0.3"},
            }
        }
    )
    assert config.wireguard.enabled == (enabled and provider == "wireguard")
    assert config.mesh.enabled == (enabled and provider != "wireguard")
    assert not (config.wireguard.enabled and config.mesh.enabled)
    assert config.wireguard.adb_local_port == 18800
    if provider != "wireguard":
        assert config.mesh.phone_ip == (
            "100.64.0.3" if provider == "tailscale" else "10.0.0.3"
        )


def test_invalid_provider_stays_disabled():
    warnings = []
    c = PhoneConfig.from_mapping(
        {"network": {"enabled": True, "provider": "invalid"}}, warn=warnings.append
    )
    assert not c.mesh.enabled and not c.wireguard.enabled and warnings


@pytest.mark.parametrize("provider", ["wireguard", "tailscale", "zerotier"])
def test_legacy_migration_preserves_parameters_and_disabled_choice(provider):
    raw = {
        "adb": {"host": "host", "port": 5555},
        "wireguard": {
            "enabled": provider == "wireguard",
            "mode": "server",
            "preshared_key": "private",
            "auto_adb": True,
        },
        "mesh": {
            "enabled": provider != "wireguard",
            "provider": provider if provider != "wireguard" else "tailscale",
            "tailscale_auth_key": "private-auth",
            "zerotier_network_id": "0123456789abcdef",
            "zerotier_library": "/manual/libzt.so",
            "phone_ip": "10.0.0.2",
        },
    }
    before = PhoneConfig.from_mapping(raw)
    assert migrate_network(raw)
    after = PhoneConfig.from_mapping(raw)
    assert before.wireguard == after.wireguard
    assert (before.mesh.enabled, before.mesh.provider, before.mesh.phone_ip) == (
        after.mesh.enabled,
        after.mesh.provider,
        after.mesh.phone_ip,
    )
    assert raw["network"]["tailscale"]["tailscale_auth_key"] == "private-auth"
    assert raw["network"]["zerotier"]["zerotier_network_id"] == "0123456789abcdef"
    assert raw["network"]["zerotier"]["zerotier_library"] == "/manual/libzt.so"
    assert raw["network"]["provider"] == provider
    assert "private-auth" not in json.dumps(raw["network"]["zerotier"])
    raw["network"]["enabled"] = False
    assert not migrate_network(raw)
    assert not PhoneConfig.from_mapping(raw).mesh.enabled


def test_conflicting_legacy_enablement_requires_explicit_choice():
    raw = {"wireguard": {"enabled": True}, "mesh": {"enabled": True}}
    migrate_network(raw)
    config = PhoneConfig.from_mapping(raw)
    assert not config.wireguard.enabled and not config.mesh.enabled


def test_new_config_is_authoritative_over_stale_fields():
    raw = {
        "network": {"enabled": False, "provider": "zerotier"},
        "wireguard": {"enabled": True},
        "mesh": {"enabled": True},
    }
    assert migrate_network(raw)
    assert raw == {"network": {"enabled": False, "provider": "zerotier"}}


def test_file_migration_happens_before_schema_cleanup_and_keeps_private_backup(
    tmp_path,
):
    path = tmp_path / "plugin_config.json"
    old = {
        "mesh": {
            "enabled": True,
            "provider": "tailscale",
            "tailscale_auth_key": "private",
        },
        "other": "preserved",
    }
    original = b"\xef\xbb\xbf" + json.dumps(old).encode()
    path.write_bytes(original)
    assert migrate_network_file(path)
    backup = tmp_path / "plugin_config.json.network-backup"
    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == 0o600
    new = json.loads(path.read_text())
    assert new["network"]["enabled"] and new["other"] == "preserved"
    assert not migrate_network_file(path)
    assert backup.read_bytes() == original


def test_malformed_or_symlink_config_is_never_overwritten(tmp_path):
    path = tmp_path / "plugin.json"
    path.write_text("invalid")
    with pytest.raises(ConfigMigrationError):
        migrate_network_file(path)
    assert path.read_text() == "invalid"
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ConfigMigrationError):
        migrate_network_file(link)


def test_switching_provider_changes_both_runtime_plans():
    old = PhoneConfig.from_mapping(
        {"network": {"enabled": True, "provider": "tailscale"}}
    )
    new = PhoneConfig.from_mapping(
        {"network": {"enabled": True, "provider": "wireguard"}}
    )
    plan = build_apply_plan(old, new)
    assert plan.mesh_changed and plan.wireguard_changed


def test_wg_command_patch_selects_wg_and_preserves_other_provider_parameters():
    source = {
        "network": {
            "enabled": True,
            "provider": "tailscale",
            "tailscale": {"phone_ip": "100.64.0.3"},
        }
    }
    original = copy.deepcopy(source)
    patch = normalize_network_patch(
        source, {"wireguard": {"enabled": True, "mode": "server"}}
    )
    assert source == original
    assert patch == {
        "network": {
            "enabled": True,
            "provider": "wireguard",
            "wireguard": {"mode": "server"},
        }
    }


def test_source_plugin_import_translates_network_fields_before_schema_filter(tmp_path):
    source = tmp_path / "astrbot_phone_control_config.json"
    source.write_text(
        json.dumps(
            {
                "mesh": {
                    "enabled": True,
                    "provider": "zerotier",
                    "zerotier_network_id": "0123456789abcdef",
                }
            }
        )
    )
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
    )
    plan = prepare_migration(tmp_path, "astrbot_phone_control_mirror", schema)
    config = PhoneConfig.from_mapping(plan.merged)
    assert config.mesh.enabled and config.mesh.provider == "zerotier"
    assert config.mesh.zerotier_network_id == "0123456789abcdef"
    assert json.loads(source.read_text())["mesh"]["enabled"]
