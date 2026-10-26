"""虚拟网入口使用真实TCP验证负载、来源限制和停止资源回收。"""
import socket
import threading
import time
from core.mesh_zerotier_worker import Ingress

class Listener:
    def __init__(self, peer):
        self.socket=socket.socket();self.socket.bind(('127.0.0.1',0));self.socket.listen();self.socket.setblocking(False)
        self.address=self.socket.getsockname();self.peer=peer
    def accept(self):
        s,_=self.socket.accept();return s,self.peer
    def close(self):self.socket.close()


def pump(left,right,stop):
    left.settimeout(.05)
    try:
        while not stop.is_set():
            try:chunk=left.recv(65536)
            except socket.timeout:continue
            if not chunk:break
            right.sendall(chunk);left.sendall(right.recv(len(chunk)))
    finally:left.close();right.close()


def echo_server():
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();closed=threading.Event()
    def run():
        c,_=listener.accept()
        try:
            while b:=c.recv(65536):c.sendall(b)
        finally:c.close();closed.set()
    t=threading.Thread(target=run,daemon=True);t.start();return listener,closed


def test_ingress_opaque_bytes_and_stop_closes_active_channel():
    target,ended=echo_server();source=Listener('10.66.0.2')
    ingress=Ingress(source,target.getsockname()[1],'10.66.0.2',pump)
    c=socket.create_connection(source.address);c.settimeout(2)
    try:
        payload=bytes(range(256))*64;c.sendall(payload)
        received=bytearray()
        while len(received)<len(payload):received.extend(c.recv(65536))
        assert bytes(received)==payload
        ingress.close();assert c.recv(1)==b'';assert ended.wait(1)
        assert not ingress.threads and not ingress.native
    finally:c.close();ingress.close();target.close()


def test_ingress_rejects_wrong_phone_before_connecting_local_target():
    target=socket.socket();target.bind(('127.0.0.1',0));target.listen();target.settimeout(.2)
    source=Listener('10.66.0.3');ingress=Ingress(source,target.getsockname()[1],'10.66.0.2',pump)
    c=socket.create_connection(source.address);c.settimeout(1)
    try:
        assert c.recv(1)==b''
        try:target.accept()
        except socket.timeout:pass
        else:raise AssertionError('错误来源连接了主机服务')
    finally:c.close();ingress.close();target.close()
