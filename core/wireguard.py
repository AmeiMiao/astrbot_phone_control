"""WireGuard 密钥、用户态组网、TCP/SOCKS5 与 Android App UID 中转。

Android 官方 WireGuard App 配合 Phone ADB Relay，将隧道内 15555 端口转给
手机 loopback adbd:5555；Linux 与 root 全局隧道可直接使用 adbd 端口。
密钥与 helper 配置以 0600 持久化，helper 由插件管理并按状态和退出码退避。
"""

from __future__ import annotations

from .identity import PLUGIN_NAME

import asyncio
import base64
import binascii
import ipaddress
import json
import os
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from astrbot.api import logger

from .process_lease import guarded_command, start_heartbeat, stop_heartbeat

from .plugin_shared import ensure_executable
from .platform_bins import bundled_phone_wg

WIREGUARD_DIR_NAME = "wireguard"
KEYS_FILE_NAME = "keys.json"
HELPER_CONFIG_FILE_NAME = "phone-wg.json"
HELPER_STATUS_FILE_NAME = "phone-wg-status.json"

MODES: tuple[str, ...] = ("client", "server")
DEFAULT_MODE = "client"
KEY_ROLES: tuple[str, ...] = ("client", "server", "phone")
DEFAULT_LISTEN_PORT = 51820
DEFAULT_ADB_LOCAL_PORT = 15555
# 两种角色缺省的本机 WG 地址 / 手机 WG 地址（配置留空时按模式取默认）。
DEFAULT_INTERFACE_ADDRESS = {"client": "10.66.0.2/24", "server": "10.66.0.1/24"}
DEFAULT_PHONE_WG_IP = {"client": "10.66.0.3", "server": "10.66.0.2"}
DEFAULT_MTU = 1420
PERSISTENT_KEEPALIVE_SECONDS = 25
# 手机 adbd 在设备内的 TCP 端口（helper 经 WG 隧道拨号的目标）。
ADB_OVER_WG_TARGET_PORT = 5555

# helper 状态文件健康阈值：契约要求周期 ≤2 秒，插件按 15 秒内更新判定健康。
STATUS_MAX_AGE_SECONDS = 15.0
STARTUP_TIMEOUT_SECONDS = 10.0
STOP_TIMEOUT_SECONDS = 5.0

# 临时失败退避：首次 30 秒，之后翻倍，最多 10 分钟；连续失败达到上限后收敛为
# 确定性不可用并停止自动重试（与抓包失败口径一致）。
RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 600.0
RETRY_FAILURE_LIMIT = 4

# helper 退出码契约：2=配置非法，3=运行期建 WG 接口失败（均为确定性失败）。
EXIT_CODE_CONFIG_ERROR = 2
EXIT_CODE_INTERFACE_ERROR = 3
_DETERMINISTIC_EXIT_CODES = frozenset({EXIT_CODE_CONFIG_ERROR, EXIT_CODE_INTERFACE_ERROR})

# X25519 参考实现常量（RFC 7748）；生产环境优先用 cryptography。
_P = 2**255 - 19
_A24 = 121665
_BASE_POINT = bytes([9] + [0] * 31)


class WireGuardError(Exception):
    """WireGuard 组网可预期错误基类。"""


class WireGuardConfigError(WireGuardError):
    """配置/密钥数据非法（确定性失败，重试不会恢复）。"""


# ----------------------------------------------------------------------
# 密钥：X25519 生成、clamp、公钥推导与持久化
# ----------------------------------------------------------------------


def clamp_private_key(raw: bytes) -> bytes:
    """按 WireGuard/X25519 规则 clamp 32 字节私钥。"""
    value = bytes(raw)
    if len(value) != 32:
        raise WireGuardConfigError(f"WireGuard 私钥长度应为 32 字节（实际 {len(value)}）")
    key = bytearray(value)
    key[0] &= 248
    key[31] &= 127
    key[31] |= 64
    return bytes(key)


def generate_private_key() -> str:
    """生成 WireGuard 私钥（32 字节随机数 + clamp，base64 标准格式）。"""
    return _b64(clamp_private_key(os.urandom(32)))


def _b64(raw: bytes) -> str:
    return base64.b64encode(bytes(raw)).decode("ascii")


def decode_key(text: Any, *, label: str = "密钥") -> bytes:
    """解码 base64 的 32 字节 WireGuard 密钥；非法值抛 :class:`WireGuardConfigError`。"""
    value = str(text or "").strip()
    if not value:
        raise WireGuardConfigError(f"{label}为空")
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WireGuardConfigError(f"{label}不是合法的 base64：{exc}") from exc
    if len(raw) != 32:
        raise WireGuardConfigError(f"{label}长度应为 32 字节（实际 {len(raw)}）")
    return raw


def is_valid_key(text: Any) -> bool:
    """判断文本是否为合法的 32 字节 base64 WireGuard 密钥。"""
    try:
        decode_key(text)
    except WireGuardConfigError:
        return False
    return True


def canonical_key(text: Any, *, label: str = "密钥") -> str:
    """把密钥规范化为标准 base64（补齐/去掉多余填充）。"""
    return _b64(decode_key(text, label=label))


def public_key_from_private(private_key: Any, *, label: str = "私钥") -> str:
    """由私钥推导 WireGuard 公钥（X25519，base64）。"""
    scalar = clamp_private_key(decode_key(private_key, label=label))
    try:
        public = _x25519_cryptography(scalar)
    except _CryptographyUnavailable:
        public = _x25519_rfc7748(scalar, _BASE_POINT)
    return _b64(public)


class _CryptographyUnavailable(Exception):
    """本环境没有 ``cryptography``（纯逻辑单测/裁剪环境）。"""


