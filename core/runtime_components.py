"""按需从官方来源准备运行组件；下载目录随插件数据保留。"""

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

from .errors import PhoneControlError


class ComponentError(PhoneControlError):
    pass


def platform_id(system=None, machine=None):
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64", "i686": "x86", "i386": "x86"}.get(machine, machine)
    return system + "-" + machine


def official_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password
        or parsed.hostname not in {"dl.google.com", "dl-ssl.google.com", "redirector.gvt1.com", "pkgs.tailscale.com"}):
        raise ComponentError("组件下载地址不是受支持的官方 HTTPS 来源")
    return url


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return super().redirect_request(request, fp, code, msg, headers, official_url(newurl))


class RuntimeComponents:
    def __init__(self, root, *, host=None):
        self.root = Path(root)
        self.host = host or platform_id()
        self.states = {}
        self._lock = asyncio.Lock()
        self._cancel = threading.Event()

    def path(self, name):
        group = "adb" if name == "adb" else "tailscale"
        suffix = ".exe" if self.host.startswith("windows-") else ""
        relative = Path("platform-tools") / ("adb" + suffix) if name == "adb" else Path(name + suffix)
        return self.root / self.host / group / relative

    def resolve(self, name, configured=""):
        if configured:
            return Path(configured).expanduser()
        managed = self.path(name)
        if managed.is_file():
            return managed
        system = shutil.which(name)
        return Path(system) if system else managed

    def status(self):
        return {key: dict(value) for key, value in self.states.items()}

    def cancel(self):
        self._cancel.set()

    def needs_prepare(self, config):
        wanted = [("adb", config.adb.path)] if config.adb.configured else []
        if config.mesh.enabled and config.mesh.provider == "tailscale":
            wanted.extend([("tailscale", config.mesh.tailscale_path), ("tailscaled", config.mesh.tailscaled_path)])
        if any(not self.resolve(name, path).is_file() for name, path in wanted):
            return True
        return bool(config.mesh.enabled and config.mesh.provider == "zerotier" and (
            not config.mesh.zerotier_library or not Path(config.mesh.zerotier_library).expanduser().is_file()
        ))

    async def prepare(self, config):
        async with self._lock:
            if self._cancel.is_set():
                raise ComponentError("组件准备已取消，请重新加载插件")
            self.states.clear()
            wanted = []
            if config.adb.configured:
                wanted.append(("adb", config.adb.path))
            if config.mesh.enabled and config.mesh.provider == "tailscale":
                wanted.extend([("tailscale", config.mesh.tailscale_path), ("tailscaled", config.mesh.tailscaled_path)])
            for name, configured in wanted:
                candidate = self.resolve(name, configured)
                if candidate.is_file():
                    self.states[name] = {"state": "ready", "path": str(candidate)}
                    continue
                if configured:
                    self.states[name] = {"state": "error", "message": "手动配置的程序文件不存在", "path": str(candidate)}
                    raise ComponentError(f"{name} 手动路径不存在，请修正配置后重载")
                self.states[name] = {"state": "downloading", "message": "正在获取官方组件"}
                job = asyncio.create_task(asyncio.to_thread(self._install, "adb" if name == "adb" else "tailscale"))
                try:
                    await asyncio.shield(job)
                except asyncio.CancelledError:
                    self.cancel()
                    # 线程有界读取并检查取消，不在停用后发布文件。
                    job.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                    raise
                except Exception as exc:
                    message = str(exc) if isinstance(exc, ComponentError) else "官方下载失败，请检查网络、磁盘和系统权限后重载"
                    self.states[name] = {"state": "error", "message": message}
                    raise ComponentError(f"{name}：{message}") from exc
                self.states[name] = {"state": "ready", "path": str(self.path(name))}
            if config.mesh.enabled and config.mesh.provider == "zerotier":
                library = Path(config.mesh.zerotier_library).expanduser()
                exists = bool(config.mesh.zerotier_library and library.is_file())
                self.states["libzt"] = {"state": "ready" if exists else "error", "message": "使用手动提供的动态库" if exists else "请填写手动准备的 ZeroTier SDK 动态库绝对路径"}
                if not exists:
                    raise ComponentError(self.states["libzt"]["message"])

    def _download(self, url, destination=None, *, limit=8 * 1024 * 1024):
        official_url(url)
        opener = urllib.request.build_opener(OfficialRedirect())
        request = urllib.request.Request(url, headers={"User-Agent": "AstrBot-Phone-Components/1.0"})
        started = time.monotonic()
        result = bytearray()
        with opener.open(request, timeout=25) as response:
            total = int(response.headers.get("Content-Length") or 0)
            if total > limit:
                raise ComponentError("官方组件超过允许的下载大小")
            with (Path(destination).open("wb") if destination else contextlib.nullcontext(None)) as output:
                count = 0
                while True:
                    if self._cancel.is_set():
                        raise ComponentError("组件准备已取消")
                    if time.monotonic() - started > 300:
                        raise ComponentError("组件下载超过 300 秒，请检查网络后重载")
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > limit:
                        raise ComponentError("官方组件超过允许的下载大小")
                    if output:
                        output.write(chunk)
                    else:
                        result.extend(chunk)
        if total and count != total:
            raise ComponentError("官方组件下载不完整，请重新加载重试")
        return bytes(result)

    def _adb_plan(self):
        host = {"linux-x86_64": "linux", "windows-x86_64": "windows", "windows-x86": "windows", "darwin-x86_64": "macosx", "darwin-arm64": "macosx"}.get(self.host)
        if host is None:
            raise ComponentError(f"Google 未提供 {self.host} 的 ADB 官方平台包，请手动填写 adb.path")
        root = ET.fromstring(self._download("https://dl.google.com/android/repository/repository2-1.xml"))
        for package in root.iter("remotePackage"):
            if package.get("path") != "platform-tools":
                continue
            revision = package.find("revision")
            version = ".".join(revision.findtext(key) or "0" for key in ("major", "minor", "micro"))
            for archive in package.findall("./archives/archive"):
                if archive.findtext("host-os") != host:
                    continue
                complete = archive.find("complete")
                name = complete.findtext("url")
                checksum = complete.find("checksum")
                algorithm = checksum.get("type", "sha1")
                digest = (checksum.text or "").strip()
                if algorithm not in {"sha1", "sha256"} or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", digest):
                    raise ComponentError("Google 平台包校验信息无效")
                return "https://dl.google.com/android/repository/" + name, algorithm, digest.lower(), version
        raise ComponentError("Google 官方仓库未列出当前平台的 ADB 下载")

    def _tailscale_plan(self):
        if not self.host.startswith("linux-"):
            raise ComponentError(f"Tailscale 官方独立 CLI/daemon 便携包适用于 Linux；{self.host} 请安装官方客户端并填写两个程序路径")
        arch = {"x86_64": "amd64", "x86": "386", "arm64": "arm64", "armv7l": "arm", "riscv64": "riscv64"}.get(self.host[6:])
        info = json.loads(self._download("https://pkgs.tailscale.com/stable/?mode=json"))
        name = info.get("Tarballs", {}).get(arch)
        if not name or PurePosixPath(name).name != name or not name.endswith(".tgz"):
            raise ComponentError(f"Tailscale 官方未提供 {self.host} 便携包，请手动填写程序路径")
        url = "https://pkgs.tailscale.com/stable/" + name
        digest = self._download(url + ".sha256", limit=4096).decode().strip().split()[0]
        if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            raise ComponentError("Tailscale 官方 SHA256 信息无效")
        return url, "sha256", digest.lower(), info["TarballsVersion"]

    @staticmethod
    def _safe_name(name):
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ComponentError("官方归档包含非法路径")
        return path

    def _extract(self, archive, target, group):
        total = 0
        if group == "adb":
            with zipfile.ZipFile(archive) as source:
                seen = set()
                if len(source.infolist()) > 10000:
                    raise ComponentError("官方归档文件数超限")
                for item in source.infolist():
                    name = self._safe_name(item.filename)
                    if stat.S_ISLNK(item.external_attr >> 16):
                        raise ComponentError("官方归档不允许符号链接")
                    if not name.parts or name.parts[0] != "platform-tools" or item.is_dir():
                        continue
                    if str(name) in seen:
                        raise ComponentError("官方归档包含重复文件")
                    seen.add(str(name))
                    total += item.file_size
                    if total > 512 * 1024 * 1024:
                        raise ComponentError("官方归档解压大小超限")
                    destination = target.joinpath(*name.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(item) as reader, destination.open("wb") as writer:
                        shutil.copyfileobj(reader, writer)

        else:
            with tarfile.open(archive, "r:gz") as source:
                seen = set()
                for index, item in enumerate(source):
                    if index > 10000:
                        raise ComponentError("官方归档文件数超限")
                    name = self._safe_name(item.name)
                    if item.issym() or item.islnk():
                        raise ComponentError("官方归档不允许链接")
                    if not item.isfile() or name.name not in {"tailscale", "tailscaled", "LICENSE", "LICENSE.txt", "NOTICE"}:
                        continue
                    if name.name in seen:
                        raise ComponentError("官方归档存在重复程序文件")
                    seen.add(name.name)
                    total += item.size
                    if total > 512 * 1024 * 1024:
                        raise ComponentError("官方归档解压大小超限")
                    with source.extractfile(item) as reader, (target / name.name).open("wb") as writer:
                        shutil.copyfileobj(reader, writer)


    def _validate_binary(self, path):
        with path.open("rb") as source:
            header = source.read(64)
        if self.host.startswith("linux-"):
            arch = {"x86_64": 62, "arm64": 183, "x86": 3, "armv7l": 40, "riscv64": 243}.get(self.host[6:])
            endian = "little" if header[5:6] == b"\x01" else "big"
            valid = header[:4] == b"\x7fELF" and int.from_bytes(header[18:20], endian) == arch
        elif self.host.startswith("windows-"):
            valid = header[:2] == b"MZ"
        else:
            valid = header[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
        if not valid:
            raise ComponentError("官方下载程序格式或架构不匹配，未安装")

    def _install(self, group):
        parent = self.root / self.host
        parent.mkdir(parents=True, exist_ok=True)
        lock_path = parent / (group + ".lock")
        with lock_path.open("a+b") as lock:
            if os.name == "posix":
                import fcntl
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if self._cancel.wait(0.1):
                            raise ComponentError("组件准备已取消")
            else:
                import msvcrt
                lock.seek(0); lock.write(b"0"); lock.flush(); lock.seek(0)
                while True:
                    try:
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if self._cancel.wait(0.1):
                            raise ComponentError("组件准备已取消")
            names = ["adb"] if group == "adb" else ["tailscale", "tailscaled"]
            if all(self.path(name).is_file() for name in names):
                return
            url, algorithm, digest, version = self._adb_plan() if group == "adb" else self._tailscale_plan()
            with tempfile.TemporaryDirectory(prefix=group + "-", dir=parent) as work:
                work = Path(work)
                archive = work / "archive"
                self._download(url, archive, limit=200 * 1024 * 1024)
                if hashlib.new(algorithm, archive.read_bytes()).hexdigest() != digest:
                    raise ComponentError("官方组件校验和不匹配，未安装")
                stage = work / "ready"; stage.mkdir()
                self._extract(archive, stage, group)
                for name in names:
                    path = stage / self.path(name).relative_to(parent / group)
                    if not path.is_file() or path.stat().st_size < 64:
                        raise ComponentError("官方归档缺少必需程序文件")
                    self._validate_binary(path)
                    path.chmod(0o755)
                (stage / "manifest.json").write_text(json.dumps({"source": url, "version": version, "checksum": digest, "algorithm": algorithm}))
                if self._cancel.is_set():
                    raise ComponentError("组件准备已取消")
                target = parent / group
                previous = work / "previous"
                if target.exists():
                    target.rename(previous)
                try:
                    stage.rename(target)
                except BaseException:
                    if previous.exists():
                        previous.rename(target)
                    raise
