"""ADB 抓包管理器：启动 mitmproxy、注入系统 CA、设置设备代理并读取解密流量。

运行前提（由部署环境准备）：
* 容器内安装 mitmproxy（``pip3 install mitmproxy``，提供 ``mitmdump``）；
* 手机可通过 adb root 获取 root（用于系统 CA 绑定挂载与 ``nsenter``）。

工作方式：
1. 在插件数据目录启动 ``mitmdump``（监听 127.0.0.1:<port>），生成 CA 证书；
2. ``adb root`` 后把 CA 混入系统证书目录并用 ``nsenter -t 1 -m -- mount --bind`` 挂载；
3. ``adb reverse`` 把手机 127.0.0.1:<port> 转发到容器 mitmdump，并设置全局 http_proxy；
4. addon 把解密后的 HTTP(S) 流量写入 JSONL，供 ``phone_traffic`` 工具读取。

停止时会恢复设备代理、移除 reverse 并卸载 CA 绑定挂载；异常退出后再次启动会先清理
遗留状态，避免设备因为代理指向不存在的服务而断网。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import traffic as traffic_mod

REMOTE_TMP = "/data/local/tmp/astrbot_phone_traffic"
SYSTEM_CACERTS = "/system/etc/security/cacerts"
CA_WAIT_SECONDS = 20.0
ROOT_SETTLE_SECONDS = 2.0
PROXY_VALUE = "127.0.0.1:{port}"


class TrafficCapture:
    """管理容器内 mitmdump 与手机侧 CA/代理/reverse 的抓包生命周期。"""

    def __init__(
        self,
        *,
        adb: Any,
        data_dir: str | Path,
        logger: Any = None,
        mitmdump: str = "",
    ) -> None:
        self.adb = adb
        self.root = Path(data_dir) / "traffic"
        self.confdir = self.root / "mitm"
        self.log_path = self.root / traffic_mod.FLOW_LOG_NAME
        self.addon_path = self.root / traffic_mod.ADDON_NAME
        self.mitm_log_path = self.root / "mitmdump.log"
        self._logger = logger
        self._mitmdump = mitmdump
        self._proc: asyncio.subprocess.Process | None = None
        self._proc_log = None
        self._port = 0
        self._body_chars = traffic_mod.DEFAULT_BODY_CHARS
        self._prev_proxy = ""
        self._last_error = ""
        self._started_at = 0.0
        # 设备无 root 时抓包不可用：本次插件运行内跳过重试，避免反复 adb root/告警。
        self._root_unavailable = False

    # ---- 基础工具 -----------------------------------------------------
    def _log(self, message: str) -> None:
        if self._logger is not None:
            try:
                self._logger(message)
            except Exception:  # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def root_unavailable(self) -> bool:
        """设备无 root、本次运行内已跳过抓包。"""
        return self._root_unavailable

    @property
    def port(self) -> int:
        return self._port

    def _find_mitmdump(self) -> str:
        if self._mitmdump:
            found = shutil.which(self._mitmdump)
            if found:
                return found
            candidate = Path(self._mitmdump).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            return ""
        found = shutil.which("mitmdump")
        if found:
            return found
        candidate = Path(sys.executable).with_name("mitmdump")
        if candidate.is_file():
            return str(candidate)
        return ""

    def _subject_hash_old(self, ca_path: Path) -> str:
        """用 openssl 计算 Android CA 文件名（subject_hash_old）。"""
        try:
            result = subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-inform",
                    "PEM",
                    "-subject_hash_old",
                    "-in",
                    str(ca_path),
                    "-noout",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        line = (result.stdout or "").strip().splitlines()
        return line[0].strip() if line else ""

    async def _clear_device_proxy(self) -> None:
        with suppress(Exception):
            await self.adb.shell("settings put global http_proxy :0", timeout=10)

    async def cleanup_device_state(self, *, port: int | None = None) -> None:
        """清理上一次运行可能残留的代理/reverse/CA 挂载（失败静默）。"""
        target_port = int(port or self._port or traffic_mod.DEFAULT_TRAFFIC_PORT)
        with suppress(Exception):
            await self.adb.run(["reverse", "--remove", f"tcp:{target_port}"], timeout=10)
        with suppress(Exception):
            await self.adb.run(["reverse", "--remove-all"], timeout=10)
        await self._clear_device_proxy()
        with suppress(Exception):
            await self.adb.shell(
                "nsenter -t 1 -m -- umount -l " + SYSTEM_CACERTS + " 2>/dev/null; true",
                timeout=15,
            )

    async def _wait_root(self, attempts: int = 30) -> bool:
        for _ in range(attempts):
            try:
                uid = (await self.adb.shell("id -u", timeout=5)).strip()
            except Exception:  # noqa: BLE001 - adb root 重启 adbd 期间会短暂失败
                await asyncio.sleep(0.5)
                continue
            if uid == "0":
                return True
            await asyncio.sleep(0.5)
        return False

    async def _ensure_reverse(self, attempts: int = 4) -> bool:
        """确保 adb reverse 存在（adb root/adbd 重启可能把 reverse 清掉）。"""
        args = ["reverse", f"tcp:{self._port}", f"tcp:{self._port}"]
        for _ in range(max(1, attempts)):
            try:
                await self.adb.run(args, timeout=15)
            except Exception:  # noqa: BLE001 - 重试后仍失败由调用方处理
                await asyncio.sleep(0.8)
                continue
            try:
                listing = await self.adb.run(["reverse", "--list"], timeout=10)
            except Exception:  # noqa: BLE001
                listing = ""
            if f"tcp:{self._port}" in listing:
                return True
            await asyncio.sleep(0.8)
        return False

    async def _force_stop_apps(self) -> int:
        """结束已运行的第三方 App，让它们的新进程信任刚注入的系统 CA。"""
        try:
            output = await self.adb.shell("pm list packages -3", timeout=20)
        except Exception:  # noqa: BLE001 - 取包失败就跳过
            return 0
        packages = [
            line.strip().split(":")[-1]
            for line in str(output or "").splitlines()
            if line.strip().startswith("package:")
        ]
        # 批量 force-stop，避免几十次 adb 往返
        commands = "; ".join(
            f"am force-stop {package}" for package in packages[:80] if package
        )
        if not commands:
            return 0
        try:
            await self.adb.shell(commands, timeout=45)
        except Exception:  # noqa: BLE001 - 杀进程失败不阻塞抓包
            return 0
        return len(packages[:80])

    async def _setup_device(self) -> None:
        """注入系统 CA、建立 reverse 与全局代理（需要 root）。"""
        ca_path = self.confdir / traffic_mod.CA_FILE_NAME
        cert_hash = self._subject_hash_old(ca_path)
        if not cert_hash:
            raise RuntimeError("无法计算 CA 证书哈希（容器缺少 openssl）")
        # 先清掉可能残留的旧挂载/代理，避免重复绑定失败
        await self.cleanup_device_state()
        with suppress(Exception):
            await self.adb.run(["root"], timeout=20)
        # adb root 会让 adbd 重启：等它完成后再建 reverse，否则 reverse 会被清掉。
        await asyncio.sleep(ROOT_SETTLE_SECONDS)
        if not await self._wait_root():
            self._root_unavailable = True
            raise RuntimeError("设备无 root，已跳过抓包（adb root 失败）")
        remote_ca = f"{REMOTE_TMP}/cacerts/{cert_hash}.0"
        await self.adb.shell(
            f"rm -rf {REMOTE_TMP}; mkdir -p {REMOTE_TMP}/cacerts; "
            f"cp -a {SYSTEM_CACERTS}/. {REMOTE_TMP}/cacerts/",
            timeout=40,
        )
        await self.adb.run(["push", str(ca_path), remote_ca], timeout=30)
        await self.adb.shell(f"chmod 644 {remote_ca}", timeout=10)
        await self.adb.shell(
            f"nsenter -t 1 -m -- mount --bind {REMOTE_TMP}/cacerts {SYSTEM_CACERTS}",
            timeout=30,
        )
        visible = await self.adb.shell(
            f"ls {SYSTEM_CACERTS}/{cert_hash}.0", timeout=10
        )
        if cert_hash not in visible:
            raise RuntimeError("系统 CA 绑定挂载失败（nsenter/mount 被拒绝）")
        # 已运行的 App 进程不会重新加载系统 CA：先结束它们，保证新进程能解密。
        stopped = await self._force_stop_apps()
        if stopped:
            self._log(f"抓包：已结束 {stopped} 个第三方 App 进程，确保新进程信任 CA")
        if not await self._ensure_reverse():
            raise RuntimeError("adb reverse 建立失败（端口映射不可用）")
        previous = ""
        with suppress(Exception):
            previous = (await self.adb.shell(
                "settings get global http_proxy", timeout=10
            )).strip()
        if previous.lower() in {"", "null", ":0"}:
            self._prev_proxy = ""
        else:
            self._prev_proxy = previous
        await self.adb.shell(
            f"settings put global http_proxy {PROXY_VALUE.format(port=self._port)}",
            timeout=10,
        )
        # adb root 的 adbd 重启可能晚于上面步骤，设置代理后再确认一次 reverse。
        if not await self._ensure_reverse(attempts=2):
            raise RuntimeError("adb reverse 在设置代理后丢失，端口映射不可用")

    async def start(
        self,
        *,
        port: int | None = None,
        body_chars: int | None = None,
    ) -> dict[str, Any]:
        """启动抓包；返回 ``{"ok","reason",...}``，失败时自动清理。"""
        if self.running:
            return self.status()
        if self._root_unavailable:
            return {
                "ok": False,
                "root_unavailable": True,
                "reason": "设备无 root，已跳过抓包",
            }
        target_port = int(port or traffic_mod.DEFAULT_TRAFFIC_PORT)
        if not 1024 <= target_port <= 65535:
            return {"ok": False, "reason": f"端口非法：{target_port}"}
        mitmdump = self._find_mitmdump()
        if not mitmdump:
            return {
                "ok": False,
                "reason": (
                    "未找到 mitmdump：请用独立 venv 安装 mitmproxy 后，"
                    "把 traffic.mitmdump_path 指向该 venv/bin/mitmdump"
                ),
            }
        self._port = target_port
        self._body_chars = traffic_mod.normalize_body_chars(
            body_chars if body_chars is not None else self._body_chars
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.confdir.mkdir(parents=True, exist_ok=True)
            self.addon_path.write_text(
                traffic_mod.build_addon_script(self.log_path, self._body_chars),
                encoding="utf-8",
            )
            traffic_mod.prune_log(self.log_path)
        except OSError as exc:
            return {"ok": False, "reason": f"抓包目录准备失败：{exc}"}
        try:
            self._proc_log = self.mitm_log_path.open("ab")
            self._proc = await asyncio.create_subprocess_exec(
                mitmdump,
                "-q",
                "-p",
                str(target_port),
                "--set",
                f"confdir={self.confdir}",
                "-s",
                str(self.addon_path),
                stdout=self._proc_log,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "HOME": str(self.root)},
            )
        except (OSError, asyncio.SubprocessError) as exc:
            await self.stop()
            return {"ok": False, "reason": f"启动 mitmdump 失败：{exc}"}
        # 等 CA 生成
        ca_path = self.confdir / traffic_mod.CA_FILE_NAME
        for _ in range(int(CA_WAIT_SECONDS * 2)):
            if ca_path.is_file():
                break
            if self._proc.returncode is not None:
                break
            await asyncio.sleep(0.5)
        if not ca_path.is_file():
            tail = self._tail_mitm_log()
            await self.stop()
            return {"ok": False, "reason": f"mitmdump 未生成 CA 证书：{tail}"}
        try:
            await self._setup_device()
        except Exception as exc:  # noqa: BLE001 - 设备环境配置失败时整体回滚
            await self.stop()
            self._last_error = str(exc)
            return {
                "ok": False,
                "root_unavailable": self._root_unavailable,
                "reason": f"设备抓包环境配置失败：{exc}",
            }
        self._last_error = ""
        self._started_at = time.time()
        self._log(
            f"抓包已启动：端口 {self._port}，流量日志 {self.log_path}；"
            "已注入系统 CA 并设置设备代理（新启动的 App 才会信任 CA）"
        )
        return self.status()

    def _tail_mitm_log(self, limit: int = 400) -> str:
        try:
            data = self.mitm_log_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return "（无日志）"
        return data.strip()[-limit:] or "（无日志）"

    async def stop(self) -> None:
        """停止抓包并恢复设备代理/证书挂载。"""
        if self._port:
            with suppress(Exception):
                await self.adb.run(
                    ["reverse", "--remove", f"tcp:{self._port}"], timeout=10
                )
            with suppress(Exception):
                if self._prev_proxy:
                    await self.adb.shell(
                        f"settings put global http_proxy {self._prev_proxy}",
                        timeout=10,
                    )
                else:
                    await self.adb.shell(
                        "settings put global http_proxy :0", timeout=10
                    )
            with suppress(Exception):
                await self.adb.shell(
                    "nsenter -t 1 -m -- umount -l "
                    + SYSTEM_CACERTS
                    + " 2>/dev/null; true",
                    timeout=15,
                )
        self._prev_proxy = ""
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                with suppress(ProcessLookupError):
                    proc.kill()
                with suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=3)
        if self._proc_log is not None:
            with suppress(Exception):
                self._proc_log.close()
            self._proc_log = None
        self._log("抓包已停止，设备代理/CA 挂载已恢复")

    async def ensure_device(self) -> None:
        """定期校验：进程存活、设备仍走代理且 root/CA 正常；异常时重建。"""
        if not self.running:
            return
        if self._root_unavailable:
            return
        traffic_mod.prune_log(self.log_path)
        proxy = ""
        with suppress(Exception):
            proxy = (
                await self.adb.shell("settings get global http_proxy", timeout=8)
            ).strip()
        expected = PROXY_VALUE.format(port=self._port)
        reverse_ok = False
        with suppress(Exception):
            listing = await self.adb.run(["reverse", "--list"], timeout=8)
            reverse_ok = f"tcp:{self._port}" in listing
        root_ok = False
        with suppress(Exception):
            root_ok = (await self.adb.shell("id -u", timeout=8)).strip() == "0"
        if proxy == expected and root_ok and not reverse_ok:
            # 只有 reverse 丢失（adb 重连/transport 变化）时只重建端口映射，
            # 不重启 App，避免打断正在进行的玩手机任务。
            self._log("抓包保活：reverse 丢失，重新建立端口映射")
            with suppress(Exception):
                await self._ensure_reverse()
            return
        if proxy != expected or not reverse_ok or not root_ok:
            self._log(
                "抓包保活：设备代理/reverse/root 状态异常"
                f"（proxy={proxy or '空'} reverse={reverse_ok} root={root_ok}），重新配置"
            )
            with suppress(Exception):
                await self._setup_device()
        # 进程若已退出，尝试重启由上层插件循环负责
        if self._proc is not None and self._proc.returncode is not None:
            self._last_error = "mitmdump 意外退出"

    def status(self) -> dict[str, Any]:
        """返回抓包状态（不访问设备，供工具/命令快速展示）。"""
        flows = 0
        try:
            if self.log_path.exists():
                with self.log_path.open("rb") as handle:
                    flows = sum(1 for _ in handle)
        except OSError:
            flows = 0
        return {
            "ok": self.running,
            "running": self.running,
            "port": self._port,
            "flows": flows,
            "log_path": str(self.log_path),
            "mitmdump": self._find_mitmdump() or "",
            "error": self._last_error,
            "root_unavailable": self._root_unavailable,
            "started_at": self._started_at,
        }

    def status_text(self) -> str:
        status = self.status()
        if status["running"]:
            text = (
                f"抓包：已开启（端口 {status['port']}，已记录 {status['flows']} 条流量）"
            )
        elif status["error"]:
            text = f"抓包：未开启（{status['error']}）"
        else:
            text = "抓包：未开启"
        return text

    def recent(self, *, limit: int = traffic_mod.DEFAULT_LIMIT, since_seconds: int = 0) -> list[dict[str, Any]]:
        return traffic_mod.load_recent_flows(
            self.log_path, limit=limit, since_seconds=since_seconds
        )
