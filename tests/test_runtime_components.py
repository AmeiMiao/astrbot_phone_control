"""官方组件获取的持久化、失败隔离与归档边界。"""

import asyncio
import hashlib
import io
import json
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.config import PhoneConfig
from core.runtime_components import ComponentError, RuntimeComponents, official_url, platform_id


def elf(machine=62):
    data = bytearray(128)
    data[:6] = b"\x7fELF\x02\x01"
    data[18:20] = machine.to_bytes(2, "little")
    return bytes(data)


def package(group, payload=None):
    output = io.BytesIO()
    if group == "adb":
        with zipfile.ZipFile(output, "w") as z:
            z.writestr("platform-tools/adb", payload or elf())
            z.writestr("platform-tools/NOTICE.txt", "official license")
            z.writestr("platform-tools/lib64/libadbmdns.so", "support")
    else:
        with tarfile.open(fileobj=output, mode="w:gz") as z:
            for name in ("tailscale", "tailscaled"):
                data = payload or elf()
                member = tarfile.TarInfo("release/" + name)
                member.size = len(data)
                z.addfile(member, io.BytesIO(data))
    return output.getvalue()


def mock_source(monkeypatch, manager, group, content=None, digest=None):
    content = content or package(group)
    calls = []
    plan = ("https://pkgs.tailscale.com/stable/test.tgz", "sha256", digest or hashlib.sha256(content).hexdigest(), "1.2.3")
    monkeypatch.setattr(manager, "_" + group + "_plan", lambda: plan)

    def download(url, destination, **kwargs):
        calls.append(url)
        Path(destination).write_bytes(content)
    monkeypatch.setattr(manager, "_download", download)
    return calls


def test_official_download_persists_cache_and_support_files(tmp_path, monkeypatch):
    manager = RuntimeComponents(tmp_path, host="linux-x86_64")
    calls = mock_source(monkeypatch, manager, "adb")
    manager._install("adb")
    assert manager.path("adb").read_bytes() == elf()
    assert manager.path("adb").stat().st_mode & 0o111
    assert (manager.path("adb").parent / "lib64/libadbmdns.so").read_text() == "support"
    manifest = json.loads((manager.path("adb").parents[1] / "manifest.json").read_text())
    assert manifest["version"] == "1.2.3"
    manager._install("adb")
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["checksum", "architecture", "truncated"])
def test_invalid_artifact_never_replaces_existing_files(tmp_path, monkeypatch, failure):
    manager = RuntimeComponents(tmp_path, host="linux-x86_64")
    target = manager.path("tailscale")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"preserve-existing-cli")
    content = package("tailscale", elf(183)) if failure == "architecture" else b"bad" if failure == "truncated" else package("tailscale")
    mock_source(monkeypatch, manager, "tailscale", content, "0" * 64 if failure == "checksum" else None)
    with pytest.raises((ComponentError, tarfile.ReadError)):
        manager._install("tailscale")
    assert target.read_bytes() == b"preserve-existing-cli"
    assert not manager.path("tailscaled").exists()


@pytest.mark.parametrize("unsafe", ["../escape", "/escape", "platform-tools/../../escape", "C:/escape", "platform-tools\\escape"])
def test_zip_traversal_rejected(tmp_path, unsafe):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(unsafe, b"content")
    with pytest.raises(ComponentError):
        RuntimeComponents(tmp_path)._extract(archive, tmp_path / "ready", "adb")
    assert not (tmp_path / "escape").exists()


def test_tar_symlink_rejected(tmp_path):
    archive = tmp_path / "source.tgz"
    with tarfile.open(archive, "w:gz") as z:
        member = tarfile.TarInfo("release/tailscale")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        z.addfile(member)
    with pytest.raises(ComponentError):
        RuntimeComponents(tmp_path)._extract(archive, tmp_path, "tailscale")


@pytest.mark.parametrize("url", ["http://dl.google.com/a", "https://evil.test/a", "https://dl.google.com.evil.test/a", "https://token@pkgs.tailscale.com/a", "file:///etc/passwd"])
def test_reject_untrusted_source_and_redirect(url):
    with pytest.raises(ComponentError):
        official_url(url)


def test_concurrent_install_downloads_one_archive(tmp_path, monkeypatch):
    a = RuntimeComponents(tmp_path, host="linux-x86_64")
    b = RuntimeComponents(tmp_path, host="linux-x86_64")
    calls = mock_source(monkeypatch, a, "tailscale")
    monkeypatch.setattr(b, "_tailscale_plan", a._tailscale_plan)
    monkeypatch.setattr(b, "_download", a._download)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda m: m._install("tailscale"), [a, b]))
    assert len(calls) == 1 and a.path("tailscaled").is_file()


def test_disabled_capabilities_do_not_download(tmp_path, monkeypatch):
    m = RuntimeComponents(tmp_path)
    monkeypatch.setattr(m, "_install", lambda *_: pytest.fail("disabled capability downloaded"))
    asyncio.run(m.prepare(PhoneConfig.from_mapping({})))
    assert m.status() == {}


def test_manual_libzt_is_required_without_download(tmp_path, monkeypatch):
    m = RuntimeComponents(tmp_path)
    monkeypatch.setattr(m, "_install", lambda *_: pytest.fail("libzt must be manual"))
    c = PhoneConfig.from_mapping({"mesh": {"enabled": True, "provider": "zerotier"}})
    with pytest.raises(ComponentError, match="手动"):
        asyncio.run(m.prepare(c))
    lib = tmp_path / "manual.so"; lib.write_bytes(b"manual")
    c = PhoneConfig.from_mapping({"mesh": {"enabled": True, "provider": "zerotier", "zerotier_library": str(lib)}})
    asyncio.run(m.prepare(c))
    assert m.status()["libzt"]["state"] == "ready"


def test_manual_adb_missing_is_explicit_error(tmp_path, monkeypatch):
    m = RuntimeComponents(tmp_path)
    monkeypatch.setattr(m, "_install", lambda *_: pytest.fail("explicit path must not download"))
    c = PhoneConfig.from_mapping({"adb": {"host": "127.0.0.1", "port": 5555, "path": str(tmp_path / "missing")}})
    with pytest.raises(ComponentError, match="手动路径不存在"):
        asyncio.run(m.prepare(c))
    assert m.status()["adb"]["state"] == "error"


def test_unsupported_architecture_is_actionable(tmp_path):
    m = RuntimeComponents(tmp_path, host="linux-arm64")
    with pytest.raises(ComponentError, match="手动"):
        m._adb_plan()
    with pytest.raises(ComponentError, match="程序路径"):
        RuntimeComponents(tmp_path, host="windows-x86_64")._tailscale_plan()
    assert platform_id("Linux", "aarch64") == "linux-arm64"


def test_cancelled_install_does_not_publish_files(tmp_path, monkeypatch):
    m = RuntimeComponents(tmp_path, host="linux-x86_64")
    mock_source(monkeypatch, m, "adb")
    m.cancel()
    with pytest.raises(ComponentError, match="取消"):
        m._install("adb")
    assert not m.path("adb").exists()
