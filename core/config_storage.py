"""配置JSON读取与原子保存，校验文件类型、体积和编码。"""

import json
import os
from pathlib import Path
import stat
import tempfile

MAX_CONFIG_BYTES = 1024 * 1024


class ConfigStorageError(Exception):
    """配置文件读取与保存失败。"""


def _read(path: Path, *, optional: bool = False) -> tuple[dict, bytes]:
    if path.is_symlink():
        raise ConfigStorageError("配置文件不能是符号链接")
    if not path.exists():
        if optional:
            return {}, b""
        raise ConfigStorageError("未找到配置文件")
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ConfigStorageError("配置路径必须是普通文件")
        with path.open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise ConfigStorageError("配置文件超过 1 MiB")
        result = json.loads(data.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigStorageError("读取配置失败，请检查 JSON 格式与文件权限") from exc
    if not isinstance(result, dict):
        raise ConfigStorageError("配置内容必须是 JSON 对象")
    return result, data


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
