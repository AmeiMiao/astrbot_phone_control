"""WireGuard helper 生命周期测试：用 fake helper 脚本覆盖启动/停止/失败分流/ADB 切换。

fake helper 完全按 ``helper-contract.md`` 的行为实现（参数、状态文件、退出码），
但不建立真实 WireGuard 接口，因此本文件在本机无 AstrBot/Go 环境下即可运行；
真实 helper 的端到端集成由 UAT 容器验证。
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import time
from pathlib import Path

import pytest

from core.wireguard import (
    DEFAULT_ADB_LOCAL_PORT,
    WireGuardManager,
    WireGuardSettings,
    generate_private_key,
    public_key_from_private,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = PLUGIN_ROOT / "core" / "plugin" / "bootstrap.py"
RUNTIME_PATH = PLUGIN_ROOT / "core" / "plugin" / "runtime.py"

SERVER_PUBLIC = public_key_from_private(generate_private_key())

FAKE_HELPER_SOURCE = '''#!/usr/bin/env python3
"""测试用 phone-wg helper 桩：按契约写状态文件，不建立真实 WireGuard 接口。"""
import json
import os
import signal
import sys
import time

MODE = "__MODE__"
MARKER = "__MARKER__"

config_path = ""
status_path = ""
args = sys.argv[1:]
index = 0
while index < len(args):
    if args[index] == "--config" and index + 1 < len(args):
        config_path = args[index + 1]
        index += 2
    elif args[index] == "--status-file" and index + 1 < len(args):
        status_path = args[index + 1]
        index += 2
    else:
        index += 1

with open(MARKER, "a", encoding="utf-8") as handle:
    handle.write("spawn\\n")

if MODE == "config_error":
    sys.stderr.write("invalid config: bad private key\\n")
    sys.stderr.flush()
    sys.exit(2)
if MODE == "interface_error":
    sys.stderr.write("wireguard-go: interface denied\\n")
    sys.stderr.flush()
    sys.exit(3)
if MODE == "crash":
    sys.stderr.write("boom\\n")
    sys.stderr.flush()
    sys.exit(1)

with open(config_path, "r", encoding="utf-8") as handle:
    config = json.load(handle)

stopping = {"value": False}


def _handle_signal(signum, frame):
    stopping["value"] = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

written = 0
while not stopping["value"]:
    if MODE == "stale" and written:
        time.sleep(0.2)
        continue
    now = int(time.time())
    peers = config.get("peers") or []
    payload = {
        "running": True,
        "version": "0.1.0-fake",
        "interface": {
            "listen_port": int(config["interface"].get("listen_port") or 51820),
            "addresses": list(config["interface"].get("addresses") or []),
            "public_key": "fake-interface-public-key",
        },
        "peers": [
            {
                "public_key": peers[0].get("public_key", ""),
                "endpoint": "192.168.250.10:16448",
                "allowed_ips": ["10.66.0.0/24"],
                "last_handshake_unix": now - 3,
                "rx_bytes": 120,
                "tx_bytes": 340,
            }
        ]
        if peers
        else [],
        "forwards": [
            {
                "listen": item.get("listen", ""),
                "target": item.get("target", ""),
                "active_connections": 1,
                "total_connections": 7,
                "last_error": "",
            }
            for item in config.get("forwards") or []
        ],
        "updated_at_unix": time.time(),
    }
    temporary = status_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(temporary, status_path)
    written += 1
    time.sleep(0.2)
sys.exit(0)
'''


def _write_fake_helper(tmp_path: Path, mode: str) -> Path:
    helper = tmp_path / "phone-wg"
    marker = tmp_path / "spawn.log"
    helper.write_text(
        FAKE_HELPER_SOURCE.replace("__MODE__", mode).replace("__MARKER__", str(marker)),
        encoding="utf-8",
    )
    helper.chmod(0o755)
    return helper


def _spawn_count(tmp_path: Path) -> int:
    marker = tmp_path / "spawn.log"
    if not marker.exists():
        return 0
    return len([line for line in marker.read_text(encoding="utf-8").splitlines() if line])


def _client_settings(**overrides) -> WireGuardSettings:
    base = dict(
        enabled=True,
        mode="client",
        listen_port=51820,
        interface_address="10.66.0.2/24",
        server_endpoint="192.168.250.50:51820",
        server_public_key=SERVER_PUBLIC,
        phone_wg_ip="10.66.0.3",
        adb_local_port=DEFAULT_ADB_LOCAL_PORT,
        auto_adb=False,
    )
    base.update(overrides)
    return WireGuardSettings(**base)


def _make_manager(
    tmp_path: Path,
    settings: WireGuardSettings,
    *,
    helper_path: Path | None = None,
    adb=None,
    adb_target_provider=None,
    **kwargs,
) -> WireGuardManager:
    return WireGuardManager(
        data_dir=tmp_path / "data",
        settings_provider=lambda: settings,
        helper_path=helper_path if helper_path is not None else tmp_path / "phone-wg",
        adb=adb,
        adb_target_provider=adb_target_provider,
        **kwargs,
    )


class FakeAdb:
    """记录 reconfigure 调用的假 ADB 客户端。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def reconfigure(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"changed": True}