def _x25519_cryptography(scalar: bytes) -> bytes:
    """用 cryptography 计算 X25519 公钥（AstrBot 容器自带该依赖）。"""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import x25519
    except ImportError as exc:  # pragma: no cover - 仅无 cryptography 的环境走到
        raise _CryptographyUnavailable() from exc
    private = x25519.X25519PrivateKey.from_private_bytes(scalar)
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _x25519_rfc7748(scalar: bytes, u_coordinate: bytes) -> bytes:
    """纯 Python X25519（RFC 7748 Montgomery ladder），作为 cryptography 的备用实现。"""
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    scalar_int = int.from_bytes(k, "little")
    x1 = int.from_bytes(u_coordinate, "little") & ((1 << 255) - 1)
    x2, z2 = 1, 0
    x3, z3 = x1, 1
    swap = 0
    for bit_index in range(254, -1, -1):
        bit = (scalar_int >> bit_index) & 1
        swap ^= bit
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = bit
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = pow(da + cb, 2, _P)
        z3 = x1 * pow(da - cb, 2, _P) % _P
        x2 = aa * bb % _P
        z2 = e * ((aa + _A24 * e) % _P) % _P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def _write_private_json(path: Path, payload: Any) -> None:
    """原子写入含私钥的 JSON（文件 0600，目录 0700）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise
    os.replace(tmp, target)
    with suppress(OSError):
        os.chmod(target, 0o600)


class WireGuardKeyStore:
    """WireGuard 密钥持久化（client/server/phone 三套，0600）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, dict[str, str]]:
        """读取密钥文件；不存在返回空；内容损坏抛 :class:`WireGuardConfigError`。"""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise WireGuardConfigError(f"读取 WireGuard 密钥文件失败：{exc}") from exc
        if not text.strip():
            return {}
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise WireGuardConfigError(
                f"WireGuard 密钥文件不是合法 JSON：{self.path}（{exc}）"
            ) from exc
        if not isinstance(payload, Mapping):
            raise WireGuardConfigError(f"WireGuard 密钥文件结构非法：{self.path}")
        raw_keys = payload.get("keys")
        if not isinstance(raw_keys, Mapping):
            return {}
        result: dict[str, dict[str, str]] = {}
        for role in KEY_ROLES:
            entry = raw_keys.get(role)
            if not isinstance(entry, Mapping):
                continue
            private = str(entry.get("private_key") or "").strip()
            if not private:
                continue
            result[role] = {"private_key": private}
        return result

    def _load_or_recover(self) -> dict[str, dict[str, str]]:
        """读取密钥；文件损坏时改名保留证据并重新生成。"""
        try:
            return self.load()
        except WireGuardConfigError as exc:
            backup = self.path.with_name(
                f"{self.path.name}.corrupt-{int(time.time())}"
            )
            with suppress(OSError):
                self.path.replace(backup)
            logger.warning(
                "WireGuard 密钥文件损坏，已改名保留为 %s 并重新生成：%s", backup, exc
            )
            return {}

    def get_public_key(self, role: str) -> str:
        """读取已存储公钥（不生成、不写盘；不存在/损坏返回空串）。"""
        try:
            keys = self.load()
        except WireGuardConfigError:
            return ""
        entry = keys.get(role) or {}
        text = str(entry.get("public_key") or "").strip()
        return text

    def ensure_all(self) -> dict[str, dict[str, str]]:
        """确保三套密钥都存在，返回 ``{role: {private_key, public_key}}``。

        已有私钥重新推导公钥（公钥可由私钥唯一确定，借此修复公钥字段缺失/漂移）；
        缺失或非法私钥重新生成。变更时原子写回（0600）。
        """
        stored = self._load_or_recover()
        result: dict[str, dict[str, str]] = {}
        changed = False
        for role in KEY_ROLES:
            private = str((stored.get(role) or {}).get("private_key") or "").strip()
            public = ""
            if private:
                try:
                    public = public_key_from_private(private)
                except WireGuardConfigError:
                    private = ""
            if not private:
                private = generate_private_key()
                public = public_key_from_private(private)
                changed = True
            result[role] = {"private_key": private, "public_key": public}
            if (stored.get(role) or {}).get("public_key") != public:
                changed = True
        if changed:
            self.save(result)
        return result

    def save(self, keys: Mapping[str, Mapping[str, str]]) -> None:
        """写回密钥文件（只保留已知角色与字段）。"""
        payload = {
            "version": 1,
            "keys": {
                role: {
                    "private_key": str(keys[role]["private_key"]),
                    "public_key": str(keys[role]["public_key"]),
                }
                for role in KEY_ROLES
                if role in keys
            },
        }
        _write_private_json(self.path, payload)


# ----------------------------------------------------------------------
# 配置快照与 helper 配置 JSON
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class WireGuardSettings:
    """配置卡 ``wireguard`` 的不可变快照。"""

    enabled: bool = False
    mode: str = DEFAULT_MODE
    listen_port: int = DEFAULT_LISTEN_PORT
    interface_address: str = DEFAULT_INTERFACE_ADDRESS[DEFAULT_MODE]
    server_endpoint: str = ""
    public_endpoint: str = ""
    server_public_key: str = ""
    preshared_key: str = ""
    phone_wg_ip: str = DEFAULT_PHONE_WG_IP[DEFAULT_MODE]
    adb_local_port: int = DEFAULT_ADB_LOCAL_PORT
    auto_adb: bool = False
    adb_target_port: int = 5555
    socks_port: int = 0

    @property
    def role(self) -> str:
        """合法角色（非法 mode 回退 client）。"""
        return self.mode if self.mode in MODES else DEFAULT_MODE

    @property
    def effective_interface_address(self) -> str:
        return (
            str(self.interface_address or "").strip()
            or DEFAULT_INTERFACE_ADDRESS[self.role]
        )

    @property
    def effective_phone_wg_ip(self) -> str:
        return str(self.phone_wg_ip or "").strip() or DEFAULT_PHONE_WG_IP[self.role]


def _network_of(address: str) -> ipaddress.IPv4Network:
    text = str(address or "").strip()
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise WireGuardConfigError(
            f"interface_address 不是合法 CIDR：{text!r}（示例 10.66.0.2/24）"
        ) from exc
    if network.version != 4:
        raise WireGuardConfigError(
            f"interface_address 仅支持 IPv4（当前 {text!r}）"
        )
    return network


