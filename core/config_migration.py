"""原插件配置的独立预览与导入；只补充目标中的默认字段。"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SOURCE_PLUGIN = "astrbot_phone_control"
MAX_CONFIG_BYTES = 1024 * 1024


class ConfigMigrationError(Exception):
    """配置迁移的可预期失败。"""


def _read(path: Path, *, optional: bool = False) -> tuple[dict, bytes]:
    if path.is_symlink():
        raise ConfigMigrationError("配置文件不能是符号链接")
    if not path.exists():
        if optional:
            return {}, b""
        raise ConfigMigrationError(
            "未找到原插件配置，请保留 astrbot_phone_control_config.json"
        )
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ConfigMigrationError("配置路径必须是普通文件")
        with path.open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise ConfigMigrationError("配置文件超过 1 MiB，无法迁移")
        result = json.loads(data.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigMigrationError("读取配置失败，请检查 JSON 格式与文件权限") from exc
    if not isinstance(result, dict):
        raise ConfigMigrationError("配置内容必须是 JSON 对象")
    return result, data


def _fingerprint(source: bytes, target: bytes, schema: dict, name: str) -> str:
    digest = hashlib.sha256()
    for value in (
        source,
        target,
        json.dumps(schema, sort_keys=True).encode(),
        name.encode(),
    ):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def _leaf_valid(value: Any, spec: dict) -> bool:
    kind = spec["type"]
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "float":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if kind == "string" or kind == "text":
        return isinstance(value, str)
    if kind == "list":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return False


@dataclass(frozen=True)
class MigrationPlan:
    source: Path
    target: Path
    token: str
    changed: tuple[str, ...]
    kept: tuple[str, ...]
    merged: dict = field(repr=False)

    def summary(self) -> dict:
        return {
            "source_plugin": SOURCE_PLUGIN,
            "token": self.token,
            "changed": list(self.changed),
            "kept": list(self.kept),
            "count": len(self.changed),
            "preserved": len(self.kept),
        }


def prepare_migration(
    config_dir: Path,
    target_name: str,
    schema: dict,
    *,
    validate: Callable[[dict], None] | None = None,
) -> MigrationPlan:
    """读取固定命名的原配置，仅迁移 schema 中声明且目标仍为默认值的字段。"""
    if target_name == SOURCE_PLUGIN:
        raise ConfigMigrationError("当前插件与配置来源相同，无需迁移")
    if not target_name.isidentifier():
        raise ConfigMigrationError("目标插件标识无效")
    base = config_dir.resolve()
    source = base / f"{SOURCE_PLUGIN}_config.json"
    target = base / f"{target_name}_config.json"
    original, source_bytes = _read(source)
    current, target_bytes = _read(target, optional=True)
    merged = copy.deepcopy(current)
    changed, kept = [], []

    def walk(specs, src, dst, result, prefix=""):
        for key, spec in specs.items():
            path = f"{prefix}.{key}" if prefix else key
            if spec["type"] == "object":
                source_card, target_card = src.get(key, {}), dst.get(key, {})
                if not isinstance(source_card, dict) or not isinstance(
                    target_card, dict
                ):
                    raise ConfigMigrationError(f"配置分组 {path} 必须是对象")
                child = copy.deepcopy(target_card)
                walk(spec["items"], source_card, target_card, child, path)
                result[key] = child
                continue
            default = copy.deepcopy(spec.get("default"))
            current_value = dst.get(key, default)
            result.setdefault(key, copy.deepcopy(current_value))
            if key not in src:
                continue
            value = src[key]
            if not _leaf_valid(value, spec):
                raise ConfigMigrationError(f"原配置字段 {path} 的类型不正确")
            if path == "adb.host" and not value.strip():
                value = default
            if path == "adb.port" and value == 0:
                value = default
            if current_value == value:
                continue
            if current_value == default:
                result[key] = copy.deepcopy(value)
                changed.append(path)
            else:
                kept.append(path)

    walk(schema, original, current, merged)
    if validate is not None:
        validate(merged)
    return MigrationPlan(
        source,
        target,
        _fingerprint(source_bytes, target_bytes, schema, target_name),
        tuple(changed),
        tuple(kept),
        merged,
    )


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


def apply_migration(
    config_dir: Path,
    target_name: str,
    schema: dict,
    token: str,
    *,
    validate: Callable[[dict], None] | None = None,
    persist: Callable[[dict], None] | None = None,
) -> MigrationPlan:
    """重新核对预览指纹，备份目标后原子保存；源文件保持不变。"""
    plan = prepare_migration(config_dir, target_name, schema, validate=validate)
    if not token or token != plan.token:
        raise ConfigMigrationError("配置已变化，请重新预览后再导入")
    if not plan.changed:
        return plan
    try:
        _, before = _read(plan.target, optional=True)
        if before:
            backup = plan.target.with_name(
                f"{target_name}_migration-{plan.token[:16]}.json"
            )
            _atomic_write(backup, before)
        if persist is None:
            _atomic_write(
                plan.target,
                (json.dumps(plan.merged, ensure_ascii=False, indent=2) + "\n").encode(),
            )
        else:
            persist(plan.merged)
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigMigrationError("保存迁移配置失败，目标配置未确认写入") from exc
    return plan