# ----------------------------------------------------------------------
# 正常生命周期
# ----------------------------------------------------------------------


def test_start_stop_and_status_fields(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "ok")
    manager = _make_manager(tmp_path, _client_settings())

    async def scenario() -> None:
        result = await manager.start()
        assert result["ok"] is True, result
        assert manager.running is True
        status = manager.status()
        assert status["healthy"] is True
        assert status["version"] == "0.1.0-fake"
        assert status["peer_count"] == 1
        assert status["last_handshake_unix"] > 0
        assert status["tx_bytes"] == 340
        assert status["forwards"][0]["active_connections"] == 1
        assert status["adb_over_wg"] is False
        text = manager.status_text()
        assert "运行中" in text
        assert "对端：1 个" in text
        assert "127.0.0.1:15555" in text
        await manager.stop()
        assert manager.running is False
        assert manager.health()["ok"] is False

    asyncio.run(scenario())
    assert _spawn_count(tmp_path) == 1


def test_restart_respawns_helper(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "ok")
    manager = _make_manager(tmp_path, _client_settings())

    async def scenario() -> None:
        assert (await manager.start())["ok"] is True
        assert (await manager.restart())["ok"] is True
        assert manager.running is True
        await manager.stop()

    asyncio.run(scenario())
    assert _spawn_count(tmp_path) == 2


def test_start_is_idempotent_while_running(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "ok")
    manager = _make_manager(tmp_path, _client_settings())

    async def scenario() -> None:
        assert (await manager.start())["ok"] is True
        assert (await manager.start())["ok"] is True
        await manager.stop()

    asyncio.run(scenario())
    assert _spawn_count(tmp_path) == 1


# ----------------------------------------------------------------------
# 不启动子进程的场景
# ----------------------------------------------------------------------


def test_disabled_settings_never_spawn(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "ok")
    manager = _make_manager(tmp_path, _client_settings(enabled=False))

    result = asyncio.run(manager.start())
    assert result["ok"] is False
    assert "未启用" in result["reason"]
    assert _spawn_count(tmp_path) == 0


def test_missing_helper_is_deterministic_and_not_retried(tmp_path: Path) -> None:
    manager = _make_manager(
        tmp_path,
        _client_settings(),
        helper_path=tmp_path / "missing-phone-wg",
    )

    async def scenario() -> None:
        first = await manager.start()
        assert first["ok"] is False
        assert first["unavailable"] is True
        assert "未找到 WireGuard helper" in first["reason"]
        assert manager.retry_ready is False
        second = await manager.start()
        assert second["ok"] is False
        assert second["unavailable"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mode", "code_text"),
    [("config_error", "退出码 2"), ("interface_error", "退出码 3")],
)
def test_deterministic_exit_codes_mark_unavailable(
    tmp_path: Path, mode: str, code_text: str
) -> None:
    _write_fake_helper(tmp_path, mode)
    manager = _make_manager(tmp_path, _client_settings())

    async def scenario() -> None:
        result = await manager.start()
        assert result["ok"] is False
        assert result["unavailable"] is True
        assert code_text in result["reason"]
        # 确定性失败只告警一次并停止重试：再次 start 不再拉起子进程。
        again = await manager.start()
        assert again["unavailable"] is True

    asyncio.run(scenario())
    assert _spawn_count(tmp_path) == 1


