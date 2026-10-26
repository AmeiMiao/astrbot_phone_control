"""独立进程运行官方 ZeroTier Sockets SDK，提供回环 SOCKS 与组网 TCP 代理。"""

import argparse
import ctypes as c
import ipaddress
import json
import os
import select
import signal
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

from .mesh import private_json


IP_STRING_SIZE = 46


class PollFd(c.Structure):
    _fields_ = [("fd", c.c_int), ("events", c.c_short), ("revents", c.c_short)]


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
            "zts_bsd_accept": ([c.c_int, c.c_void_p, c.c_void_p], c.c_int),
            "zts_getpeername": ([c.c_int, c.c_void_p, c.c_int, c.POINTER(c.c_ushort)], c.c_int),
            "zts_send": ([c.c_int, c.c_void_p, c.c_size_t, c.c_int], c.c_ssize_t),
            "zts_recv": ([c.c_int, c.c_void_p, c.c_size_t, c.c_int], c.c_ssize_t),
            "zts_close": ([c.c_int], c.c_int),
            "zts_bsd_poll": ([c.POINTER(PollFd), c.c_uint, c.c_int], c.c_int),
            "zts_shutdown_wr": ([c.c_int], c.c_int),
            "zts_set_no_delay": ([c.c_int, c.c_int], c.c_int),
            "zts_set_blocking": ([c.c_int, c.c_int], c.c_int),
            "zts_set_recv_timeout": ([c.c_int, c.c_int, c.c_int], c.c_int),
            "zts_set_send_timeout": ([c.c_int, c.c_int, c.c_int], c.c_int),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self.lib, name)
            fn.argtypes = args
            fn.restype = result

    def check(self, result):
        if result < 0:
            if result != -1:
                raise OSError(f"ZeroTier SDK operation failed (code {result})")
            error = c.c_int.in_dll(self.lib, "zts_errno").value
            raise OSError(error, "ZeroTier socket operation failed")
        return result

    def stream(self):
        fd = self.check(self.lib.zts_socket(2, 1, 0))
        self.lib.zts_set_no_delay(fd, 1)
        self.lib.zts_set_blocking(fd, 1)
        self.lib.zts_set_recv_timeout(fd, 60, 0)
        self.lib.zts_set_send_timeout(fd, 30, 0)
        return Stream(self, fd)

    def address(self, network):
        buf = c.create_string_buffer(IP_STRING_SIZE)
        if self.lib.zts_addr_get_str(network, 2, buf, IP_STRING_SIZE) != 0:
            return ""
        return buf.value.decode()


class Stream:
    def __init__(self, sdk, fd):
        self.sdk = sdk
        self.fd = fd
        self.lock = threading.Lock()

    def accept(self):
        with self.lock:
            if self.fd < 0:
                raise OSError("ZeroTier listener closed")
            fd = self.sdk.check(self.sdk.lib.zts_bsd_accept(self.fd, None, None))
        # 先确认 accept 成功，再按官方固定长度合约查询对端地址。
        address = c.create_string_buffer(IP_STRING_SIZE)
        port = c.c_ushort()
        try:
            self.sdk.check(self.sdk.lib.zts_getpeername(
                fd, address, IP_STRING_SIZE, c.byref(port)
            ))
        except BaseException:
            self.sdk.lib.zts_close(fd)
            raise
        return Stream(self.sdk, fd), address.value.decode()

    def recv(self, size):
        buf = c.create_string_buffer(size)
        n = self.sdk.check(self.sdk.lib.zts_recv(self.fd, buf, size, 8))
        return buf.raw[:n]

    def send(self, data):
        buf = c.create_string_buffer(data)
        return self.sdk.check(self.sdk.lib.zts_send(self.fd, buf, len(data), 8))

    def poll(self, reading, writing):
        descriptor = PollFd(self.fd, (1 if reading else 0) | (2 if writing else 0), 0)
        self.sdk.check(self.sdk.lib.zts_bsd_poll(c.byref(descriptor), 1, 0))
        if descriptor.revents & 12:
            raise OSError("ZeroTier socket unavailable")
        return bool(descriptor.revents & 1), bool(descriptor.revents & 2)

    def shutdown(self, how):
        self.sdk.lib.zts_shutdown_wr(self.fd)

    def close(self):
        with self.lock:
            if self.fd >= 0:
                self.sdk.lib.zts_close(self.fd)
                self.fd = -1


def bridge(left, right, stopping=None):
    # 虚拟套接字由其转发线程完成收发和关闭。
    virtual, native = (left, right) if isinstance(left, Stream) else (right, left)
    native.setblocking(False)
    pending_virtual = bytearray()
    pending_native = bytearray()
    native_read = virtual_read = True
    native_write = virtual_write = True
    last_activity = time.monotonic()
    limit = 65536
    try:
        while (native_read or virtual_read or pending_virtual or pending_native) and not (stopping and stopping.is_set()):
            iteration_activity = last_activity
            if time.monotonic() - last_activity > 60:
                raise TimeoutError("ZeroTier TCP channel idle timeout")
            reads, writes, _ = select.select(
                [native] if native_read and len(pending_virtual) < limit else [],
                [native] if pending_native else [], [], 0 if pending_virtual else 0.01,
            )
            can_read, can_write = virtual.poll(
                virtual_read and len(pending_native) < limit, bool(pending_virtual)
            )
            for source, target, readable, writable, buffer, destination in [
                (native, virtual, bool(reads), can_write, pending_virtual, "virtual"),
                (virtual, native, can_read, bool(writes), pending_native, "native"),
            ]:
                if readable:
                    try:
                        data = source.recv(limit - len(buffer))
                    except BlockingIOError:
                        data = None
                    if data:
                        buffer.extend(data)
                        last_activity = time.monotonic()
                    elif data == b"":
                        if destination == "virtual":
                            native_read = False
                        else:
                            virtual_read = False
                if writable and buffer:
                    try:
                        count = target.send(bytes(buffer))
                    except BlockingIOError:
                        count = 0
                    if count:
                        del buffer[:count]
                        last_activity = time.monotonic()
            if pending_virtual and last_activity == iteration_activity:
                time.sleep(0.005)
            if not native_read and not pending_virtual and virtual_write:
                virtual.shutdown(socket.SHUT_WR)
                virtual_write = False
            if not virtual_read and not pending_native and native_write:
                native.shutdown(socket.SHUT_WR)
                native_write = False
    except OSError as exc:
        print("ZeroTier TCP 通道结束：", str(exc), file=sys.stderr, flush=True)
    finally:
        virtual.close()
        native.close()


