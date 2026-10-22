"""原插件持久化资料迁移：记录合并、路径转换、指纹校验和事务备份。"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config_migration import ConfigMigrationError, SOURCE_PLUGIN, _atomic_write

JSONL_FILES = frozenset(
    {
        "diary/diary.jsonl",
        "chat/chat_memory.jsonl",
        "learning.jsonl",
        "interactions.jsonl",
        "token_usage.jsonl",
        "metrics.jsonl",
        "content_tasks/history.jsonl",
    }
)
JSON_FILES = frozenset(
    {
        "app_memory.json",
        "novelty.json",
        "experience_pending.json",
        "saved_images/index.json",
    }
)
KEY_FILES = frozenset(
    {"adbhome/.android/adbkey", "adbhome/.android/adbkey.pub", "wireguard/keys.json"}
)
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILES = 10000


class DataMigrationError(ConfigMigrationError):
    """资料迁移未完成，源资料保持完整。"""


def _selected(rel: str, include_keys: bool) -> bool:
    return (
        rel in JSONL_FILES
        or rel in JSON_FILES
        or (include_keys and rel in KEY_FILES)
        or bool(
            re.fullmatch(
                r"(?:screenshots|saved_images/files)/[^/]+\.(?:png|jpg|jpeg|gif|webp)",
                rel,
                re.I,
            )
        )
    )


def _root(path: Path, *, required=False):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise DataMigrationError("资料目录不能包含符号链接")
    if required and not path.is_dir():
        raise DataMigrationError(
            "未找到原插件数据目录，请保留 plugin_data/astrbot_phone_control"
        )
    if path.exists() and not path.is_dir():
        raise DataMigrationError("资料目录必须为普通目录")


def _snapshot(root: Path, include_keys: bool) -> dict[str, bytes]:
    result = {}
    total = 0
    if not root.exists():
        return result
    # 只进入有持久化资料的目录，运行期素材与缓存不参与迁移。
    directories = {
        "diary",
        "chat",
        "content_tasks",
        "screenshots",
        "saved_images",
        "saved_images/files",
    }
    if include_keys:
        directories.update({"adbhome", "adbhome/.android", "wireguard"})

    def walk(directory):
        nonlocal total
        for path in sorted(directory.iterdir()):
            rel = path.relative_to(root).as_posix()
            if rel not in directories and not _selected(rel, include_keys):
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise DataMigrationError(f"资料路径不能是符号链接：{rel}")
            if rel in directories:
                if not stat.S_ISDIR(mode):
                    raise DataMigrationError(f"资料分组必须为目录：{rel}")
                walk(path)
                continue
            if not stat.S_ISREG(mode):
                raise DataMigrationError(f"资料路径必须为普通文件：{rel}")
            with path.open("rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if (
                len(data) > MAX_FILE_BYTES
                or total > MAX_TOTAL_BYTES
                or len(result) >= MAX_FILES
            ):
                raise DataMigrationError(
                    "迁移资料超过限制：单文件 64 MiB、总计 512 MiB、10000 个文件"
                )
            result[rel] = data

    walk(root)
    return result


def _json(data: bytes, rel: str):
    try:
        return json.loads(
            data.decode("utf-8-sig"),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError) as exc:
        raise DataMigrationError(f"资料 JSON 格式错误：{rel}") from exc


def _encode(value) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    ).encode()


def _records(data: bytes, rel: str) -> list[dict]:
    result = []
    try:
        lines = data.decode("utf-8-sig").splitlines()
    except UnicodeError as exc:
        raise DataMigrationError(f"资料编码必须为 UTF-8：{rel}") from exc
    for line_number, line in enumerate(lines):
        if not line.strip():
            continue
        value = _json(line.encode(), rel)
        if not isinstance(value, dict):
            raise DataMigrationError(f"资料记录必须为对象：{rel}")
        if rel == "learning.jsonl" and not value.get("id"):
            value["id"] = hashlib.sha256(
                f"{line_number}:{line.strip()}".encode()
            ).hexdigest()
        result.append(value)
    return result


def _merge_records(
    source: list[dict], current: list[dict], key: Callable | None = None
):
    key = key or (
        lambda r: ("id", str(r["id"])) if r.get("id") else ("value", _encode(r))
    )
    merged = list(current)
    seen = {key(r) for r in current}
    for record in source:
        identity = key(record)
        if identity not in seen:
            merged.append(record)
            seen.add(identity)

    # 稳定按事件时间排列，重复 ID 的内容采用目标记录。
    def when(record):
        value = record.get("ts", record.get("ended", 0))
        return float(value) if isinstance(value, (int, float)) else 0

    return sorted(merged, key=when)


def _rebase(value, source: Path, target: Path, renames: dict[str, str]):
    if isinstance(value, dict):
        return {k: _rebase(v, source, target, renames) for k, v in value.items()}
    if isinstance(value, list):
        return [_rebase(v, source, target, renames) for v in value]
    if isinstance(value, str) and value.startswith(str(source) + "/"):
        rel = value[len(str(source)) + 1 :]
        return str(target / renames.get(rel, rel))
    return value


@dataclass
class DataMigrationPlan:
    target: Path
    token: str
    writes: dict[str, bytes] = field(repr=False)
    before: dict[str, bytes] = field(repr=False)
    preserved: tuple[str, ...] = ()
    source_files: int = 0

    def summary(self):
        return {
            "source_plugin": SOURCE_PLUGIN,
            "token": self.token,
            "changed": sorted(self.writes),
            "count": len(self.writes),
            "kept": list(self.preserved),
            "preserved": len(self.preserved),
            "bytes": sum(len(v) for v in self.writes.values()),
            "source_files": self.source_files,
        }


def prepare_data_migration(
    data_parent: Path, target_name: str, *, include_keys=False
) -> DataMigrationPlan:
    if not target_name.isidentifier() or target_name == SOURCE_PLUGIN:
        raise DataMigrationError("目标插件标识无效或与原插件相同")
    parent = Path(data_parent).absolute()
    source, target = parent / SOURCE_PLUGIN, parent / target_name
    _root(source, required=True)
    _root(target)
    original = _snapshot(source, include_keys)
    current = _snapshot(target, include_keys)
    digest = hashlib.sha256()
    for value in (
        target_name.encode(),
        str(parent).encode(),
        str(bool(include_keys)).encode(),
    ):
        digest.update(len(value).to_bytes(8, "big") + value)
    for files in (original, current):
        digest.update(len(files).to_bytes(8, "big"))
        for rel, data in sorted(files.items()):
            for value in (rel.encode(), data):
                digest.update(len(value).to_bytes(8, "big") + value)
    result, kept, renames = {}, [], {}

    for rel, data in original.items():
        if rel in JSONL_FILES or rel in JSON_FILES or rel in KEY_FILES:
            continue
        destination = rel
        if rel in current and current[rel] != data:
            if rel.startswith("saved_images/"):
                raise DataMigrationError("图片库文件内容冲突，请先检查目标图片完整性")
            p = Path(rel)
            destination = str(
                p.with_name(
                    p.stem
                    + "-migrated-"
                    + hashlib.sha256(data).hexdigest()[:16]
                    + p.suffix
                )
            )
            renames[rel] = destination
            kept.append(rel)
        if destination in current and current[destination] != data:
            raise DataMigrationError("截图迁移目标冲突，请检查文件完整性")
        result[destination] = data

    for rel, data in original.items():
        if rel in JSONL_FILES:
            rows = _rebase(_records(data, rel), source, target, renames)
            existing = _records(current.get(rel, b""), rel)
            merged = _merge_records(rows, existing)
            result[rel] = b"".join(_encode(row) for row in merged)
        elif rel in JSON_FILES:
            value = _rebase(_json(data, rel), source, target, renames)
            default = [] if rel == "experience_pending.json" else {}
            existing = _json(current[rel], rel) if rel in current else default
            if rel in {"app_memory.json", "novelty.json"}:

                def entries(payload):
                    rows = (
                        payload.get("entries", [])
                        if isinstance(payload, dict)
                        else payload
                    )
                    if not isinstance(rows, list) or any(
                        not isinstance(row, dict) for row in rows
                    ):
                        raise DataMigrationError(f"资料条目结构错误：{rel}")
                    return rows

                identity = (
                    (
                        lambda r: (
                            str(r.get("app") or "").strip().lower(),
                            " ".join(str(r.get("page") or "").split()),
                        )
                    )
                    if rel == "app_memory.json"
                    else (lambda r: str(r.get("key", "")))
                )
                old_rows, new_rows = entries(existing), entries(value)
                if any(
                    not (r.get("app") if rel == "app_memory.json" else r.get("key"))
                    for r in old_rows + new_rows
                ):
                    raise DataMigrationError(f"资料条目标识缺失：{rel}")
                rows = _merge_records(new_rows, old_rows, identity)
                merged = (
                    copy.deepcopy(existing)
                    if isinstance(existing, dict)
                    else {"version": 1}
                )
                merged["entries"] = rows
            elif rel == "experience_pending.json":
                if (
                    not isinstance(value, list)
                    or not isinstance(existing, list)
                    or any(not isinstance(r, dict) for r in value + existing)
                ):
                    raise DataMigrationError("经验待写队列必须为对象列表")
                merged = _merge_records(value, existing)
            else:
                if not isinstance(value, dict) or not isinstance(existing, dict):
                    raise DataMigrationError("图片库索引必须为对象")
                for image_id, record in value.items():
                    if not re.fullmatch(r"[a-f0-9]{64}", image_id) or not isinstance(
                        record, dict
                    ):
                        raise DataMigrationError("图片库索引标识无效")
                    server = record.get("server_path")
                    if server:
                        file = Path(server)
                        relfile = "saved_images/files/" + file.name
                        blob = original.get(relfile)
                        if (
                            file.parent != target / "saved_images/files"
                            or blob is None
                            or hashlib.sha256(blob).hexdigest() != image_id
                        ):
                            raise DataMigrationError("图片库索引路径或原图哈希不匹配")
                merged = {**value, **existing}
            if rel in current:
                kept.append(rel)
            result[rel] = _encode(merged)

    if include_keys:
        for group in (
            ("adbhome/.android/adbkey", "adbhome/.android/adbkey.pub"),
            ("wireguard/keys.json",),
        ):
            available = [rel for rel in group if rel in original]
            if not available:
                continue
            if any(rel in current for rel in group):
                kept.extend(available)
                continue
            if len(available) != len(group):
                raise DataMigrationError("ADB 私钥与公钥必须同时存在")
            for rel in available:
                if not original[rel].strip():
                    raise DataMigrationError("设备密钥文件不能为空")
                if rel.endswith(".json") and not isinstance(
                    _json(original[rel], rel), dict
                ):
                    raise DataMigrationError("WireGuard 密钥文件必须为对象")
                result[rel] = original[rel]

    # 语义相同时保持目标原有格式，重复导入不会重写文件。
    writes = {}
    for rel, data in result.items():
        old = current.get(rel)
        if old == data:
            continue
        if (
            old is not None
            and rel in JSON_FILES
            and _json(old, rel) == _json(data, rel)
        ):
            continue
        if (
            old is not None
            and rel in JSONL_FILES
            and _records(old, rel) == _records(data, rel)
        ):
            continue
        writes[rel] = data
    return DataMigrationPlan(
        target,
        digest.hexdigest(),
        writes,
        current,
        tuple(sorted(set(kept))),
        len(original),
    )


def apply_data_migration(
    data_parent: Path,
    target_name: str,
    token: str,
    *,
    include_keys=False,
    reload: Callable[[], None] | None = None,
) -> DataMigrationPlan:
    """校验最新快照，持久备份与回滚清单后写入；失败恢复已写文件。"""
    plan = prepare_data_migration(data_parent, target_name, include_keys=include_keys)
    if not token or token != plan.token:
        raise DataMigrationError("资料已变化，请重新预览后再导入")
    if not plan.writes:
        return plan
    backup_parent = plan.target.parent / (target_name + "_migration-backups")
    _root(backup_parent)
    backup_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=plan.token[:12] + "-", dir=backup_parent))
    written = []
    try:
        manifest = {
            "state": "prepared",
            "files": {rel: rel in plan.before for rel in plan.writes},
        }
        for rel, data in plan.before.items():
            _atomic_write(backup / rel, data)
        _atomic_write(backup / "manifest.json", _encode(manifest))
        # 备份期间有外部写入则拒绝，避免覆盖新资料。
        fresh = prepare_data_migration(
            data_parent, target_name, include_keys=include_keys
        )
        if fresh.token != token:
            raise DataMigrationError("资料已变化，请重新预览后再导入")
        for rel, data in plan.writes.items():
            _atomic_write(plan.target / rel, data)
            written.append(rel)
        if reload:
            reload()
        manifest["state"] = "complete"
        _atomic_write(backup / "manifest.json", _encode(manifest))
    except Exception as exc:
        try:
            for rel in reversed(written):
                if rel in plan.before:
                    _atomic_write(plan.target / rel, plan.before[rel])
                else:
                    (plan.target / rel).unlink(missing_ok=True)
            if reload and written:
                reload()
        except Exception as rollback:
            raise DataMigrationError(
                "资料保存及恢复失败，请使用迁移备份目录中的 manifest.json 恢复"
            ) from rollback
        if isinstance(exc, DataMigrationError):
            raise
        raise DataMigrationError(
            "资料导入失败，已恢复目标文件；迁移备份已保留"
        ) from exc
    return plan