def test_invalid_helper_config_does_not_spawn(tmp_path: Path) -> None:
    """配置不完整（client 缺服务器公钥）时先校验失败，不启动子进程。"""
    _write_fake_helper(tmp_path, "ok")
    manager = _make_manager(
        tmp_path, _client_settings(server_public_key="")
    )

    result = asyncio.run(manager.start())
    assert result["ok"] is False
    assert result["unavailable"] is True
    assert "server_public_key" in result["reason"]
    assert _spawn_count(tmp_path) == 0


# ----------------------------------------------------------------------
# 临时失败退避
# ----------------------------------------------------------------------


def test_temporary_failure_backs_off(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "crash")
    manager = _make_manager(
        tmp_path, _client_settings(), retry_base_seconds=30.0
    )

    async def scenario() -> None:
        first = await manager.start()
        assert first["ok"] is False
        assert first["unavailable"] is False
        assert first["retry_in"] > 0
        # 退避未到期：再次 start 直接返回，不再拉起子进程。
        second = await manager.start()
        assert second["ok"] is False
        assert second["retry_in"] > 0

    asyncio.run(scenario())
    assert _spawn_count(tmp_path) == 1


def test_consecutive_failures_converge_to_unavailable(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "crash")
    manager = _make_manager(
        tmp_path,
        _client_settings(),
        retry_base_seconds=0.0,
        retry_failure_limit=2,
    )

    async def scenario() -> None:
        first = await manager.start()
        assert first["unavailable"] is False
        second = await manager.start()
        assert second["ok"] is False
        assert second["unavailable"] is True
        assert "连续 2 次失败" in second["unavailable_reason"]
        third = await manager.start()
        assert third["unavailable"] is True
        manager.reset_unavailable("测试重置")
        assert manager.unavailable is False

    asyncio.run(scenario())
    assert _spawn_count(tmp_path) == 2


# ----------------------------------------------------------------------
# 健康判定（状态文件过期）
# ----------------------------------------------------------------------


def test_stale_status_is_unhealthy_and_stops_helper(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "stale")
    manager = _make_manager(
        tmp_path,
        _client_settings(),
        status_max_age_seconds=0.5,
        retry_base_seconds=30.0,
    )

    async def scenario() -> None:
        assert (await manager.start())["ok"] is True
        await asyncio.sleep(1.0)
        health = manager.health()
        assert health["ok"] is False
        assert "过期" in health["reason"]
        result = await manager.ensure_healthy()
        assert result["ok"] is False
        assert manager.running is False
        assert manager.unavailable is False
        assert result["retry_in"] > 0

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# ADB over WG
# ----------------------------------------------------------------------


ORIGINAL_ADB_TARGET = {"host": "192.168.250.10", "port": 16448, "serial": ""}


def test_auto_adb_switch_and_restore(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "ok")
    adb = FakeAdb()
    manager = _make_manager(
        tmp_path,
        _client_settings(auto_adb=True),
        adb=adb,
        adb_target_provider=lambda: dict(ORIGINAL_ADB_TARGET),
    )

    async def scenario() -> None:
        assert (await manager.start())["ok"] is True
        assert adb.calls == [{"host": "127.0.0.1", "port": 15555, "serial": ""}]
        assert manager.adb_over_wg is True
        await manager.stop()
        assert adb.calls[-1] == dict(ORIGINAL_ADB_TARGET)
        assert manager.adb_over_wg is False

    asyncio.run(scenario())


class FakeAdbUnreachable(FakeAdb):
    """reconfigure 成功但隧道内 ADB 不可达的假客户端。"""

    def __init__(self) -> None:
        super().__init__()
        self.probes = 0

    async def ensure_ready(self) -> None:
        self.probes += 1
        raise RuntimeError("connect failed: 127.0.0.1:15555")

    async def run(self, args, *, timeout=None):  # pragma: no cover - 兜底
        raise RuntimeError("unreachable")