def parse_endpoint(text: Any, *, required_port: bool = False) -> tuple[str, int]:
    """解析 ``host`` / ``host:port`` / ``[IPv6]:port``；返回 ``(host, port)``。"""
    value = str(text or "").strip()
    if not value:
        if required_port:
            raise WireGuardConfigError("endpoint 为空（client 模式需填写服务器 host:port）")
        return "", 0
    host = ""
    port_text = ""
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise WireGuardConfigError(f"endpoint 方括号不匹配：{value!r}")
        host = value[1:end]
        rest = value[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                raise WireGuardConfigError(f"endpoint 格式非法：{value!r}")
            port_text = rest[1:]
    else:
        head, sep, tail = value.rpartition(":")
        if sep and tail.isdigit():
            host, port_text = head, tail
        else:
            host = value
    host = host.strip()
    if not host or any(char.isspace() for char in host):
        raise WireGuardConfigError(f"endpoint 主机非法：{value!r}")
    port = 0
    if port_text:
        try:
            port = int(port_text)
        except ValueError as exc:
            raise WireGuardConfigError(f"endpoint 端口非法：{value!r}") from exc
        if not 1 <= port <= 65535:
            raise WireGuardConfigError(f"endpoint 端口越界：{port}（应在 1–65535）")
    elif required_port:
        raise WireGuardConfigError(
            f"endpoint 缺少端口：{value!r}（client 模式需填写服务器 host:port）"
        )
    return host, port


def _validated_preshared_key(text: Any) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    return canonical_key(value, label="preshared_key")


def _validate_adb_local_port(settings: WireGuardSettings) -> int:
    try:
        port = int(settings.adb_local_port or 0)
    except (TypeError, ValueError) as exc:
        raise WireGuardConfigError(
            f"adb_local_port 非法：{settings.adb_local_port!r}"
        ) from exc
    if port == 0:
        return 0
    if not 1 <= port <= 65535:
        raise WireGuardConfigError(f"adb_local_port 越界：{port}（0 或 1–65535）")
    return port


def build_helper_config(
    settings: WireGuardSettings,
    keys: Mapping[str, Mapping[str, str]],
    *, app_port: int = 0,
) -> dict[str, Any]:
    """按 helper 契约生成配置 JSON（``helper-contract.md`` 第 2 节）。

    Args:
        settings: wireguard 配置快照。
        keys: :meth:`WireGuardKeyStore.ensure_all` 的结果。

    Raises:
        WireGuardConfigError: 配置/密钥非法（确定性失败，原因可直接展示给用户）。
    """
    mode = settings.mode if settings.mode in MODES else DEFAULT_MODE
    interface_address = settings.effective_interface_address
    network = _network_of(interface_address)
    phone_text = settings.effective_phone_wg_ip
    try:
        phone_ip = ipaddress.ip_address(phone_text)
    except ValueError as exc:
        raise WireGuardConfigError(
            f"phone_wg_ip 不是合法 IPv4 地址：{phone_text!r}"
        ) from exc
    if phone_ip.version != 4:
        raise WireGuardConfigError(f"phone_wg_ip 仅支持 IPv4：{phone_text!r}")
    if phone_ip not in network:
        raise WireGuardConfigError(
            f"phone_wg_ip {phone_ip} 不在 interface_address 网段 {network} 内"
        )
    adb_local_port = 0 if app_port else _validate_adb_local_port(settings)
    if not app_port and not 1 <= settings.adb_target_port <= 65535:
        raise WireGuardConfigError("adb_target_port 需为 1–65535")
    if not 0 <= settings.socks_port <= 65535 or (settings.socks_port and settings.socks_port == adb_local_port):
        raise WireGuardConfigError("socks_port 需为 0–65535 且与 ADB 本地端口不同")
    forwards: list[dict[str, Any]] = []
    if adb_local_port:
        forwards.append(
            {
                "listen": f"127.0.0.1:{adb_local_port}",
                "target": f"{phone_ip}:{settings.adb_target_port}",
            }
        )
    preshared_key = _validated_preshared_key(settings.preshared_key)
    if mode == "client":
        private = str((keys.get("client") or {}).get("private_key") or "").strip()
        if not private:
            raise WireGuardConfigError("缺少 client 私钥（无法生成 helper 配置）")
        server_public = canonical_key(settings.server_public_key, label="server_public_key")
        endpoint = str(settings.server_endpoint or "").strip()
        if not endpoint:
            raise WireGuardConfigError(
                "client 模式需要配置 server_endpoint（要连接的 WireGuard 服务器 IP:port）"
            )
        endpoint_host, endpoint_port = parse_endpoint(endpoint, required_port=True)
        # helper 用 netip.ParseAddrPort 校验 endpoint：只接受 IP:port，不接受域名。
        try:
            endpoint_ip = ipaddress.ip_address(endpoint_host)
        except ValueError as exc:
            raise WireGuardConfigError(
                "client 模式 server_endpoint 主机必须是 IP 地址"
                f"（如 192.168.250.50:51820，当前 {endpoint!r}）"
            ) from exc
        endpoint = (
            f"[{endpoint_host}]:{endpoint_port}"
            if endpoint_ip.version == 6
            else f"{endpoint_host}:{endpoint_port}"
        )
        peers = [
            {
                "public_key": server_public,
                "preshared_key": preshared_key,
                "endpoint": endpoint,
                "allowed_ips": [str(network)],
                "persistent_keepalive_seconds": PERSISTENT_KEEPALIVE_SECONDS,
            }
        ]
        interface = {
            "private_key": private,
            "addresses": [interface_address],
            "listen_port": 0,
            "mtu": DEFAULT_MTU,
        }
    else:
        private = str((keys.get("server") or {}).get("private_key") or "").strip()
        phone_public = str((keys.get("phone") or {}).get("public_key") or "").strip()
        if not private or not phone_public:
            raise WireGuardConfigError("缺少 server/phone 密钥（无法生成 helper 配置）")
        try:
            listen_port = int(settings.listen_port or 0)
        except (TypeError, ValueError) as exc:
            raise WireGuardConfigError(
                f"listen_port 非法：{settings.listen_port!r}"
            ) from exc
        if not 1 <= listen_port <= 65535:
            raise WireGuardConfigError(
                f"server 模式 listen_port 需在 1–65535（当前 {listen_port}）"
            )
        public_endpoint = str(settings.public_endpoint or "").strip()
        if not public_endpoint:
            raise WireGuardConfigError(
                "server 模式需要配置 public_endpoint"
                "（手机侧可访问到本插件的地址，如 <服务器公网地址>:51820）"
            )
        parse_endpoint(public_endpoint)
        peers = [
            {
                "public_key": phone_public,
                "preshared_key": preshared_key,
                "endpoint": "",
                "allowed_ips": [f"{phone_ip}/32"],
                "persistent_keepalive_seconds": 0,
            }
        ]
        interface = {
            "private_key": private,
            "addresses": [interface_address],
            "listen_port": listen_port,
            "mtu": DEFAULT_MTU,
        }
    if type(app_port) is not int or app_port != 0 and not 1024 <= app_port <= 65535:
        raise WireGuardConfigError("App入口端口需为1024–65535")
    return {
        "interface": interface,
        "peers": peers,
        "forwards": forwards,
        "socks_listen": f"127.0.0.1:{settings.socks_port}" if settings.socks_port else "",
        "log_level": "info",
        **({"app_port": app_port, "app_peer": str(phone_ip)} if app_port else {}),
    }


# ----------------------------------------------------------------------
# helper 状态文件解析与健康判定
# ----------------------------------------------------------------------


def load_status_file(path: str | Path) -> tuple[dict[str, Any] | None, str]:
    """读取 helper 状态 JSON；返回 ``(status, error_text)``。"""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "状态文件不存在"
    except OSError as exc:
        return None, f"读取状态文件失败：{exc}"
    if not text.strip():
        return None, "状态文件为空"
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"状态文件不是合法 JSON：{exc}"
    if not isinstance(payload, Mapping):
        return None, "状态文件结构不是 JSON 对象"
    return dict(payload), ""


def status_age_seconds(
    status: Mapping[str, Any] | None,
    path: str | Path,
    *,
    now: float | None = None,
) -> float | None:
    """状态新鲜度：优先 ``updated_at_unix``，缺失时退回文件 mtime。"""
    current = time.time() if now is None else float(now)
    updated = 0.0
    if isinstance(status, Mapping):
        try:
            updated = float(status.get("updated_at_unix") or 0)
        except (TypeError, ValueError):
            updated = 0.0
    if updated > 0:
        return max(0.0, current - updated)
    try:
        return max(0.0, current - Path(path).stat().st_mtime)
    except OSError:
        return None


def evaluate_health(
    *,
    running: bool,
    status: Mapping[str, Any] | None,
    status_error: str = "",
    status_age: float | None = None,
    max_age: float = STATUS_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """按契约判定 helper 健康：进程存活 + 状态文件新鲜且报告 running。"""
    if not running:
        return {
            "ok": False,
            "reason": "helper 进程未运行",
            "status": dict(status) if isinstance(status, Mapping) else None,
            "status_age": status_age,
        }
    if not isinstance(status, Mapping):
        return {
            "ok": False,
            "reason": str(status_error or "状态文件不可用"),
            "status": None,
            "status_age": None,
        }
    if status.get("running") is False:
        return {
            "ok": False,
            "reason": "helper 报告未运行",
            "status": dict(status),
            "status_age": status_age,
        }
    if status_age is None:
        return {
            "ok": False,
            "reason": "状态文件缺少更新时间",
            "status": dict(status),
            "status_age": None,
        }
    if status_age > max_age:
        return {
            "ok": False,
            "reason": f"状态文件已过期（{status_age:.0f} 秒未更新）",
            "status": dict(status),
            "status_age": status_age,
        }
    return {
        "ok": True,
        "reason": "",
        "status": dict(status),
        "status_age": status_age,
    }


def summarize_peers(status: Mapping[str, Any] | None) -> dict[str, Any]:
    """汇总 helper 状态里的对端信息（握手时间/收发字节）。"""
    peers = status.get("peers") if isinstance(status, Mapping) else None
    items = [dict(peer) for peer in peers if isinstance(peer, Mapping)] if isinstance(peers, list) else []
    last_handshake = 0.0
    rx_bytes = 0
    tx_bytes = 0
    for peer in items:
        try:
            last_handshake = max(
                last_handshake, float(peer.get("last_handshake_unix") or 0)
            )
        except (TypeError, ValueError):
            continue
        for key in ("rx_bytes", "tx_bytes"):
            try:
                value = int(peer.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if key == "rx_bytes":
                rx_bytes += value
            else:
                tx_bytes += value
    return {
        "count": len(items),
        "peers": items,
        "last_handshake_unix": last_handshake,
        "rx_bytes": rx_bytes,
        "tx_bytes": tx_bytes,
    }


def summarize_forwards(status: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """汇总 helper 状态里的 TCP 转发信息。"""
    forwards = status.get("forwards") if isinstance(status, Mapping) else None
    if not isinstance(forwards, list):
        return []
    return [dict(item) for item in forwards if isinstance(item, Mapping)]


def _format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        size = 0.0
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024.0 or unit == "GiB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GiB"


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "未知"
    value = max(0.0, float(seconds))
    if value < 60:
        return f"{int(value)} 秒"
    if value < 3600:
        return f"{int(value // 60)} 分钟"
    if value < 86400:
        return f"{value / 3600:.1f} 小时"
    return f"{value / 86400:.1f} 天"


# ----------------------------------------------------------------------
# helper 子进程生命周期
# ----------------------------------------------------------------------


class WireGuardManager:
    """管理 ``phone-wg`` helper 子进程、状态展示与 ADB over WG 切换。"""

    def __init__(
        self,
        *,
        data_dir: str | Path,
        settings_provider: Callable[[], WireGuardSettings] | None = None,
        app_port_provider: Callable[[], int] | None = None,
        adb: Any = None,
        adb_target_provider: Callable[[], Mapping[str, Any]] | None = None,
        config_patch: Callable[..., Any] | None = None,
        blocking_reason: Callable[[], str] | None = None,
        helper_path: str | Path | None = None,
        status_max_age_seconds: float | None = None,
        startup_timeout_seconds: float | None = None,
        stop_timeout_seconds: float | None = None,
        retry_base_seconds: float | None = None,
        retry_max_seconds: float | None = None,
        retry_failure_limit: int | None = None,
    ) -> None:
        self.root = Path(data_dir) / WIREGUARD_DIR_NAME
        self.keys = WireGuardKeyStore(self.root / KEYS_FILE_NAME)
        self.config_path = self.root / HELPER_CONFIG_FILE_NAME
        self.status_path = self.root / HELPER_STATUS_FILE_NAME
        self.helper_path = (
            Path(helper_path)
            if helper_path is not None
            else (
                bundled_phone_wg()
                or Path(__file__).resolve().parents[1] / "bin" / "phone-wg"
            )
        )
        self._settings_provider = settings_provider or (lambda: WireGuardSettings())
        self._app_port_provider = app_port_provider or (lambda: 0)
        self._adb = adb
        self._adb_target_provider = adb_target_provider
        self._config_patch = config_patch
        self._blocking_reason = blocking_reason
        self._status_max_age = float(
            STATUS_MAX_AGE_SECONDS
            if status_max_age_seconds is None
            else status_max_age_seconds
        )
        self._startup_timeout = float(
            STARTUP_TIMEOUT_SECONDS
            if startup_timeout_seconds is None
            else startup_timeout_seconds
        )
        self._stop_timeout = float(
            STOP_TIMEOUT_SECONDS if stop_timeout_seconds is None else stop_timeout_seconds
        )
        self._retry_base = float(
            RETRY_BASE_SECONDS if retry_base_seconds is None else retry_base_seconds
        )
        self._retry_max = float(
            RETRY_MAX_SECONDS if retry_max_seconds is None else retry_max_seconds
        )
        self._retry_failure_limit = int(
            RETRY_FAILURE_LIMIT if retry_failure_limit is None else retry_failure_limit
        )
        self._process_heartbeat = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._started_at = 0.0
        self._last_error = ""
        self._unavailable_reason = ""
        self._failures = 0
        self._next_retry_at = 0.0
        self._adb_switched: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    # ---- 基础状态 -----------------------------------------------------

    def _settings(self) -> WireGuardSettings:
        try:
            return self._settings_provider()
        except Exception as exc:  # noqa: BLE001 - 配置解析异常时按默认（不启动）
            logger.warning("WireGuard：读取配置失败：%s", exc)
            return WireGuardSettings()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def unavailable(self) -> bool:
        """确定性不可用（本次插件运行内重试不会恢复）。"""
        return bool(self._unavailable_reason)

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    @property
    def retry_ready(self) -> bool:
        """临时失败退避是否到期（不可用状态永远为 False）。"""
        return bool(
            not self._unavailable_reason and time.monotonic() >= self._next_retry_at
        )

    @property
    def next_retry_in(self) -> float:
        return max(0.0, self._next_retry_at - time.monotonic())

    @property
    def adb_over_wg(self) -> bool:
        """ADB 目标是否已切到 WG 隧道本地端口。"""
        return self._adb_switched is not None

    def health(self) -> dict[str, Any]:
        """按契约检查进程与状态文件；返回 ``{"ok","reason","status","status_age"}``。"""
        if not self.running:
            return evaluate_health(running=False, status=None)
        status, error = load_status_file(self.status_path)
        age = status_age_seconds(status, self.status_path) if status else None
        return evaluate_health(
            running=True,
            status=status,
            status_error=error,
            status_age=age,
            max_age=self._status_max_age,
        )

    # ---- 失败状态机 ---------------------------------------------------

    def _failure_result(
        self, reason: str, *, unavailable: bool = False
    ) -> dict[str, Any]:
        """构造失败结果；``reason`` 保证为非空字符串。"""
        text = str(reason or "").strip() or "WireGuard helper 启动失败（未提供具体原因）"
        return {
            "ok": False,
            "running": self.running,
            "healthy": False,
            "reason": text,
            "error": self._last_error,
            "unavailable": bool(unavailable or self._unavailable_reason),
            "unavailable_reason": self._unavailable_reason,
            "retry_in": round(self.next_retry_in, 1),
        }

    def _fail_locked(
        self, reason: str, *, unavailable: bool = False
    ) -> dict[str, Any]:
        self._note_failure(reason, unavailable=unavailable)
        return self._failure_result(reason, unavailable=self.unavailable)

    def _mark_unavailable_locked(self, reason: str) -> None:
        text = str(reason or "").strip() or "WireGuard helper 不可用"
        if self._unavailable_reason == text:
            return
        self._unavailable_reason = text
        logger.warning(
            "WireGuard 不可用：%s；本次运行不再自动重试"
            "（/phone wg status 查看，修改配置或 /phone wg start 可重试）",
            text,
        )

    def _note_failure(self, reason: str, *, unavailable: bool = False) -> None:
        text = str(reason or "").strip() or "WireGuard helper 启动失败（未提供具体原因）"
        self._last_error = text
        if unavailable:
            self._mark_unavailable_locked(text)
            return
        self._failures += 1
        if self._failures >= self._retry_failure_limit:
            self._mark_unavailable_locked(
                f"连续 {self._failures} 次失败，本次运行不再自动重试：{text}"
            )
            return
        delay = min(
            self._retry_max, self._retry_base * (2 ** (self._failures - 1))
        )
        self._next_retry_at = time.monotonic() + delay
        logger.warning(
            "WireGuard 启动失败（第 %d 次）：%s；约 %.0f 秒后重试",
            self._failures,
            text,
            delay,
        )

    def _note_success(self) -> None:
        self._last_error = ""
        self._unavailable_reason = ""
        self._failures = 0
        self._next_retry_at = 0.0

    def reset_unavailable(self, reason: str = "") -> None:
        """清除不可用标记/退避，允许再次尝试（配置变化或用户显式启动后调用）。"""
        if (
            not self._unavailable_reason
            and not self._failures
            and not self._next_retry_at
        ):
            return
        previous = self._unavailable_reason
        self._unavailable_reason = ""
        self._failures = 0
        self._next_retry_at = 0.0
        self._last_error = ""
        if previous:
            suffix = f"（{reason}）" if reason else ""
            logger.info(
                "WireGuard：已清除不可用标记，允许重新尝试%s（原原因：%s）",
                suffix,
                previous,
            )

    # ---- ADB over WG --------------------------------------------------

    async def _apply_adb_over_wg_locked(self) -> None:
        """helper 健康且 auto_adb 开启时，把 ADB 目标切到 WG 隧道本地端口。"""
        settings = self._settings()
        if not settings.enabled or not settings.auto_adb or self._adb is None or getattr(self._adb, "app_mode", False):
            return
        if self._adb_switched is not None:
            return
        port = _validate_adb_local_port(settings)
        if not port:
            return
        try:
            await self._adb.reconfigure(
                host="127.0.0.1", port=port, serial=""
            )
        except Exception as exc:  # noqa: BLE001 - 切换失败不阻塞 helper
            logger.warning(
                "WireGuard：自动切换 ADB 目标到 127.0.0.1:%s 失败：%s", port, exc
            )
            return
        self._adb_switched = {"host": "127.0.0.1", "port": port, "serial": ""}
        # 切换后做一次可达自检：隧道内 ADB 不可达时恢复原目标，避免设备操控被
        # 静默切断（例如手机官方 WireGuard App 受 Android VPN 按 UID 分流限制，
        # adbd 回包不走隧道；此时 auto_adb 不应生效）。
        if not await self._probe_adb_over_wg():
            await self._restore_adb_locked()
            logger.warning(
                "WireGuard：auto_adb 自检失败（隧道内无法连接手机 ADB），已恢复原 ADB 目标；"
                "手机官方 WireGuard App 场景属已知限制，请关闭 auto_adb 或改用"
                " Linux/root 全局 WG 对端（详见使用手册「已知限制」）"
            )
            return
        logger.info(
            "WireGuard：ADB 目标已切换到 127.0.0.1:%s（ADB over WG，helper 停止时恢复）",
            port,
        )

    async def _probe_adb_over_wg(self) -> bool:
        """切换 ADB 目标后做一次就绪自检；ADB 客户端无自检能力时按通过处理。"""
        adb = self._adb
        if adb is None:
            return False
        ensure_ready = getattr(adb, "ensure_ready", None)
        if not callable(ensure_ready):
            return True
        try:
            await asyncio.wait_for(ensure_ready(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("WireGuard：ADB over WG 自检超时（15s）")
            return False
        except Exception as exc:  # noqa: BLE001 - 自检失败即回退
            logger.warning("WireGuard：ADB over WG 自检失败：%s", exc)
            return False
        return True

    async def _restore_adb_locked(self) -> None:
        """把 ADB 目标恢复到插件配置的原始 host/port/serial。"""
        if self._adb_switched is None:
            return
        previous: dict[str, Any] = {}
        if self._adb_target_provider is not None:
            try:
                previous = dict(self._adb_target_provider() or {})
            except Exception as exc:  # noqa: BLE001 - 读取原目标失败只记录
                logger.warning("WireGuard：读取原 ADB 目标失败：%s", exc)
        self._adb_switched = None
        if self._adb is None or not str(previous.get("host") or "").strip():
            return
        try:
            await self._adb.reconfigure(
                host=str(previous.get("host") or ""),
                port=int(previous.get("port") or 0),
                serial=str(previous.get("serial") or ""),
            )
            logger.info(
                "WireGuard：ADB 目标已恢复为 %s:%s",
                previous.get("host"),
                previous.get("port"),
            )
        except Exception as exc:  # noqa: BLE001 - 恢复失败给出可操作提示
            logger.warning(
                "WireGuard：恢复原 ADB 目标失败：%s；可在配置页重新保存 ADB 连接参数",
                exc,
            )

    # ---- 进程生命周期 -------------------------------------------------

    def _resolve_helper(self) -> Path:
        helper = Path(self.helper_path)
        if not helper.is_file():
            raise WireGuardConfigError(
                f"未找到 WireGuard helper：{helper}"
                "（插件只随包提供 Linux x86_64 的 phone-wg；其他平台请按 helper/phone-wg "
                "源码编译后放到插件根目录 bin/phone-wg，也可用 /phone wg status 查看平台信息）"
            )
        if not ensure_executable(helper):
            raise WireGuardConfigError(
                f"无法为 WireGuard helper 设置可执行权限：{helper}"
            )
        return helper

    def _stderr_text(self, limit: int = 400) -> str:
        text = "\n".join(self._stderr_tail).strip()
        return text[-limit:] if text else "（无日志）"

    async def _pump_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """逐行转发 helper stderr 到 astrbot logger，并保留最近若干行用于报错。"""
        stream = proc.stderr
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            self._stderr_tail.append(text)
            logger.info("[phone-wg] %s", text)

    async def _teardown_process_locked(self) -> None:
        """停止 helper 进程、取消 stderr 转发并清理状态文件（调用方持锁）。"""
        await stop_heartbeat(self._process_heartbeat)
        self._process_heartbeat = None
        proc = self._proc
        self._proc = None
        task = self._stderr_task
        self._stderr_task = None
        if proc is not None and proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._stop_timeout)
            except (asyncio.TimeoutError, ProcessLookupError):
                with suppress(ProcessLookupError):
                    proc.kill()
                with suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=3)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self._proc is None:
            self._started_at = 0.0
        with suppress(OSError):
            self.status_path.unlink()

    async def _wait_ready_locked(self) -> bool:
        """等待 helper 写出新鲜状态文件（或进程退出）。"""
        deadline = time.monotonic() + max(0.5, self._startup_timeout)
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is None or proc.returncode is not None:
                return False
            status, _error = load_status_file(self.status_path)
            if status is not None:
                age = status_age_seconds(status, self.status_path)
                if (
                    status.get("running", True)
                    and age is not None
                    and age <= self._status_max_age
                ):
                    return True
            await asyncio.sleep(0.1)
        return False

    async def start(self) -> dict[str, Any]:
        """启动 helper（幂等、串行）：返回 ``{"ok","reason",...}``。"""
        async with self._lock:
            try:
                return await self._start_locked()
            except asyncio.CancelledError:
                await self._teardown_process_locked()
                raise
            except WireGuardConfigError as exc:
                await self._teardown_process_locked()
                return self._fail_locked(str(exc), unavailable=True)
            except Exception as exc:  # noqa: BLE001 - 未预期异常也要给出明确原因
                await self._teardown_process_locked()
                return self._fail_locked(
                    f"{type(exc).__name__}: WireGuard 启动异常：{exc}"
                )

    async def _start_locked(self) -> dict[str, Any]:
        if self._blocking_reason and (reason := self._blocking_reason()):
            self._last_error = reason
            return self._failure_result(reason)
        settings = self._settings()
        if self.running:
            return self.status()
        if not settings.enabled:
            return self._failure_result(
                "WireGuard 未启用（请在组网配置中开启并选择 WireGuard）"
            )
        if self._unavailable_reason:
            return self._failure_result(
                self._unavailable_reason, unavailable=True
            )
        if time.monotonic() < self._next_retry_at:
            return self._failure_result(
                self._last_error or "WireGuard 启动失败，等待退避重试"
            )
        helper = self._resolve_helper()
        keys = self.keys.ensure_all()
        config = build_helper_config(settings, keys, app_port=self._app_port_provider())
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_private_json(self.config_path, config)
        # 清掉上次运行可能遗留的进程句柄与状态文件。
        await self._teardown_process_locked()
        self._stderr_tail.clear()
        try:
            command = [str(helper), "run", "--config", str(self.config_path),
                       "--status-file", str(self.status_path)]
            self._proc = await asyncio.create_subprocess_exec(
                *guarded_command(command, self.root / "process.lock"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._process_heartbeat = start_heartbeat(self._proc)
        except (OSError, asyncio.SubprocessError) as exc:
            deterministic = isinstance(
                exc, (FileNotFoundError, PermissionError, NotADirectoryError)
            )
            return self._fail_locked(
                f"{type(exc).__name__}: 启动 WireGuard helper 失败：{exc}",
                unavailable=deterministic,
            )
        self._stderr_task = asyncio.create_task(
            self._pump_stderr(self._proc),
            name=f"{PLUGIN_NAME}_wireguard_stderr",
        )
        if not await self._wait_ready_locked():
            proc = self._proc
            returncode = proc.returncode if proc is not None else None
            tail = self._stderr_text()
            await self._teardown_process_locked()
            if returncode is not None:
                deterministic = returncode in _DETERMINISTIC_EXIT_CODES
                code_text = (
                    "配置非法"
                    if returncode == EXIT_CODE_CONFIG_ERROR
                    else "建 WG 接口失败"
                    if returncode == EXIT_CODE_INTERFACE_ERROR
                    else "非预期退出"
                )
                return self._fail_locked(
                    f"helper 启动失败（退出码 {returncode}，{code_text}）：{tail}",
                    unavailable=deterministic,
                )
            return self._fail_locked(
                f"helper 启动后 {self._startup_timeout:.0f} 秒内未写出健康状态：{tail}"
            )
        self._started_at = time.time()
        self._note_success()
        public_key = str(
            (keys.get("client") or {}).get("public_key")
            if settings.mode == "client"
            else (keys.get("server") or {}).get("public_key")
        )
        logger.info(
            "WireGuard：helper 已启动（%s 模式，PUBLICKEY %s，配置 %s）",
            settings.mode,
            public_key or "?",
            self.config_path,
        )
        await self._apply_adb_over_wg_locked()
        return self.status()

    async def stop(self) -> None:
        """停止 helper 并恢复 ADB 目标（幂等、串行）。"""
        async with self._lock:
            was_running = self.running
            await self._restore_adb_locked()
            await self._teardown_process_locked()
            if was_running:
                logger.info("WireGuard：helper 已停止")

    async def restart(self) -> dict[str, Any]:
        """按当前配置重启 helper。"""
        await self.stop()
        return await self.start()

    async def ensure_healthy(self) -> dict[str, Any]:
        """保活检查：进程存活且状态文件新鲜；异常时停止并交给上层退避重启。"""
        async with self._lock:
            if not self.running:
                await self._restore_adb_locked()
                await self._teardown_process_locked()
                return self._fail_locked("helper 进程已退出")
            health = self.health()
            if not health.get("ok"):
                reason = str(health.get("reason") or "helper 状态异常")
                await self._restore_adb_locked()
                await self._teardown_process_locked()
                return self._fail_locked(f"helper 状态异常：{reason}")
            await self._apply_adb_over_wg_locked()
            return self.status()

    # ---- 状态展示 -----------------------------------------------------

    def _status_document(self) -> tuple[dict[str, Any] | None, str]:
        return load_status_file(self.status_path)

    def status(self) -> dict[str, Any]:
        """汇总 helper/配置/对端/转发状态（不访问设备）。"""
        settings = self._settings()
        health = self.health()
        document = health.get("status")
        if not isinstance(document, Mapping):
            document, _error = self._status_document()
        peers = summarize_peers(document)
        forwards = summarize_forwards(document)
        interface = document.get("interface") if isinstance(document, Mapping) else None
        interface = dict(interface) if isinstance(interface, Mapping) else {}
        stored_public = self.keys.get_public_key(settings.role)
        return {
            "ok": bool(self.running and health.get("ok")),
            "enabled": bool(settings.enabled),
            "mode": settings.role,
            "running": self.running,
            "healthy": bool(health.get("ok")),
            "health_reason": str(health.get("reason") or ""),
            "status_age": health.get("status_age"),
            "helper": str(self.helper_path),
            "helper_exists": Path(self.helper_path).is_file(),
            "config_path": str(self.config_path),
            "status_path": str(self.status_path),
            "keys_path": str(self.keys.path),
            "version": str(document.get("version") or "") if document else "",
            "public_key": str(interface.get("public_key") or stored_public),
            "interface_addresses": list(interface.get("addresses") or []),
            "listen_port": int(interface.get("listen_port") or 0),
            "configured_listen_port": int(settings.listen_port or 0),
            "server_endpoint": str(settings.server_endpoint or ""),
            "public_endpoint": str(settings.public_endpoint or ""),
            "phone_wg_ip": settings.effective_phone_wg_ip,
            "adb_local_port": int(settings.adb_local_port or 0),
            "adb_target_port": settings.adb_target_port,
            "socks_port": settings.socks_port,
            "auto_adb": bool(settings.auto_adb),
            "adb_over_wg": self.adb_over_wg,
            "peers": peers["peers"],
            "peer_count": peers["count"],
            "last_handshake_unix": peers["last_handshake_unix"],
            "rx_bytes": peers["rx_bytes"],
            "tx_bytes": peers["tx_bytes"],
            "forwards": forwards,
            "app_port": int(document.get("app_port") or 0) if document and self.running and health.get("ok") else 0,
            "socks_listen": f"127.0.0.1:{settings.socks_port}" if settings.socks_port else "",
            "error": self._last_error,
            "unavailable": self.unavailable,
            "unavailable_reason": self._unavailable_reason,
            "retry_in": round(self.next_retry_in, 1),
            "started_at": self._started_at,
        }

    def brief_text(self) -> str:
        """``/phone status`` 中的一行摘要。"""
        status = self.status()
        if not status["enabled"]:
            return "WireGuard：未启用（组网配置未选择或启用 WireGuard）"
        if status["running"] and not status["healthy"]:
            return (
                "WireGuard：helper 运行中但状态异常"
                f"（{status['health_reason']}；/phone wg status 查看）"
            )
        if status["running"]:
            peers = status["peer_count"]
            handshake = float(status["last_handshake_unix"] or 0)
            if handshake > 0:
                handshake_text = (
                    f"，最近握手 {_format_age(time.time() - handshake)}前"
                )
            else:
                handshake_text = "，尚未握手"
            return (
                f"WireGuard：运行中（{status['mode']} 模式，对端 {peers} 个"
                f"{handshake_text}）"
            )
        if status["unavailable"]:
            return f"WireGuard：不可用（{status['unavailable_reason']}）"
        reason = status.get("error") or status.get("health_reason") or ""
        suffix = f"（{reason}）" if reason else ""
        return f"WireGuard：未运行{suffix}"

    def status_text(self) -> str:
        """``/phone wg status`` 的完整中文状态。"""
        status = self.status()
        settings = self._settings()
        lines = ["WireGuard 组网："]
        if status["enabled"]:
            lines.append(
                f"- 配置：已启用（{status['mode']} 模式，"
                f"ADB 隧道端口 {status['adb_local_port'] or '未启用'}，"
                f"auto_adb={'开' if status['auto_adb'] else '关'}）"
            )
        else:
            lines.append("- 配置：已关闭（可在组网配置选择 WireGuard，或用 /phone wg start 开启）")
        if status["running"] and status["healthy"]:
            age = status.get("status_age")
            lines.append(
                "- 运行：运行中"
                + (f"（状态 {_format_age(age)}前更新）" if age is not None else "")
            )
        elif status["running"]:
            lines.append(f"- 运行：helper 运行中但状态异常（{status['health_reason']}）")
        elif status["unavailable"]:
            lines.append(f"- 运行：不可用（{status['unavailable_reason']}）")
        else:
            reason = status.get("error") or status.get("health_reason") or ""
            lines.append("- 运行：未运行" + (f"（{reason}）" if reason else ""))
        if status["public_key"]:
            lines.append(
                f"- 本机：{status['mode']} 角色，接口地址 "
                f"{settings.effective_interface_address}，公钥 {status['public_key']}"
            )
        else:
            lines.append(
                f"- 本机：{status['mode']} 角色，接口地址 "
                f"{settings.effective_interface_address}（密钥尚未生成）"
            )
        if status["mode"] == "server":
            effective_listen = status["listen_port"] or status["configured_listen_port"]
        else:
            effective_listen = status["listen_port"]
        lines.append(
            f"- 监听：UDP {effective_listen or '未分配'}"
            + ("（client 模式由系统分配）" if status["mode"] == "client" else "")
        )
        if status["mode"] == "server":
            lines.append(
                f"- 服务器：手机端地址 {settings.effective_phone_wg_ip}，"
                f"Endpoint {status['public_endpoint'] or '（未配置）'}"
            )
        else:
            lines.append(
                f"- 服务器：{status['server_endpoint'] or '（未配置）'}"
                + (f"，手机地址 {status['phone_wg_ip']}" if status["phone_wg_ip"] else "")
            )
        handshake = float(status["last_handshake_unix"] or 0)
        peer_line = f"- 对端：{status['peer_count']} 个"
        if handshake > 0:
            peer_line += f"，最近握手 {_format_age(time.time() - handshake)}前"
        else:
            peer_line += "，尚未握手"
        if status["peer_count"]:
            peer_line += (
                f"，rx {_format_bytes(status['rx_bytes'])} / "
                f"tx {_format_bytes(status['tx_bytes'])}"
            )
        lines.append(peer_line)
        forward = status["forwards"][0] if status["forwards"] else None
        if forward:
            lines.append(
                "- 转发："
                f"{forward.get('listen') or '?'} → {forward.get('target') or '?'}"
                f"（活跃 {int(forward.get('active_connections') or 0)} / "
                f"累计 {int(forward.get('total_connections') or 0)}）"
            )
            last_forward_error = str(forward.get("last_error") or "").strip()
            if last_forward_error:
                lines.append(f"- 转发错误：{last_forward_error}")
        elif status["adb_local_port"]:
            lines.append(
                f"- 转发：未运行（将监听 127.0.0.1:{status['adb_local_port']} → "
                f"{status['phone_wg_ip']}:{settings.adb_target_port}）"
            )
        else:
            lines.append("- 转发：未启用（adb_local_port=0，ADB over WG 关闭）")
        if status["auto_adb"]:
            if status["adb_over_wg"]:
                lines.append(
                    f"- ADB：已切换到 127.0.0.1:{status['adb_local_port']}"
                    "（helper 停止时恢复原目标）"
                )
            else:
                lines.append("- ADB：未切换（helper 就绪且 auto_adb 开启后才切换）")
        last_error = str(status.get("error") or "").strip()
        if last_error and last_error not in str(status.get("unavailable_reason") or ""):
            lines.append(f"- 最近错误：{last_error}")
        if status["unavailable"]:
            lines.append(
                "- 重试：本次运行不再自动重试；修改 wireguard 配置保存或 "
                "/phone wg start 可重新尝试"
            )
        elif status["retry_in"] > 0:
            lines.append(f"- 重试：约 {int(status['retry_in'])} 秒后重试")
        lines.append(f"- helper：{status['helper']}")
        if not status["helper_exists"]:
            lines.append("  （helper 不存在：仅 Linux x86_64 随包 phone-wg；其他平台可自行编译后放到插件根目录 bin/phone-wg）")
        version = status.get("version") or ""
        if version:
            lines.append(f"  helper 版本：{version}")
        return "\n".join(lines)

    def config_text(self) -> str:
        """``/phone wg config`` 的导入用配置文本。

        client 模式输出「服务器侧需要添加的 Peer 片段」；server 模式输出手机端
        完整 WireGuard 配置（可复制导入手机 App）。配置非法时抛
        :class:`WireGuardConfigError`。
        """
        settings = self._settings()
        keys = self.keys.ensure_all()
        config = build_helper_config(settings, keys)
        mode = settings.mode if settings.mode in MODES else DEFAULT_MODE
        interface = dict(config.get("interface") or {})
        addresses = list(interface.get("addresses") or [])
        interface_address = (
            addresses[0] if addresses else settings.effective_interface_address
        )
        network = _network_of(str(interface_address))
        preshared_key = str((config.get("peers") or [{}])[0].get("preshared_key") or "")
        if settings.adb_local_port:
            adb_line = (
                f"ADB over WG：127.0.0.1:{settings.adb_local_port} → "
                f"{settings.effective_phone_wg_ip}:{settings.adb_target_port}"
            )
        else:
            adb_line = "ADB over WG：未启用（adb_local_port=0）"
        if mode == "client":
            client_public = str((keys.get("client") or {}).get("public_key") or "")
            client_ip = ipaddress.ip_interface(interface_address).ip
            lines = [
                "WireGuard 客户端配置（插件 → 服务器）",
                "- 模式：client",
                f"- 接口地址：{interface_address}",
                f"- 服务器：{settings.server_endpoint}",
                f"- 本机公钥：{client_public}",
                f"- {adb_line}",
                "",
                "服务器侧（WireGuard server）需要为本插件添加的 Peer：",
                "[Peer]",
                f"PublicKey = {client_public}",
                *([f"PresharedKey = {preshared_key}"] if preshared_key else []),
                f"AllowedIPs = {client_ip}/32",
            ]
            return "\n".join(lines)
        status_document, _status_error = self._status_document()
        server_public = ""
        if isinstance(status_document, Mapping):
            raw_interface = status_document.get("interface")
            if isinstance(raw_interface, Mapping):
                server_public = str(raw_interface.get("public_key") or "")
        server_public = server_public or str(
            (keys.get("server") or {}).get("public_key") or ""
        )
        phone_private = str((keys.get("phone") or {}).get("private_key") or "")
        phone_ip = ipaddress.ip_address(settings.effective_phone_wg_ip)
        listen_port = int(interface.get("listen_port") or settings.listen_port)
        lines = [
            "WireGuard 服务端配置（手机 → 插件）",
            "- 模式：server",
            f"- 监听端口：{listen_port}（需在容器/网关暴露 UDP {listen_port}）",
            f"- 接口地址：{interface_address}",
            f"- 本机公钥（手机端 Peer PublicKey）：{server_public}",
            f"- {adb_line}",
            "",
            "手机端 WireGuard 配置（复制导入手机 App）：",
            "[Interface]",
            f"PrivateKey = {phone_private}",
            f"Address = {phone_ip}/{network.prefixlen}",
            "",
            "[Peer]",
            f"PublicKey = {server_public}",
            *([f"PresharedKey = {preshared_key}"] if preshared_key else []),
            f"Endpoint = {self._phone_endpoint(settings, config)}",
            f"AllowedIPs = {network}",
            f"PersistentKeepalive = {PERSISTENT_KEEPALIVE_SECONDS}",
        ]
        return "\n".join(lines)

    def _phone_endpoint(
        self, settings: WireGuardSettings, config: Mapping[str, Any]
    ) -> str:
        """server 模式手机端 Endpoint：配置可只填 host，端口取 listen_port。"""
        listen_port = int(
            (config.get("interface") or {}).get("listen_port")
            or settings.listen_port
            or DEFAULT_LISTEN_PORT
        )
        text = str(settings.public_endpoint or "").strip()
        if not text:
            return f"<服务器公网地址>:{listen_port}"
        host, port = parse_endpoint(text)
        return f"{host}:{port or listen_port}"

    # ---- 命令/工具动作 -------------------------------------------------

    _ACTION_ALIASES = {
        "状态": "status",
        "配置": "config",
        "开启": "start",
        "启动": "start",
        "停止": "stop",
        "关闭": "stop",
        "重启": "restart",
    }

    async def control(self, action: str) -> dict[str, Any]:
        """命令/工具共用的动作入口（status/start/stop/restart/config）。"""
        name = str(action or "status").strip().lower()
        name = self._ACTION_ALIASES.get(name, name)
        if name == "status":
            return {"ok": True, "action": "status", "text": self.status_text()}
        if name == "config":
            try:
                text = self.config_text()
            except WireGuardConfigError as exc:
                return {"ok": False, "action": "config", "text": f"error: {exc}"}
            return {"ok": True, "action": "config", "text": text}
        if name not in {"start", "stop", "restart"}:
            return {
                "ok": False,
                "action": name,
                "text": (
                    f"error: 未知动作 {action!r}，可用 status/start/stop/restart/config"
                ),
            }
        if name in {"start", "restart"} and self._blocking_reason and (reason := self._blocking_reason()):
            return {"ok": False, "action": name, "text": "error: " + reason}
        settings = self._settings()
        if name in {"start", "restart"} and not settings.enabled:
            await self._persist_enabled(True)
            settings = self._settings()
        if name == "stop":
            if settings.enabled:
                await self._persist_enabled(False)
            else:
                await self.stop()
            return {"ok": True, "action": "stop", "text": self.status_text()}
        self.reset_unavailable("用户显式操作")
        if name == "restart":
            await self.restart()
        elif not self.running:
            await self.start()
        return {
            "ok": bool(self.running),
            "action": name,
            "text": self.status_text(),
        }

    async def _persist_enabled(self, enabled: bool) -> None:
        """把 enabled 写回插件配置（配置补丁由插件注入；缺失时直接启停）。"""
        if self._config_patch is not None:
            try:
                await self._config_patch({"wireguard": {"enabled": bool(enabled)}})
                return
            except Exception as exc:  # noqa: BLE001 - 补丁失败退化为直接启停
                logger.warning("WireGuard：保存启用配置失败：%s", exc)
        if not enabled:
            await self.stop()
