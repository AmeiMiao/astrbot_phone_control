"""把当前图文任务中已关联的原图保存到手机相册。"""

import asyncio
import hashlib
import shlex
from pathlib import Path

from .errors import PhoneControlError


async def save_content_image(service, event, *, task_id="", index=0, reason=""):
    if not service.config.browse.auto_save_images:
        raise PhoneControlError("自动存图已关闭，可在 browse.auto_save_images 开启")
    if not str(reason).strip():
        raise PhoneControlError("保存前需说明喜欢这张图或表情包的具体理由")
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
    directory = (service.contents.root / task["id"]).resolve()
    if path.parent != directory or not path.is_file():
        raise PhoneControlError("原图不属于当前内容任务")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise PhoneControlError("原图超过 20 MiB 保存上限")
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        suffix = ".gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise PhoneControlError("素材格式不是 PNG/JPEG/GIF/WebP 原图")
    digest = hashlib.sha256(data).hexdigest()
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
    destination = f"{directory}/{digest}{suffix}"
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
        indexed = await service.adb.shell(query)
        if destination in indexed:
            break
        await asyncio.sleep(0.25)
    else:
        raise PhoneControlError("图片已写入手机，但相册索引未确认；请检查设备媒体服务")
    return {
        "saved_to": destination,
        "sha256": digest,
        "image_index": index,
        "gallery_indexed": True,
        "note": "原图与相册索引已确认；保存到手机与分享给用户分别判断。",
    }
