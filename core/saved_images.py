"""图片库：持久保存图片信息、来源与路径，支持服务端、手机或两地保存。"""

import asyncio
import hashlib
import json
import os
import re
import shlex
import time
from pathlib import Path

from .errors import PhoneControlError

MAX_IMAGE_BYTES = 20 * 1024 * 1024


def image_suffix(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise PhoneControlError("素材格式不是 PNG/JPEG/GIF/WebP 原图")


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    temporary.replace(path)


class SavedImages:
    def __init__(self, service):
        self.service = service
        self.root = service.data_dir / "saved_images"
        self.catalog = self.root / "index.json"
        self.lock = asyncio.Lock()

    def load(self):
        if not self.catalog.exists():
            return {}
        try:
            data = json.loads(self.catalog.read_text())
        except (OSError, ValueError) as exc:
            raise PhoneControlError("图片库索引无法读取，请检查存储") from exc
        if not isinstance(data, dict) or any(
            not isinstance(v, dict) for v in data.values()
        ):
            raise PhoneControlError("图片库索引结构错误")
        return data

    def write(self, records):
        private_write(
            self.catalog, json.dumps(records, ensure_ascii=False, indent=2).encode()
        )

    async def to_phone(self, path, digest):
        service = self.service
        service.guard_device("save_image")
        await service.adb.shell("mkdir -p /sdcard/Pictures/AstrBot")
        directory = (
            await service.adb.shell("readlink -f /sdcard/Pictures/AstrBot")
        ).strip()
        if (
            not directory.startswith("/storage/")
            or not directory.endswith("/Pictures/AstrBot")
            or ".." in directory.split("/")
        ):
            raise PhoneControlError("无法确认手机相册目录的规范路径")
        destination = f"{directory}/{digest}{path.suffix}"
        present = await service.adb.shell(
            f"if [ -f {shlex.quote(destination)} ]; then sha256sum {shlex.quote(destination)}; fi"
        )
        if not present.startswith(digest + " "):
            service.guard_device("save_image")
            await service.adb.run(["push", str(path), destination])
            actual = await service.adb.shell(f"sha256sum {shlex.quote(destination)}")
            if not actual.startswith(digest + " "):
                raise PhoneControlError("手机端文件校验失败，未确认保存成功")
        service.guard_device("save_image")
        await service.adb.shell(
            "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d "
            + shlex.quote("file://" + destination)
        )
        query = (
            "content query --uri content://media/external/images/media --projection _id:_data --where "
            + shlex.quote("_data='" + destination.replace("'", "''") + "'")
        )
        for _ in range(20):
            service.guard_device("save_image")
            if destination in await service.adb.shell(query):
                return destination
            await asyncio.sleep(0.25)
        raise PhoneControlError("图片已写入手机，但相册索引未确认；请检查设备媒体服务")

    def find(self, query="", limit=10):
        needle = str(query).strip().lower()
        records = sorted(
            self.load().values(), key=lambda x: x.get("updated_at", 0), reverse=True
        )
        return [
            r
            for r in records
            if not needle or needle in json.dumps(r, ensure_ascii=False).lower()
        ][: max(1, min(int(limit), 30))]

    def local_path(self, record):
        path = Path(record.get("server_path") or "").resolve()
        if path.parent != (self.root / "files").resolve():
            raise PhoneControlError("这张图片没有可用的服务端原图")
        return path

    async def update_semantics(self, image_id, *, description, reason, tags, kind):
        if not isinstance(description, str) or not description.strip() or len(description) > 500:
            raise PhoneControlError("图片语义需为 1–500 字")
        if not isinstance(reason, str) or len(reason) > 500:
            raise PhoneControlError("保存理由最多 500 字")
        if not isinstance(kind, str) or kind not in {"image", "sticker"}:
            raise PhoneControlError("图片类型需为 image 或 sticker")
        if not isinstance(tags, list) or len(tags) > 10 or any(
            not isinstance(tag, str) or len(tag) > 40 for tag in tags
        ):
            raise PhoneControlError("标签最多 10 个，每个最多 40 字")
        async with self.lock:
            records = self.load()
            if image_id not in records:
                raise PhoneControlError("图片编号不存在，请刷新列表")
            record = records[image_id]
            record.update(description=description.strip(), reason=reason.strip(),
                          tags=list(dict.fromkeys(t.strip() for t in tags if t.strip())),
                          kind=kind, updated_at=time.time())
            self.write(records)
            return record

    async def delete(self, image_id):
        """移除图片库记录及服务端原图，手机相册副本由手机管理。"""
        async with self.lock:
            records = self.load()
            record = records.get(image_id)
            if record is None:
                raise PhoneControlError("图片编号不存在，请刷新列表")
            path = self.local_path(record) if record.get("server_path") else None
            # 先把文件移到库内隔离位置，索引保存失败时恢复原图。
            staged = path.with_suffix(path.suffix + ".deleting") if path else None
            moved = path is not None and path.exists()
            if moved:
                path.replace(staged)
            try:
                del records[image_id]
                self.write(records)
            except OSError:
                if moved:
                    staged.replace(path)
                raise
            if moved:
                try:
                    staged.unlink()
                except OSError:
                    records[image_id] = record
                    staged.replace(path)
                    self.write(records)
                    raise
            return record

    async def preview(self, image_id):
        """读取索引内已校验的原图，供面板登录用户查看。"""
        async with self.lock:
            record = self.load().get(image_id)
            if record is None:
                raise PhoneControlError("图片编号不存在，请刷新列表")
            if record.get("server_path"):
                path = self.local_path(record)
                if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                    raise PhoneControlError("服务端原图不可用")
                data = path.read_bytes()
            else:
                # 手机原图按保存时的设备与哈希读取，不改变前台或手机文件。
                service = self.service
                digest = str(record.get("sha256") or "")
                phone_path = str(record.get("phone_path") or "")
                expected = r"/storage/emulated/[0-9]+/Pictures/AstrBot/" + re.escape(digest) + r"\.(?:png|jpg|gif|webp)"
                if not re.fullmatch(r"[0-9a-f]{64}", digest) or not re.fullmatch(expected, phone_path):
                    raise PhoneControlError("手机原图路径不可用")
                if not record.get("phone_device") or record["phone_device"] != service.adb.serial:
                    raise PhoneControlError("请连接保存这张图片时使用的手机")
                if getattr(getattr(service, "gate", None), "busy", False):
                    raise PhoneControlError("手机正在执行任务，请稍后查看图片")
                service.guard_device("preview_image")
                data = await service.adb.exec_out("head", "-c", str(MAX_IMAGE_BYTES + 1), phone_path)
                service.guard_device("preview_image")
                if record["phone_device"] != service.adb.serial:
                    raise PhoneControlError("手机设备已切换，请刷新图片列表")
            if len(data) > MAX_IMAGE_BYTES or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise PhoneControlError("服务端原图校验失败")
            return data, image_suffix(data)

    async def send_to_phone(self, image_id):
        if not self.service.config.browse.auto_save_images:
            raise PhoneControlError("图片保存已关闭，请开启 browse.auto_save_images")
        async with self.lock:
            records = self.load()
            record = records.get(str(image_id))
            if record is None:
                raise PhoneControlError("图片编号不存在，请先查询图片库")
            path = Path(record.get("server_path") or "").resolve()
            if path.parent != (self.root / "files").resolve() or not path.is_file():
                raise PhoneControlError("这张图片没有可用的服务端原图")
            if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
                raise PhoneControlError("服务端原图校验失败")
            try:
                record["phone_path"] = await self.to_phone(path, record["sha256"])
                record["last_error"] = ""
            except PhoneControlError as exc:
                record["last_error"] = str(exc)
                self.write(records)
                raise
            record["phone_device"] = str(
                getattr(self.service.adb, "serial", "")
                or self.service.config.adb.target
            )
            record["updated_at"] = time.time()
            self.write(records)
            return record

    async def save(self, event, task, path, *, index, description, reason, kind, tags):
        data = path.read_bytes()
        suffix = image_suffix(data)
        digest = hashlib.sha256(data).hexdigest()
        config = self.service.config.browse
        async with self.lock:
            records = self.load()
            record = records.get(digest) or {
                "id": digest,
                "sha256": digest,
                "created_at": time.time(),
                "server_path": "",
                "phone_path": "",
                "sources": [],
            }
            source = {
                "app": task["package"],
                "content_id": task["content_id"],
                "session": str(getattr(event, "unified_msg_origin", "") or ""),
                "image_index": index,
            }
            record.update(
                description=description,
                reason=reason,
                kind=kind,
                tags=tags,
                bytes=len(data),
                format=suffix.lstrip("."),
                updated_at=time.time(),
                storage_location="both"
                if config.save_images_both
                else config.image_storage_location,
                save_both=config.save_images_both,
            )
            if source not in record["sources"]:
                record["sources"] = (record["sources"] + [source])[-20:]
            if config.image_storage_location == "server" or config.save_images_both:
                target = self.root / "files" / (digest + suffix)
                if (
                    not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest() != digest
                ):
                    private_write(target, data)
                record["server_path"] = str(target)
            records[digest] = record
            # 先保存已落地的服务端信息，手机失败时仍能查询和重试。
            self.write(records)
            if config.image_storage_location == "phone" or config.save_images_both:
                try:
                    record["phone_path"] = await self.to_phone(path, digest)
                    record["phone_device"] = str(
                        getattr(self.service.adb, "serial", "")
                        or self.service.config.adb.target
                    )
                    record["last_error"] = ""
                except PhoneControlError as exc:
                    record["last_error"] = str(exc)
                    self.write(records)
                    raise
            record["last_error"] = ""
            self.write(records)
            return record


def image_library(service):
    if not hasattr(service, "_image_library"):
        service._image_library = SavedImages(service)
    return service._image_library


async def save_content_image(
    service,
    event,
    *,
    task_id="",
    index=0,
    reason="",
    description="",
    kind="image",
    tags=(),
):
    if not service.config.browse.auto_save_images:
        raise PhoneControlError("图片保存已关闭，可在 browse.auto_save_images 开启")
    reason, description = str(reason).strip(), str(description).strip()
    if not reason or not description:
        raise PhoneControlError("保存前需填写图片内容描述与喜欢的具体理由")
    if kind not in {"image", "sticker"}:
        raise PhoneControlError("图片类型需为 image 或 sticker")
    if not isinstance(tags, (list, tuple)):
        raise PhoneControlError("tags 需为字符串数组")
    service.guard_device("save_image")
    task = service.contents.current(event, task_id)
    if task["kind"] != "post" or not task["summaries"]:
        raise PhoneControlError("先理解当前图文内容，再按兴趣选择原图保存")
    if (await service.actions.foreground())[0] != task["package"]:
        raise PhoneControlError("前台 App 已改变，请重新确认图文内容")
    _text, images = service.contents.post_material(task)
    if not 0 <= index < len(images):
        raise PhoneControlError(
            f"原图编号超出范围，当前有 {len(images)} 张已关联原图；无原图时可观察 App 原生保存入口"
        )
    path = Path(images[index]).resolve()
    if (
        path.parent != (service.contents.root / task["id"]).resolve()
        or not path.is_file()
    ):
        raise PhoneControlError("原图不属于当前内容任务")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise PhoneControlError("原图超过 20 MiB 保存上限")
    record = await image_library(service).save(
        event,
        task,
        path,
        index=index,
        description=description[:500],
        reason=reason[:500],
        kind=kind,
        tags=[str(t)[:40] for t in tags[:10]],
    )
    return {
        **record,
        "saved_to": record["server_path"]
        if service.config.browse.save_images_both
        or service.config.browse.image_storage_location == "server"
        else record["phone_path"],
        "note": "图片信息、来源与存储路径已入库；可用 phone_saved_images 查询或把服务端原图传到手机。",
    }