class Ingress:
    """来源受限的虚拟网入口，连接收发与SDK关闭均归所属线程管理。"""
    def __init__(self, listener, target, peer, bridge_fn=bridge):
        if type(target) is not int or not 1024 <= target <= 65535:
            raise ValueError('组网入口目标端口无效')
        self.peer = str(ipaddress.IPv4Address(peer))
        address=ipaddress.IPv4Address(self.peer)
        if address.is_unspecified or address.is_loopback or address.is_multicast or int(address)==0xffffffff:
            raise ValueError('组网入口需要手机单播IPv4')
        self.listener, self.target, self.bridge_fn = listener, target, bridge_fn
        self.stopping = threading.Event()
        self.lock = threading.RLock()
        self.threads = set()
        self.native = set()
        self.capacity = threading.BoundedSemaphore(32)
        self.error = ''
        self.connection_error = ''
        self.failures = 0
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            while not self.stopping.is_set():
                try:
                    incoming, peer = self.listener.accept()
                except BlockingIOError:
                    self.stopping.wait(.05); continue
                except OSError as exc:
                    if not self.stopping.is_set(): self.error = str(exc)
                    return
                if peer != self.peer or not self.capacity.acquire(blocking=False):
                    incoming.close(); continue
                thread = threading.Thread(target=self.handle,args=(incoming,),daemon=True)
                with self.lock:
                    self.threads.add(thread)
                    thread.start()
        finally:
            self.listener.close()

    def handle(self, incoming):
        native = None
        try:
            if self.stopping.is_set(): return
            native = socket.create_connection(('127.0.0.1',self.target),timeout=2)
            native.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            with self.lock:
                if self.stopping.is_set(): return
                self.native.add(native)
                self.connection_error = ''
            self.bridge_fn(incoming,native,self.stopping)
        except OSError as exc:
            if not self.stopping.is_set():
                with self.lock:
                    self.connection_error = str(exc)
                    self.failures += 1
        finally:
            incoming.close()
            if native is not None:
                native.close()
                with self.lock: self.native.discard(native)
            self.capacity.release()
            with self.lock: self.threads.discard(threading.current_thread())

    def close(self):
        self.stopping.set()
        self.thread.join(1)
        with self.lock:
            for native in self.native:
                native.close()
            threads = list(self.threads)
        deadline = time.monotonic()+3
        for thread in threads: thread.join(max(0,deadline-time.monotonic()))
        if self.thread.is_alive() or any(t.is_alive() for t in threads):
            raise RuntimeError('ZeroTier入口连接未在停止期限内结束')


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
                self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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
            except OSError as exc:
                print("ZeroTier TCP 连接失败：", str(exc), file=sys.stderr, flush=True)
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
    ingresses = {}
    app_error = ''

    def reconcile_ingress(name, desired, address):
        current = ingresses.get(name)
        signature = (address,desired,config.get('phone_ip'))
        if current and (current[0] != signature or current[1].error):
            current[1].close(); del ingresses[name]; current=None
        if not desired: return
        if current: return
        port,target = desired
        listener=sdk.stream()
        try:
            sdk.check(sdk.lib.zts_bind(listener.fd,address.encode(),port))
            sdk.check(sdk.lib.zts_listen(listener.fd,16))
            sdk.check(sdk.lib.zts_set_blocking(listener.fd,0))
            ingresses[name]=(signature,Ingress(listener,target,config.get('phone_ip','')))
        except BaseException:
            listener.close(); raise

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
            proxy_target=int(control.get('proxy_local_port') or 0)
            desired_proxy=(int(config['proxy_port']),proxy_target) if ready and proxy_target else None
            desired_app=(int(config.get('app_port') or 0),int(config.get('app_port') or 0)) if ready and config.get('app_port') and config.get('phone_ip') else None
            app_error = 'App入口需要配置手机组网IPv4' if config.get('app_port') and not config.get('phone_ip') else ''
            for name,desired in (('proxy',desired_proxy),('app',desired_app)):
                reconcile_ingress(name,desired,address)
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
                    proxy_port=config["proxy_port"] if 'proxy' in ingresses else 0,
                    app_port=config.get('app_port',0) if 'app' in ingresses else 0,
                    app_error=(ingresses['app'][1].connection_error if 'app' in ingresses else app_error),
                ),
            )
            stopping.wait(1)
    finally:
        stopping.set()
        server.shutdown()
        server.server_close()
        for _signature, ingress in ingresses.values():
            ingress.close()
        sdk.lib.zts_node_stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    run(json.loads(Path(args.config).read_text()))
