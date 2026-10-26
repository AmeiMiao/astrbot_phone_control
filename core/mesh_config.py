"""插件管理的用户态组网配置。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MeshSettings:
    enabled: bool = False
    provider: str = "tailscale"
    hostname: str = "astrbot-phone"
    tailscale_auth_key: str = ""
    tailscale_path: str = ""
    tailscaled_path: str = ""
    zerotier_network_id: str = ""
    zerotier_library: str = ""
    zerotier_force_tcp_relay: bool = False
    phone_ip: str = ""
    auto_adb: bool = False
    adb_port: int = 15555
    local_adb_port: int = 18855
    proxy_port: int = 18889


def mesh_settings(card, warn):
    data = card if isinstance(card, dict) else {}
    values = {}
    defaults = MeshSettings()
    for key in defaults.__dataclass_fields__:
        value = data.get(key, getattr(defaults, key))
        default = getattr(defaults, key)
        if isinstance(default, bool):
            value = value if isinstance(value, bool) else default
        elif isinstance(default, int):
            try:
                value = int(value)
                if not 1024 <= value <= 65535:
                    raise ValueError()
            except (ValueError, TypeError):
                warn(f"mesh.{key} 需为 1024–65535 的端口，使用默认值")
                value = default
        else:
            value = str(value or "").strip()
        values[key] = value
    return MeshSettings(**values)
