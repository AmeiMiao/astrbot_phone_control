"""独立进程运行官方 ZeroTier Sockets SDK，提供回环 SOCKS 与组网 TCP 代理。"""

import argparse
import ctypes as c
import ipaddress
import json
import os
import signal
import socket
import socketserver
import threading
import time
from pathlib import Path

from .mesh import private_json


class SDK:
    def __init__(self, path):
        self.lib = c.CDLL(str(path))
        specs = {
            "zts_init_from_storage": ([c.c_char_p], c.c_int),
            "zts_node_start": ([], c.c_int),
            "zts_node_stop": ([], c.c_int),
            "zts_node_get_id": ([], c.c_uint64),
            "zts_node_is_online": ([], c.c_int),
            "zts_net_join": ([c.c_uint64], c.c_int),
            "zts_net_get_status": ([c.c_uint64], c.c_int),
            "zts_net_transport_is_ready": ([c.c_uint64], c.c_int),
            "zts_addr_get_str": ([c.c_uint64, c.c_uint, c.c_void_p, c.c_uint], c.c_int),
            "zts_socket": ([c.c_int, c.c_int, c.c_int], c.c_int),
            "zts_connect": ([c.c_int, c.c_char_p, c.c_ushort, c.c_int], c.c_int),
            "zts_bind": ([c.c_int, c.c_char_p, c.c_ushort], c.c_int),
            "zts_listen": ([c.c_int, c.c_int], c.c_int),
            "zts_accept": (
                [c.c_int, c.c_void_p, c.c_int, c.POINTER(c.c_ushort)],
                c.c_int,
            ),
            "zts_send": ([c.c_int, c.c_void_p, c.c_size_t, c.c_int], c.c_ssize_t),
            "zts_recv": ([c.c_int, c.c_void_p, c.c_size_t, c.c_int], c.c_ssize_t),
            "zts_close": ([c.c_int], c.c_int),
            "zts_shutdown_wr": ([c.c_int], c.c_int),
            "zts_set_recv_timeout": ([c.c_int, c.c_int, c.c_int], c.c_int),
            "zts_set_send_timeout": ([c.c_int, c.c_int, c.c_int], c.c_int),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self.lib, name)
            fn.argtypes = args
            fn.restype = result

    def check(self, result):
        if result < 0:
            raise OSError("ZeroTier socket operation failed")
        return result

    def stream(self):
        fd = self.check(self.lib.zts_socket(2, 1, 0))
        self.lib.zts_set_recv_timeout(fd, 60, 0)
        self.lib.zts_set_send_timeout(fd, 30, 0)
        return Stream(self, fd)

    def address(self, network):
        buf = c.create_string_buffer(64)
        if self.lib.zts_addr_get_str(network, 2, buf, 64) != 0:
            return ""
        return buf.value.decode()


class Stream:
    def __init__(self, sdk, fd):
        self.sdk = sdk
        self.fd = fd
        self.lock = threading.Lock()

    def recv(self, size):
        buf = c.create_string_buffer(size)
        n = self.sdk.check(self.sdk.lib.zts_recv(self.fd, buf, size, 0))
        return buf.raw[:n]

    def sendall(self, data):
        offset = 0
        while offset < len(data):
            part = data[offset:]
            buf = c.create_string_buffer(part)
            n = self.sdk.check(self.sdk.lib.zts_send(self.fd, buf, len(part), 0))
            if not n:
                raise OSError("ZeroTier stream closed")
            offset += n

    def shutdown(self, how):
        self.sdk.lib.zts_shutdown_wr(self.fd)

    def close(self):
        with self.lock:
            if self.fd >= 0:
                self.sdk.lib.zts_close(self.fd)
                self.fd = -1


def bridge(left, right):
    def pump(src, dst):
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    other = threading.Thread(target=pump, args=(right, left), daemon=True)
    other.start()
    try:
        pump(left, right)
        other.join(65)
    finally:
        left.close()
        right.close()


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.recv(size - len(data))
        if not chunk:
            raise OSError("SOCKS handshake closed")
        data.extend(chunk)
    return bytes(data)


class ForwardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 16


