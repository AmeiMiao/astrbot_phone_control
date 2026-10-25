"""Relay App 主动连接的固定证书 TLS 通道及有期限的设备请求。"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
import ssl
import struct
import time
from pathlib import Path

from .device_gate import current_scope
from .errors import ActionFailedError, DeviceOfflineError

MAX_PACKET = 12 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
CAPABILITIES = ("shell", "screen", "nodes", "input", "clipboard")
APP_UNSUPPORTED = "App 控制模式暂不支持音视频、文件传输和抓包；请切换为直连"


class AppBridge:
    def __init__(self, directory: Path, apk: Path):
        self.directory = directory
        self.apk = apk
        self.server = None
        self.writer = None
        self.pending = {}
        self.peer = {}
        self.tasks = set()
        self.session = ""
        self._write_lock = asyncio.Lock()
        self._state = {}
        self.fingerprint = ""
        self.port = 0

    def _save_state(self):
        path = self.directory / "identity.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state))
        tmp.chmod(0o600)
        tmp.replace(path)

    def _tls(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from datetime import datetime, timedelta, timezone
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        state = self.directory / "identity.json"
        self._state = json.loads(state.read_text()) if state.exists() else {"token": secrets.token_urlsafe(32)}
        self._save_state()
        cert, key = self.directory / "server.pem", self.directory / "server.key"
        if not cert.exists() or not key.exists():
            private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Phone Relay")])
            now = datetime.now(timezone.utc)
            certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(private.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
                .sign(private, hashes.SHA256()))
            key.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            key.chmod(0o600)
            cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        certificate = x509.load_pem_x509_certificate(cert.read_bytes())
        self.fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        return ctx

    async def start(self, port):
        if self.server:
            return
        ctx = self._tls()
        self.server = await asyncio.start_server(self._accept, "0.0.0.0", port, ssl=ctx, ssl_handshake_timeout=5)
        self.port = self.server.sockets[0].getsockname()[1]

    def pairing(self, host):
        if not self.server:
            raise ActionFailedError("请先选择 App 控制并重载插件")
        payload = {"v": 1, "host": host, "port": self.port, "token": self._state["token"], "pin": self.fingerprint}
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()

    @staticmethod
    async def read(reader):
        size = struct.unpack("!I", await reader.readexactly(4))[0]
        if not 0 < size <= MAX_PACKET:
            raise ValueError("App 消息过大")
        value = json.loads(await reader.readexactly(size))
        if not isinstance(value, dict):
            raise ValueError("App 消息必须是对象")
        return value

    async def send(self, writer, value):
        data = json.dumps(value, separators=(",", ":")).encode()
        if len(data) > MAX_PACKET:
            raise ValueError("App 消息过大")
        async with self._write_lock:
            writer.write(struct.pack("!I", len(data)) + data)
            await asyncio.wait_for(writer.drain(), 10)

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        authenticated = False
        try:
            hello = await asyncio.wait_for(self.read(reader), 5)
            device = str(hello.get("device_id") or "")
            if (hello.get("v") != 1 or not secrets.compare_digest(str(hello.get("token", "")), self._state["token"])
                    or not device or len(device) > 128 or self.writer is not None
                    or self._state.get("device_id", device) != device):
                return
            # 配对秘密仅授权一台设备；后续连接继续核对已绑定身份。
            self._state["device_id"] = device
            self._save_state()
            self.writer = writer
            self.session = secrets.token_hex(16)
            self.peer = {"device_id": device, "version": str(hello.get("version", "")), "connected": True}
            authenticated = True
            await self.send(writer, {"type": "ready", "session": self.session, "v": 1})
            while True:
                value = await asyncio.wait_for(self.read(reader), 45)
                if value.get("type") == "ping":
                    await self.send(writer, {"type": "pong"})
                elif value.get("type") == "result":
                    future = self.pending.get(value.get("id"))
                    if future is not None and not future.done():
                        future.set_result(value)
                elif value.get("type") == "update":
                    await self.send(writer, self.update(value))
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass  # 连接关闭后统一撤销本会话的请求。
        finally:
            if authenticated and self.writer is writer:
                self.writer = None
                self.peer["connected"] = False
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(DeviceOfflineError("Relay App 已断线；本次操作不重试"))
            writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 2)
            self.tasks.discard(task)

    def update(self, value):
        manifest = self.apk.with_suffix(".json")
        info = json.loads(manifest.read_text())
        if self.apk.stat().st_size != info["size"]:
            raise ValueError("APK 大小与版本信息不符")
        result = {"type": "update", "id": value.get("id"), "info": info}
        if "offset" in value:
            offset = int(value["offset"])
            if not 0 <= offset <= info["size"]:
                raise ValueError("APK 下载位置无效")
            with self.apk.open("rb") as handle:
                handle.seek(offset)
                result["data"] = base64.b64encode(handle.read(65536)).decode()
            result["offset"] = offset
        return result

    async def call(self, op, *, timeout=30, **params):
        writer = self.writer
        if writer is None:
            raise DeviceOfflineError("Relay App 未连接，请在手机完成本机 ADB 授权并启动控制")
        scope = current_scope()
        if scope:
            scope.checkpoint(op)
            timeout = min(timeout, max(0.001, scope.deadline - time.monotonic()))
        request_id = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        deadline = time.monotonic() + timeout
        try:
            await self.send(writer, {"type": "call", "session": self.session, "id": request_id,
                                     "op": op, "ttl_ms": int(timeout * 1000), "params": params})
            while not future.done():
                if scope:
                    scope.checkpoint(op)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ActionFailedError("App 操作超时；执行结果可能未知，本次操作不重试")
                await asyncio.wait({future}, timeout=min(0.1, remaining))
            result = future.result()
            if result.get("error"):
                raise ActionFailedError("App 操作失败", detail=str(result["error"])[:500])
            return result
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()
                if self.writer is writer:
                    with contextlib.suppress(OSError, asyncio.TimeoutError):
                        await self.send(writer, {"type": "cancel", "id": request_id, "session": self.session})

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
