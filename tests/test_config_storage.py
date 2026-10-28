"""配置文件读取边界、编码与原子写入失败保护。"""
import json
import stat

import pytest

from core import config_storage as storage


def test_read_keeps_bom_bytes_and_custom_values(tmp_path):
    path=tmp_path/'plugin.json'
    original=b'\xef\xbb\xbf'+json.dumps({'custom':{'enabled':False,'text':'手机'}}).encode()
    path.write_bytes(original)
    value,raw=storage._read(path)
    assert value=={'custom':{'enabled':False,'text':'手机'}} and raw==original
    assert storage._read(tmp_path/'absent.json',optional=True)==({},b'')
    assert list(tmp_path.iterdir())==[path]


@pytest.mark.parametrize('content',[b'not-json',b'[]',b'{"a":"'+b'x'*(1024*1024)+b'"}'])
def test_invalid_config_is_rejected_without_overwriting(tmp_path,content):
    path=tmp_path/'plugin.json';path.write_bytes(content)
    with pytest.raises(storage.ConfigStorageError):storage._read(path)
    assert path.read_bytes()==content


def test_directory_and_symlink_are_rejected(tmp_path):
    source=tmp_path/'source.json';source.write_text('{}')
    link=tmp_path/'link.json';link.symlink_to(source)
    for path in (tmp_path,link):
        with pytest.raises(storage.ConfigStorageError):storage._read(path)
    assert source.read_text()=='{}'


def test_atomic_replace_failure_preserves_target_and_cleans_stage(tmp_path,monkeypatch):
    path=tmp_path/'plugin.json';path.write_bytes(b'original')
    def fail(*args):raise OSError('write failed')
    monkeypatch.setattr(storage.os,'replace',fail)
    with pytest.raises(OSError):storage._atomic_write(path,b'changed')
    assert path.read_bytes()==b'original' and list(tmp_path.iterdir())==[path]


def test_atomic_write_uses_private_file_permissions(tmp_path):
    path=tmp_path/'config'/'plugin.json';storage._atomic_write(path,b'{}')
    assert path.read_bytes()==b'{}' and stat.S_IMODE(path.stat().st_mode)==0o600
