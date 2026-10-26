"""管理独立 Tailscale/ZeroTier 用户态节点、ADB 转发与组网内抓包端口。"""

import asyncio
import contextlib
import ipaddress
import json
import os
import platform
import re
import sys
import time
from pathlib import Path

from .errors import PhoneControlError
from .process_lease import guarded_command, start_heartbeat, stop_heartbeat
from .plugin_shared import ensure_executable


class MeshError(PhoneControlError):
    pass


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with os.fdopen(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
    ) as f:
        json.dump(value, f, ensure_ascii=False)
    temporary.replace(path)


async def run_command(argv, timeout=15):
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    async def read(stream):
        result = bytearray()
        while chunk := await stream.read(4096):
            if len(result) + len(chunk) > 1024 * 1024:
                raise MeshError("组网命令输出超过限制")
            result.extend(chunk)
        return result.decode(errors="replace")

    async def collect():
        out, error = await asyncio.gather(read(process.stdout), read(process.stderr))
        await process.wait()
        return process.returncode, out, error

    try:
        return await asyncio.wait_for(collect(), timeout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class MeshManager:
    def __init__(self, service):
        self.service = service
        self.root = service.data_dir / "mesh"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.process = None
        self.heartbeat = None
        self.log = None
        self.server = None
        self.connections = set()
        self.lock = asyncio.Lock()
        self.socks_port = 0
        self.exposed_port = 0
        self.adb_original = None
        self.last_error = ""
        self._cli = ""
        self._signature = None
        self._login_attempted = False

    @property
    def settings(self):
        return self.service.config.mesh

    @property
    def app_port(self):
        adb = getattr(self.service.config, "adb", None)
        return adb.app_port if adb is not None and adb.mode == "app" else 0

    @property
    def running(self):
        return self.process is not None and self.process.returncode is None

    def binary(self, name, configured):
        components = getattr(self.service, "components", None)
        path = Path(configured).expanduser() if configured else (
            components.resolve(name) if components is not None else self.service.data_dir / "runtime" / name
        )
        if not path.is_file():
            raise MeshError(f"缺少组网组件 {name}，请在配置开启该功能并保存重载，或手动填写可执行文件绝对路径")
        ensure_executable(path)
        return str(path.resolve())

    async def cli(self, *args, timeout=15):
        return await run_command(
            [self._cli, "--socket=" + str(self.root / "tailscale.sock"), *args], timeout
        )

    async def start(self):
        if self.running and self._signature != self.settings:
            await self.stop()
        if self.settings.provider == "zerotier":
            return await self.start_zerotier()
        async with self.lock:
            settings = self.settings
            if not settings.enabled:
                raise MeshError("请先开启 network.enabled 并选择 Tailscale 或 ZeroTier")
            if self.service.config.wireguard.enabled:
                raise MeshError("当前选择 WireGuard，请在组网配置中选择 Tailscale 或 ZeroTier")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", settings.hostname):
                raise MeshError("组网设备名称需为 1–63 位字母、数字或连字符")
            if settings.provider != "tailscale":
                raise MeshError("未知组网服务")
            if not self.running:
                self._cli = self.binary("tailscale", settings.tailscale_path)
                daemon = self.binary("tailscaled", settings.tailscaled_path)
                # 仅在本机回环开放 SOCKS，不修改宿主路由或 DNS。
                reserve = await asyncio.start_server(
                    lambda r, w: w.close(), "127.0.0.1", 0
                )
                self.socks_port = reserve.sockets[0].getsockname()[1]
                reserve.close()
                await reserve.wait_closed()
                self.log = (self.root / "tailscaled.log").open("ab")
                os.chmod(self.root / "tailscaled.log", 0o600)
                self.process = await asyncio.create_subprocess_exec(
                    *guarded_command(
                        [
                            daemon,
                            "--tun=userspace-networking",
                            "--socket=" + str(self.root / "tailscale.sock"),
                            "--state=" + str(self.root / "tailscale.state"),
                            "--socks5-server=127.0.0.1:" + str(self.socks_port),
                            "--port=0",
                        ],
                        self.root / "process.lock",
                    ),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=self.log,
                    stderr=asyncio.subprocess.STDOUT,
                    env={**os.environ, "HOME": str(self.root)},
                )
                self.heartbeat = start_heartbeat(self.process)
                for _ in range(40):
                    if not self.running:
                        raise MeshError("Tailscale 节点启动失败，请检查组件与本机网络")
                    if (self.root / "tailscale.sock").exists():
                        break
                    await asyncio.sleep(0.1)
                self._login_attempted = False
            if not self._login_attempted:
                args = [
                    "up",
                    "--hostname=" + settings.hostname,
                    "--accept-dns=false",
                    "--accept-routes=false",
                    "--timeout=5s",
                ]
                keyfile = self.root / "auth.key"
                if settings.tailscale_auth_key:
                    with os.fdopen(
                        os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                        "w",
                    ) as f:
                        f.write(settings.tailscale_auth_key)
                    args.append("--auth-key=file:" + str(keyfile))
                try:
                    rc, _, error = await self.cli(*args, timeout=12)
                    self._login_attempted = True
                    self.last_error = "Tailscale 等待登录或节点授权" if rc else ""
                    # 密钥只通过私有文件传入；错误正文不进入插件日志或状态。
                    if rc and settings.tailscale_auth_key:
                        self.last_error = (
                            "Tailscale 登录未完成，请检查 Auth Key、设备审批或网络连接"
                        )
                finally:
                    keyfile.unlink(missing_ok=True)
            self._signature = settings
        result = await self.status()
        if result["ready"] and settings.auto_adb and not getattr(self.service.adb, "app_mode", False):
            await self.start_adb()
        return result

    async def status(self):
        result = {
            "provider": self.settings.provider,
            "running": self.running,
            "ready": False,
            "addresses": [],
            "auth_url": "",
            "error": self.last_error,
            "adb_forward": self.server is not None,
            "proxy_port": self.exposed_port,
        }
        if not self.running:
            return result
        if self.settings.provider == "zerotier":
            path = self.root / "zerotier-status.json"
            if not path.exists():
                result["state"] = "Waiting"
                return result
            try:
                value = json.loads(path.read_text())
                if (
                    time.time() - value.get("updated_at", 0) > 10
                    or value.get("network_id") != self.settings.zerotier_network_id
                ):
                    raise ValueError("stale status")
            except (OSError, ValueError, TypeError):
                result["error"] = "ZeroTier 状态未更新，请检查节点日志"
                return result
            result.update(value, adb_forward=self.server is not None)
            return result
        rc, text, _ = await self.cli("status", "--json")
        if rc:
            result["error"] = "组网状态读取失败"
            return result
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise MeshError("组网状态数据无效") from exc
        result.update(
            state=data.get("BackendState", ""),
            addresses=data.get("TailscaleIPs") or [],
            auth_url=data.get("AuthURL") or "",
            node_id=(data.get("Self") or {}).get("ID", ""),
        )
        result["ready"] = result["state"] == "Running" and bool(result["addresses"])
        result["app_port"] = self.app_port if result["ready"] else 0
        result["app_route"] = "tailscale-loopback" if result["ready"] and self.app_port else ""
        if result["ready"]:
            result["error"] = ""
        return result

    async def connect(self, host, port):
        address = ipaddress.IPv4Address(host)
        if (
            self.settings.provider == "tailscale"
            and address not in ipaddress.ip_network("100.64.0.0/10")
        ):
            raise MeshError("Tailscale 手机地址需位于 100.64.0.0/10 组网网段")
        reader, writer = await asyncio.open_connection("127.0.0.1", self.socks_port)
        try:
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            if await asyncio.wait_for(reader.readexactly(2), 10) != b"\x05\x00":
                raise MeshError("组网 SOCKS 握手失败")
            writer.write(
                b"\x05\x01\x00\x01" + address.packed + int(port).to_bytes(2, "big")
            )
            await writer.drain()
            reply = await asyncio.wait_for(reader.readexactly(4), 15)
            if reply[:2] != b"\x05\x00":
                raise MeshError("组网目标无法连接，请检查节点授权、手机 IP 与端口")
            lengths = {1: 4, 4: 16}
            length = lengths.get(reply[3])
            if reply[3] == 3:
                length = (await reader.readexactly(1))[0]
            if length is None:
                raise MeshError("组网 SOCKS 响应无效")
            await reader.readexactly(length + 2)
            return reader, writer
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise

    async def forward(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        remote_writer = None
        try:
            remote_reader, remote_writer = await self.connect(
                self.settings.phone_ip, self.settings.adb_port
            )

            async def copy(src, dst):
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
                with contextlib.suppress(OSError, NotImplementedError):
                    dst.write_eof()

            await asyncio.gather(
                copy(reader, remote_writer), copy(remote_reader, writer)
            )
        except (OSError, asyncio.IncompleteReadError, MeshError, asyncio.TimeoutError):
            self.last_error = "ADB 组网连接中断，请检查手机节点和 UID 中转服务"
        finally:
            for stream in [writer, remote_writer]:
                if stream is not None:
                    stream.close()
                    try:
                        await asyncio.wait_for(stream.wait_closed(), 2)
                    except (OSError, asyncio.TimeoutError):
                        stream.transport.abort()
            self.connections.discard(task)

    async def start_adb(self):
        if getattr(self.service.adb, "app_mode", False):
            return
        ip = ipaddress.IPv4Address(self.settings.phone_ip)
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
            raise MeshError("请填写手机的有效组网 IPv4 地址")
        if self.server is not None:
            return
        self.server = await asyncio.start_server(
            self.forward, "127.0.0.1", self.settings.local_adb_port
        )
        adb = self.service.adb
        self.adb_original = (
            adb._host,
            adb._port,
            adb.serial if adb._serial_explicit else "",
        )
        try:
            await adb.reconfigure(
                host="127.0.0.1", port=self.settings.local_adb_port, serial=""
            )
            # 本地转发重启后清除 ADB server 中该端口的离线传输，再核验实际操作。
            await adb.disconnect()
            await adb.ensure_ready()
            if (await adb.shell("echo phone_mesh_ready")).strip() != "phone_mesh_ready":
                raise MeshError("组网 ADB 实际操作核验失败")
        except BaseException:
            await self.close_forward_server()
            host, port, serial = self.adb_original
            self.adb_original = None
            await adb.reconfigure(host=host, port=port, serial=serial)
            raise

    async def expose_proxy(self, local_port):
        status = await self.status()
        if not status["ready"]:
            raise MeshError("组网节点尚未就绪，无法经组网抓包")
        port = self.settings.proxy_port
        if self.settings.provider == "zerotier":
            if not self.settings.phone_ip:
                raise MeshError("ZeroTier 抓包需先填写已授权手机的组网 IPv4")
            private_json(
                self.root / "zerotier-control.json",
                {"proxy_local_port": int(local_port)},
            )
            for _ in range(30):
                current = await self.status()
                if current.get("proxy_port") == port:
                    self.exposed_port = port
                    return current["addresses"][0] + ":" + str(port)
                await asyncio.sleep(0.1)
            raise MeshError("ZeroTier 抓包端口未就绪，请检查节点状态")
        rc, _, _ = await self.cli(
            "serve", "--bg", "--tcp=" + str(port), "tcp://127.0.0.1:" + str(local_port)
        )
        if rc:
            raise MeshError("组网代理端口发布失败，请检查 Tailscale Serve 授权与 ACL")
        self.exposed_port = port
        ip = next((a for a in status["addresses"] if ":" not in a), "")
        if not ip:
            raise MeshError("组网节点没有 IPv4 地址")
        return f"{ip}:{port}"

    async def start_zerotier(self):
        async with self.lock:
            settings = self.settings
            if not settings.enabled:
                raise MeshError("请先开启 network.enabled 并选择 ZeroTier")
            if self.service.config.wireguard.enabled:
                raise MeshError("当前选择 WireGuard，请在组网配置中选择 ZeroTier")
            if not re.fullmatch(r"[0-9a-f]{16}", settings.zerotier_network_id):
                raise MeshError("ZeroTier Network ID 需为 16 位十六进制")
            if not self.running:
                library = Path(settings.zerotier_library).expanduser()
                if not settings.zerotier_library or not library.is_file():
                    raise MeshError("请手动准备 ZeroTier Sockets SDK 并填写动态库绝对路径")
                reserve = await asyncio.start_server(
                    lambda r, w: w.close(), "127.0.0.1", 0
                )
                self.socks_port = reserve.sockets[0].getsockname()[1]
                reserve.close()
                await reserve.wait_closed()
                state = self.root / "zerotier"
                state.mkdir(exist_ok=True, mode=0o700)
                status_path = self.root / "zerotier-status.json"
                status_path.unlink(missing_ok=True)
                private_json(self.root / "zerotier-control.json", {})
                config_path = self.root / "zerotier-worker.json"
                private_json(
                    config_path,
                    dict(
                        library=str(library.resolve()),
                        state_dir=str(state),
                        network_id=settings.zerotier_network_id,
                        socks_port=self.socks_port,
                        phone_ip=settings.phone_ip,
                        adb_port=settings.adb_port,
                        proxy_port=settings.proxy_port,
                        app_port=self.app_port,
                        control_path=str(self.root / "zerotier-control.json"),
                        status_path=str(status_path),
                    ),
                )
                self.log = (self.root / "zerotier.log").open("ab")
                os.chmod(self.root / "zerotier.log", 0o600)
                package_root = str(Path(__file__).resolve().parents[1])
                self.process = await asyncio.create_subprocess_exec(
                    *guarded_command(
                        [
                            sys.executable,
                            "-m",
                            "core.mesh_zerotier_worker",
                            "--config",
                            str(config_path),
                        ],
                        self.root / "zerotier-process.lock",
                    ),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=self.log,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=package_root,
                    env={**os.environ, "PYTHONPATH": package_root},
                )
                self.heartbeat = start_heartbeat(self.process)
                for _ in range(30):
                    if not self.running:
                        raise MeshError(
                            "ZeroTier 用户态组件启动失败，请检查手动配置的 SDK 架构、API 与运行库"
                        )
                    if status_path.exists():
                        break
                    await asyncio.sleep(0.1)
                self._signature = settings
        result = await self.status()
        if result["ready"] and settings.auto_adb and not getattr(self.service.adb, "app_mode", False):
            await self.start_adb()
        return result

    async def hide_proxy(self):
        if self.settings.provider == "zerotier":
            private_json(self.root / "zerotier-control.json", {})
        elif self.running and self.exposed_port:
            rc, _, _ = await self.cli("serve", "--tcp=" + str(self.exposed_port), "off")
            if rc:
                raise MeshError("Tailscale 抓包端口关闭失败")
        self.exposed_port = 0

    async def close_forward_server(self):
        server = self.server
        self.server = None
        if server is not None:
            server.close()
        tasks = list(self.connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    async def stop(self):
        traffic = getattr(self.service, "traffic", None)
        failure = None
        if (
            traffic is not None
            and traffic.status().get("transport") == "mesh"
        ):
            try:
                await traffic.stop()
            except Exception as exc:
                failure = exc
        async with self.lock:
            if self.exposed_port:
                try:
                    await self.hide_proxy()
                except Exception as exc:
                    failure = failure or exc
            await self.close_forward_server()
            if self.adb_original is not None:
                host, port, serial = self.adb_original
                self.adb_original = None
                try:
                    await self.service.adb.reconfigure(
                        host=host, port=port, serial=serial
                    )
                except Exception as exc:
                    failure = failure or exc
            if self.running:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            await stop_heartbeat(self.heartbeat)
            self.heartbeat = None
            self.process = None
            if self.log:
                self.log.close()
                self.log = None
            self.exposed_port = 0
            self._signature = None
        if failure is not None:
            raise MeshError(
                "组网已停止，但代理或 ADB 原目标恢复未完成，请检查状态"
            ) from failure

    async def reconcile(self):
        if self.running and self._signature != self.settings:
            await self.stop()
        if self.settings.enabled:
            return await self.start()
        if self.running:
            await self.stop()
        return await self.status()
