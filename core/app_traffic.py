"""App 控制的手机代理、流量上传与认证 mitmproxy 隧道。"""

from __future__ import annotations

import asyncio
import json
import secrets
import shlex
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import traffic as traffic_mod
from .app_ca import AppCaTrust
from .errors import ActionFailedError
from .process_lease import DeviceLease
from .traffic_capture import TrafficCapture


class AppTrafficCapture:
    def __init__(
        self, *, adb, bridge, data_dir, settings_provider, logger=None, **kwargs
    ):
        self.adb, self.bridge = adb, bridge
        self.root = Path(data_dir) / "traffic"
        self.log_path = self.root / traffic_mod.FLOW_LOG_NAME
        self._settings_provider = settings_provider
        self._logger = logger or (lambda text: None)
        self._mitmdump = kwargs.get("mitmdump", "")
        self.host = TrafficCapture(
            adb=adb,
            data_dir=data_dir,
            settings_provider=settings_provider,
            mitmdump=self._mitmdump,
            logger=logger,
            manage_device=False,
        )
        self.bridge.traffic_handler = self._receive
        self._lock = asyncio.Lock()
        self._active = False
        self._capture_id = ""
        self._session_id = ""
        self._error = ""
        self._unavailable = False
        self._retry_at = 0.0
        self._port = 0
        self._started_at = 0.0
        self._last_flow_at = 0.0
        self._received = self._dropped = self._failures = 0
        self._provider = settings_provider().provider
        self._lease = None
        self._ca = None
        self._peer_version = ""

    @property
    def confdir(self):
        return self.root / "app-ca" if self._provider == "app" else self.host.confdir

    @property
    def port(self):
        return self._port

    @property
    def running(self):
        return (
            self._active
            and self.bridge.writer is not None
            and self._session_id == getattr(self.bridge, "session", "")
            and (self._provider == "app" or self.host.running)
        )

    @property
    def unavailable(self):
        return self._unavailable

    @property
    def root_unavailable(self):
        return self._unavailable and ("root" in self._error or "su" in self._error)

    @property
    def unavailable_reason(self):
        return self._error if self._unavailable else ""

    @property
    def retry_ready(self):
        if self._peer_version != self.bridge.peer.get("version", ""):
            self.reset_unavailable()
        return not self._unavailable and time.monotonic() >= self._retry_at

    @property
    def next_retry_in(self):
        return max(0.0, self._retry_at - time.monotonic())

    def reset_unavailable(self):
        self._unavailable = False
        self._retry_at = 0.0
        self._error = ""
        self._peer_version = self.bridge.peer.get("version", "")

    def _receive(self, event):
        if (
            not self._capture_id
            or self._session_id != getattr(self.bridge, "session", "")
            or event.get("capture_id") != self._capture_id
        ):
            return
        records = event.get("records")
        if not isinstance(records, list) or len(records) > 8:
            raise ValueError("App 抓包批次无效")
        if records and self._provider != "app":
            raise ValueError("抓包来源与当前方式不符")
        cleaned = []
        for row in records:
            if not isinstance(row, dict):
                raise ValueError("App 抓包记录无效")
            url = row.get("url", "")
            if (
                not isinstance(url, str)
                or len(url) > 16384
                or urlsplit(url).scheme not in ("http", "https")
            ):
                raise ValueError("App 抓包 URL 无效")
            record = {"ts": time.time(), "source": "app"}
            for key, maximum in (
                ("method", 16),
                ("url", 16384),
                ("ctype", 512),
                ("reqbody", 800),
                ("body", 8000),
            ):
                value = row.get(key, "")
                if not isinstance(value, str) or len(value) > maximum:
                    raise ValueError("App 抓包字段无效：" + key)
                record[key] = value
            status = row.get("status")
            if type(status) is not int or not 100 <= status <= 599:
                raise ValueError("App 抓包状态码无效")
            record["status"] = status
            cleaned.append(record)
        self._failures = max(0, int(event.get("failures", 0)))
        self._error = str(event.get("error", ""))[:500]
        self._dropped = max(self._dropped, max(0, int(event.get("dropped", 0))))
        if not cleaned:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as output:
            for record in cleaned:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.log_path.chmod(0o600)
        traffic_mod.prune_log(self.log_path)
        self._received += len(cleaned)
        if cleaned:
            self._last_flow_at = time.time()
        self._dropped = max(self._dropped, int(event.get("dropped", 0)))

    async def start(self, *, port=None, body_chars=None):
        async with self._lock:
            if self.running:
                return self.status()
            self._peer_version = self.bridge.peer.get("version", "")
            try:
                await self._stop_locked()
                settings = self._settings_provider()
                self._provider = settings.provider
                self._port = int(port or settings.port)
                if not 1024 <= self._port <= 65535:
                    raise ActionFailedError("抓包端口无效")
                if self.bridge.writer is None:
                    raise ActionFailedError("Relay App 未连接")
                if "traffic_proxy" not in self.bridge.peer.get("features", []):
                    raise ActionFailedError(
                        "请先将 Phone Relay App 更新到支持手机抓包的版本"
                    )
                self._capture_id = secrets.token_hex(16)
                self._session_id = getattr(self.bridge, "session", "")
                self._started_at = time.time()
                self._received = self._dropped = self._failures = 0
                self._last_flow_at = 0.0
                self.root.mkdir(parents=True, exist_ok=True)
                if self._provider == "mitmproxy":
                    self.host._mitmdump = self._mitmdump
                    result = await self.host.start(
                        port=self._port, body_chars=body_chars
                    )
                    if not result.get("ok"):
                        raise ActionFailedError(
                            result.get("reason")
                            or result.get("error")
                            or "mitmproxy 启动失败"
                        )
                    self.bridge.proxy_port = self._port
                    self.bridge.proxy_capture_id = self._capture_id
                    self.bridge.proxy_session = self._session_id
                else:
                    response = await self.bridge.call("traffic_prepare", timeout=20)
                    pem = response.get("ca", "")
                    from cryptography import x509

                    if not isinstance(pem, str) or len(pem) > 16384:
                        raise ActionFailedError("手机抓包 CA 无效")
                    cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
                    if not cert.extensions.get_extension_for_class(
                        x509.BasicConstraints
                    ).value.ca:
                        raise ActionFailedError("手机抓包证书不是 CA")
                    self.confdir.mkdir(parents=True, exist_ok=True)
                    (self.confdir / traffic_mod.CA_FILE_NAME).write_text(
                        pem, encoding="ascii"
                    )
                previous = (
                    await self.adb.shell("settings get global http_proxy", timeout=5)
                ).strip()
                self._lease = DeviceLease(self.adb, "app-traffic")
                await self._lease.prepare()
                expected = shlex.quote("127.0.0.1:" + str(self._port))

                async def guard(ca_cleanup):
                    cleanup = (
                        f'if [ "$(settings get global http_proxy)" = {expected} ]; then '
                        f"settings put global http_proxy {shlex.quote(previous if previous not in ('', 'null') else ':0')}; fi; "
                        + ca_cleanup
                    )
                    await self._lease.arm(None, cleanup=cleanup)

                self._ca = AppCaTrust(self.adb, self._capture_id, before_install=guard)
                if settings.ca_mode == "system":
                    await self._ca.install(self.confdir / traffic_mod.CA_FILE_NAME)
                else:
                    await guard("true")
                result = await self.bridge.call(
                    "traffic_start",
                    timeout=20,
                    port=self._port,
                    body_chars=body_chars or settings.body_chars,
                    provider=self._provider,
                    capture_id=self._capture_id,
                )
                if (
                    not result.get("running")
                    or result.get("capture_id") != self._capture_id
                ):
                    raise ActionFailedError("手机代理未确认启动")
                self._active = True
                self.reset_unavailable()
                return self.status()
            except asyncio.CancelledError:
                try:
                    await self._stop_locked()
                except Exception as cleanup_error:
                    self._error = "取消抓包后的收尾失败：" + str(cleanup_error)
                    self._logger(self._error)
                raise
            except Exception as exc:
                reason = str(exc)
                try:
                    await self._stop_locked()
                except Exception as cleanup_error:
                    reason += "；收尾失败：" + str(cleanup_error)
                self._error = reason
                self._unavailable = "root" in reason or "su 授权" in reason
                self._retry_at = time.monotonic() + 30
                return self.status()

    async def _stop_locked(self):
        self._active = False
        errors = []
        if self._capture_id and self.bridge.writer is not None:
            try:
                await self.bridge.call(
                    "traffic_stop", timeout=8, capture_id=self._capture_id
                )
            except Exception as exc:
                errors.append("停止手机代理：" + str(exc))
        self.bridge.proxy_port = 0
        self.bridge.proxy_capture_id = ""
        self.bridge.proxy_session = ""
        await self.bridge.close_proxies()
        if self._lease is not None:
            try:
                await self._lease.close()
            except Exception as exc:
                errors.append("代理租约收尾：" + str(exc))
            self._lease = None
        if self._ca is not None:
            try:
                await self._ca.close()
            except Exception as exc:
                errors.append("系统 CA 收尾：" + str(exc))
            self._ca = None
        await self.host.stop()
        self._capture_id = ""
        if errors:
            self._error = "；".join(errors)
            raise ActionFailedError(self._error)

    async def stop(self):
        async with self._lock:
            await self._stop_locked()

    async def cleanup_device_state(self, **kwargs):
        await self.stop()

    async def ensure_device(self):
        async with self._lock:
            if not self.running:
                return
            result = await self.bridge.call("traffic_status", timeout=8)
            self._failures = int(result.get("failures", 0))
            self._dropped = int(result.get("dropped", 0))
            self._error = str(result.get("error", ""))
            if (
                not result.get("running")
                or result.get("capture_id") != self._capture_id
            ):
                self._active = False
                self._error = "手机抓包代理已停止，等待重新启动"

    def status(self):
        settings = self._settings_provider()
        recent = self.recent(limit=100)
        if self._provider == "mitmproxy" and recent:
            self._last_flow_at = recent[-1]["ts"]
        return {
            "ok": self.running,
            "running": self.running,
            "port": self._port,
            "provider": self._provider,
            "transport": "relay_tls",
            "ca_mode": settings.ca_mode,
            "proxy_address": "127.0.0.1:" + str(self._port),
            "flows": len(recent),
            "received": self._received,
            "last_flow_at": self._last_flow_at,
            "dropped": self._dropped,
            "failures": self._failures,
            "log_path": str(self.log_path),
            "error": self._error,
            "reason": self._error,
            "unavailable": self._unavailable,
            "root_unavailable": self.root_unavailable,
            "retry_in": round(self.next_retry_in, 1),
            "started_at": self._started_at,
        }

    def status_text(self):
        return (
            "手机抓包："
            + ("运行中" if self.running else "未运行")
            + "（"
            + self._provider
            + "）"
            + ("；" + self._error if self._error else "")
        )

    def recent(self, *, limit=traffic_mod.DEFAULT_LIMIT, since_seconds=0):
        flows = traffic_mod.load_recent_flows(
            self.log_path,
            limit=100,
            since_seconds=since_seconds or self._settings_provider().keep_seconds,
        )
        flows = [
            flow
            for flow in flows
            if flow.get("source", "mitmproxy") == self._provider
            and flow["ts"] >= self._started_at
        ]
        return flows[-max(1, min(100, int(limit))) :]


def populate_capture_options(config):
    """按已保存的连接方式填充原生下拉，保留隐藏的抓包选择。"""
    schema = getattr(config, "schema", None)
    if not isinstance(schema, dict):
        return
    field = schema.get("traffic", {}).get("items", {}).get("provider")
    if not isinstance(field, dict):
        return
    app = config.get("adb", {}).get("mode") in ("app", "使用 App 控制")
    field["options"] = ["mitmproxy", "App 抓包"] if app else ["mitmproxy"]