def test_auto_adb_self_check_failure_restores_target(tmp_path: Path) -> None:
    """auto_adb 切换后自检不通：必须恢复原 ADB 目标，避免设备操控被静默切断。"""
    _write_fake_helper(tmp_path, "ok")
    adb = FakeAdbUnreachable()
    manager = _make_manager(
        tmp_path,
        _client_settings(auto_adb=True),
        adb=adb,
        adb_target_provider=lambda: dict(ORIGINAL_ADB_TARGET),
    )

    async def scenario() -> None:
        assert (await manager.start())["ok"] is True
        assert adb.probes == 1
        assert adb.calls[0] == {"host": "127.0.0.1", "port": 15555, "serial": ""}
        assert adb.calls[-1] == dict(ORIGINAL_ADB_TARGET)
        assert manager.adb_over_wg is False
        await manager.stop()

    asyncio.run(scenario())


def test_auto_adb_off_does_not_touch_adb(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "ok")
    adb = FakeAdb()
    manager = _make_manager(
        tmp_path,
        _client_settings(auto_adb=False),
        adb=adb,
        adb_target_provider=lambda: dict(ORIGINAL_ADB_TARGET),
    )

    async def scenario() -> None:
        assert (await manager.start())["ok"] is True
        assert adb.calls == []
        await manager.stop()

    asyncio.run(scenario())


def test_auto_adb_skipped_when_forward_disabled(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "ok")
    adb = FakeAdb()
    manager = _make_manager(
        tmp_path,
        _client_settings(auto_adb=True, adb_local_port=0),
        adb=adb,
        adb_target_provider=lambda: dict(ORIGINAL_ADB_TARGET),
    )

    async def scenario() -> None:
        assert (await manager.start())["ok"] is True
        assert adb.calls == []
        await manager.stop()

    asyncio.run(scenario())


def test_unhealthy_helper_restores_adb_target(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "stale")
    adb = FakeAdb()
    manager = _make_manager(
        tmp_path,
        _client_settings(auto_adb=True),
        adb=adb,
        adb_target_provider=lambda: dict(ORIGINAL_ADB_TARGET),
        status_max_age_seconds=0.5,
    )

    async def scenario() -> None:
        assert (await manager.start())["ok"] is True
        assert manager.adb_over_wg is True
        await asyncio.sleep(1.0)
        await manager.ensure_healthy()
        assert manager.adb_over_wg is False
        assert adb.calls[-1] == dict(ORIGINAL_ADB_TARGET)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 命令/工具动作入口
# ----------------------------------------------------------------------


def test_control_actions(tmp_path: Path) -> None:
    _write_fake_helper(tmp_path, "ok")
    manager = _make_manager(tmp_path, _client_settings())

    async def scenario() -> None:
        status = await manager.control("status")
        assert status["ok"] is True and "WireGuard 组网" in status["text"]
        config = await manager.control("config")
        assert config["ok"] is True and "服务器侧" in config["text"]
        unknown = await manager.control("nonsense")
        assert unknown["ok"] is False and unknown["text"].startswith("error:")
        started = await manager.control("start")
        assert started["ok"] is True and manager.running is True
        stopped = await manager.control("stop")
        assert stopped["ok"] is True and manager.running is False

    asyncio.run(scenario())


def test_control_start_stop_persists_enabled(tmp_path: Path) -> None:
    """命令/工具显式 start/stop 会先写回 wireguard.enabled（插件热应用负责启停）。"""
    _write_fake_helper(tmp_path, "ok")
    state = {"settings": _client_settings(enabled=False)}
    patches: list[dict] = []
    manager = _make_manager(
        tmp_path,
        state["settings"],
        retry_base_seconds=0.0,
    )
    manager._settings_provider = lambda: state["settings"]

    async def config_patch(patch: dict) -> list[str]:
        patches.append(dict(patch))
        enabled = bool((patch.get("wireguard") or {}).get("enabled"))
        state["settings"] = dataclasses.replace(state["settings"], enabled=enabled)
        if enabled:
            await manager.start()
        else:
            await manager.stop()
        return []

    manager._config_patch = config_patch

    async def scenario() -> None:
        started = await manager.control("start")
        assert started["ok"] is True and manager.running is True
        assert patches[0] == {"wireguard": {"enabled": True}}
        stopped = await manager.control("stop")
        assert stopped["ok"] is True and manager.running is False
        assert patches[1] == {"wireguard": {"enabled": False}}

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 插件接线（AST 级，本机无 AstrBot 依赖即可验证 terminate 会停止 helper）
# ----------------------------------------------------------------------