def run(config):
    os.umask(0o077)
    root = Path(config["state_dir"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    sdk = SDK(config["library"])
    network = int(config["network_id"], 16)
    sdk.check(sdk.lib.zts_init_from_storage(str(root).encode()))
    sdk.check(sdk.lib.zts_node_start())
    joined = False
    stopping = threading.Event()
    capacity = threading.BoundedSemaphore(24)
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, lambda *_: stopping.set())

    class Socks(socketserver.BaseRequestHandler):
        def handle(self):
            if not capacity.acquire(blocking=False):
                return
            remote = None
            try:
                self.request.settimeout(10)
                version, count = read_exact(self.request, 2)
                methods = read_exact(self.request, count)
                if version != 5 or count > 32 or 0 not in methods:
                    raise OSError("SOCKS method unsupported")
                self.request.sendall(b"\x05\x00")
                header = read_exact(self.request, 4)
                if header != b"\x05\x01\x00\x01":
                    raise OSError("SOCKS supports IPv4 TCP only")
                host = str(ipaddress.IPv4Address(read_exact(self.request, 4)))
                port = int.from_bytes(read_exact(self.request, 2), "big")
                if host != config.get("phone_ip") or port != config["adb_port"]:
                    raise OSError("SOCKS target outside configured phone endpoint")
                remote = sdk.stream()
                sdk.check(sdk.lib.zts_connect(remote.fd, host.encode(), port, 10000))
                self.request.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
                self.request.settimeout(60)
                bridge(self.request, remote)
            except OSError:
                try:
                    self.request.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
                except OSError:
                    pass
            finally:
                if remote:
                    remote.close()
                capacity.release()

    server = ForwardServer(("127.0.0.1", config["socks_port"]), Socks)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    listener = None
    proxy_target = None
    proxy_started = False

    def proxy(active_listener, active_target):
        while not stopping.is_set() and active_listener.fd >= 0:
            address = c.create_string_buffer(64)
            port = c.c_ushort()
            fd = sdk.lib.zts_accept(active_listener.fd, address, 64, c.byref(port))
            if fd < 0:
                stopping.wait(0.1)
                continue
            incoming = Stream(sdk, fd)
            peer = address.value.decode()
            # 抓包入口只接受配置的手机节点，不向全部 ZeroTier 成员开放代理。
            if peer != config.get("phone_ip") or not capacity.acquire(blocking=False):
                incoming.close()
                continue

            def handle(stream):
                try:
                    native = socket.create_connection(
                        ("127.0.0.1", active_target), timeout=10
                    )
                    native.settimeout(60)
                    bridge(stream, native)
                except OSError:
                    stream.close()
                finally:
                    capacity.release()

            threading.Thread(target=handle, args=(incoming,), daemon=True).start()

    try:
        while not stopping.is_set():
            online = bool(sdk.lib.zts_node_is_online())
            if online and not joined:
                sdk.check(sdk.lib.zts_net_join(network))
                joined = True
            ready = online and bool(sdk.lib.zts_net_transport_is_ready(network))
            address = sdk.address(network) if ready else ""
            status = sdk.lib.zts_net_get_status(network)
            error = {
                2: "请在 ZeroTier 控制台授权该节点",
                3: "网络不存在，请核对 Network ID",
                4: "ZeroTier 网络端口错误",
                5: "ZeroTier SDK 版本不兼容",
            }.get(status, "")
            control_path = Path(config["control_path"])
            control = (
                json.loads(control_path.read_text()) if control_path.exists() else {}
            )
            if ready and control.get("proxy_local_port") and not proxy_started:
                proxy_target = int(control["proxy_local_port"])
                listener = sdk.stream()
                sdk.check(
                    sdk.lib.zts_bind(listener.fd, b"0.0.0.0", config["proxy_port"])
                )
                sdk.check(sdk.lib.zts_listen(listener.fd, 16))
                threading.Thread(
                    target=proxy, args=(listener, proxy_target), daemon=True
                ).start()
                proxy_started = True
            if proxy_started and not control.get("proxy_local_port"):
                listener.close()
                listener = None
                proxy_started = False
            private_json(
                Path(config["status_path"]),
                dict(
                    provider="zerotier",
                    network_id=config["network_id"],
                    running=True,
                    ready=bool(ready and address),
                    addresses=[address] if address else [],
                    node_id=f"{sdk.lib.zts_node_get_id():010x}",
                    state="Running" if ready else "Waiting",
                    error=error,
                    auth_url="",
                    updated_at=time.time(),
                    proxy_port=config["proxy_port"] if proxy_started else 0,
                ),
            )
            stopping.wait(1)
    finally:
        stopping.set()
        server.shutdown()
        server.server_close()
        if listener:
            listener.close()
        sdk.lib.zts_node_stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    run(json.loads(Path(args.config).read_text()))
