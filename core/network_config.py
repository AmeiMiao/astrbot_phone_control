"""统一组网配置及框架载入配置前的持久化迁移。"""

import copy
from pathlib import Path

from .config_migration import _atomic_write, _read
from .mesh_config import MeshSettings
from .wireguard import WireGuardSettings

PROVIDERS = ("wireguard", "tailscale", "zerotier")
WG_KEYS = tuple(
    key for key in WireGuardSettings.__dataclass_fields__ if key != "enabled"
)
MESH_KEYS = tuple(
    key
    for key in MeshSettings.__dataclass_fields__
    if key not in {"enabled", "provider"}
)


def unified_network(source):
    if isinstance(source.get("network"), dict):
        return copy.deepcopy(source["network"])
    wg = source.get("wireguard") or {}
    mesh = source.get("mesh") or {}
    wg = wg if isinstance(wg, dict) else {}
    mesh = mesh if isinstance(mesh, dict) else {}
    wg_on = wg.get("enabled") is True
    mesh_on = mesh.get("enabled") is True
    result = {
        "enabled": (wg_on or mesh_on) and not (wg_on and mesh_on),
        "provider": "wireguard"
        if wg_on and not mesh_on
        else mesh.get("provider", "tailscale"),
    }
    result["wireguard"] = {key: copy.deepcopy(wg[key]) for key in WG_KEYS if key in wg}
    for provider in ("tailscale", "zerotier"):
        excluded = (
            {"zerotier_network_id", "zerotier_library", "zerotier_force_tcp_relay"}
            if provider == "tailscale"
            else {"hostname", "tailscale_auth_key", "tailscale_path", "tailscaled_path"}
        )
        result[provider] = {
            key: copy.deepcopy(mesh[key])
            for key in MESH_KEYS
            if key in mesh and key not in excluded
        }
    return result


def runtime_cards(source, warn):
    if not isinstance(source.get("network"), dict):
        return source.get("wireguard"), source.get("mesh")
    network = source["network"]
    provider = network.get("provider", "tailscale")
    valid = provider in PROVIDERS
    if not valid:
        warn("network.provider 必须为 wireguard、tailscale 或 zerotier，组网保持关闭")
    enabled = network.get("enabled") is True and valid
    card = network.get("wireguard") or {}
    card = card if isinstance(card, dict) else {}
    wg = {key: card[key] for key in WG_KEYS if key in card}
    wg["enabled"] = enabled and provider == "wireguard"
    card = (
        network.get(provider if provider in ("tailscale", "zerotier") else "tailscale")
        or {}
    )
    card = card if isinstance(card, dict) else {}
    mesh = {key: card[key] for key in MESH_KEYS if key in card}
    mesh.update(
        enabled=enabled and provider != "wireguard",
        provider=provider if provider != "wireguard" and valid else "tailscale",
    )
    return wg, mesh


def migrate_network(source):
    """原位迁移已保存的分离配置，保留各服务参数。"""
    if not isinstance(source, dict):
        return False
    if "network" in source and "wireguard" not in source and "mesh" not in source:
        return False
    if not any(key in source for key in ("wireguard", "mesh")):
        return False
    network = unified_network(source)
    source["network"] = network
    source.pop("wireguard", None)
    source.pop("mesh", None)
    return True


def migrate_network_file(path):
    """模块导入先于 AstrBot schema 整理；备份后原子保存组网配置。"""
    path = Path(path)
    value, original = _read(path, optional=True)
    if not migrate_network(value):
        return False
    backup = path.with_name(path.name + ".network-backup")
    if not backup.exists():
        _atomic_write(backup, original)
    import json

    _atomic_write(
        path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    )
    return True


def normalize_network_patch(source, patch):
    """命令补丁与面板保存使用相同组网开关。"""
    result = copy.deepcopy(patch)
    network = dict(result.get("network") or {})
    for card in ("wireguard", "mesh"):
        values = result.pop(card, None)
        if not isinstance(values, dict):
            continue
        provider = (
            "wireguard"
            if card == "wireguard"
            else values.get(
                "provider", unified_network(source).get("provider", "tailscale")
            )
        )
        if provider == "wireguard" and card == "mesh":
            provider = "tailscale"
        if "enabled" in values:
            network["enabled"] = values["enabled"]
            if values["enabled"] is True:
                network["provider"] = provider
        child = dict(network.get(provider) or {})
        child.update(
            {
                key: value
                for key, value in values.items()
                if key not in {"enabled", "provider"}
            }
        )
        if child:
            network[provider] = child
        if "provider" in values:
            network["provider"] = provider
    if network:
        result["network"] = network
    return result