def _class_node(path: Path, name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 缺少类 {name}")


def _method_node(klass: ast.ClassDef, name: str) -> ast.AST:
    for node in klass.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{klass.name} 缺少方法 {name}")


def test_bootstrap_initialize_starts_wireguard_and_keepalive() -> None:
    klass = _class_node(BOOTSTRAP_PATH, "BootstrapMixin")
    initialized = ast.dump(_method_node(klass, "initialize"))
    assert "_prepare_components_and_start" in initialized
    assert "_initialize_device_runtime" in initialized
    prepared = ast.dump(_method_node(klass, "_prepare_components_and_start"))
    assert "prepare" in prepared and "_initialize_device_runtime" in prepared
    dumped = ast.dump(_method_node(klass, "_initialize_device_runtime"))
    assert "_ensure_wireguard_started" in dumped
    assert "_start_wireguard_keepalive" in dumped


def test_bootstrap_terminate_stops_wireguard_helper() -> None:
    """插件 terminate 必须取消保活循环并停止 helper（复用 0.0.6 的 terminate 机制）。"""
    klass = _class_node(BOOTSTRAP_PATH, "BootstrapMixin")
    dumped = ast.dump(_method_node(klass, "_terminate_impl"))
    assert "_wireguard_keepalive_task" in dumped
    assert "_wireguard" in dumped
    assert "stop" in dumped


def test_runtime_keepalive_respects_backoff() -> None:
    klass = _class_node(RUNTIME_PATH, "RuntimeMixin")
    dumped = ast.dump(_method_node(klass, "_wireguard_keepalive_loop"))
    assert "unavailable" in dumped
    assert "retry_ready" in dumped
    assert "ensure_healthy" in dumped


def test_runtime_config_apply_handles_actions() -> None:
    klass = _class_node(RUNTIME_PATH, "RuntimeMixin")
    dumped = ast.dump(_method_node(klass, "_apply_wireguard_config"))
    assert "ACTION_STOP" in dumped
    assert "ACTION_RESTART" in dumped
    assert "reset_unavailable" in dumped


# ----------------------------------------------------------------------
# 真实 helper 冒烟（插件包内置二进制时运行；缺失则 skip）
# ----------------------------------------------------------------------

from core.platform_bins import bundled_phone_wg

BUNDLED_HELPER = bundled_phone_wg() or (
    PLUGIN_ROOT / "bin" / "linux-x86_64" / "phone-wg"
)


@pytest.mark.skipif(
    not BUNDLED_HELPER.is_file(),
    reason="当前平台未随包 phone-wg（helper 需先构建）",
)
def test_bundled_helper_real_lifecycle(tmp_path: Path) -> None:
    """用真实 helper 验证配置 JSON/状态 JSON 契约与启停（不建立对端，无需特权）。"""
    manager = _make_manager(
        tmp_path,
        _client_settings(adb_local_port=0),
        helper_path=BUNDLED_HELPER,
    )

    async def scenario() -> None:
        result = await manager.start()
        assert result["ok"] is True, result
        status = manager.status()
        assert status["healthy"] is True
        assert status["version"], "真实 helper 应在状态文件报告版本"
        assert status["listen_port"] > 0, "client 模式应由系统分配 UDP 端口"
        assert status["peer_count"] == 1
        await manager.stop()
        assert manager.running is False

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 容器级冒烟（本机只有 astrbot 桩，跳过；UAT 由真实依赖执行）
# ----------------------------------------------------------------------

_astrbot = pytest.importorskip("astrbot")
_IS_LOCAL_STUB = bool(getattr(_astrbot, "_acat_local_stub", False))


@pytest.mark.skipif(_IS_LOCAL_STUB, reason="需要真实 AstrBot 依赖（本机只有 astrbot 桩）")
def test_tool_registered_in_container() -> None:
    from core.tools import TOOL_NAMES
    from core.tools.wireguard import PhoneWireguardTool

    assert "phone_wireguard" in TOOL_NAMES
    tool = PhoneWireguardTool(service=object())
    assert tool.name == "phone_wireguard"
