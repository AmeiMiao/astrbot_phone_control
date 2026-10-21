"""存图开关、来源边界、手机校验与同图去重。"""

import asyncio
import hashlib
from types import SimpleNamespace
from pathlib import Path

import pytest
from core.config import PhoneConfig
from core.content_task import ContentTasks
from core.errors import PhoneControlError
from core.saved_images import save_content_image


def test_save_original_to_phone_checks_hash_and_deduplicates(tmp_path):
    async def run():
        commands, pushed = [], []
        remote = {}
        data = b"GIF89a" + b"synthetic-emoji"
        digest = hashlib.sha256(data).hexdigest()

        async def shell(cmd):
            commands.append(cmd)
            if cmd.startswith("readlink"):
                return "/storage/emulated/0/Pictures/AstrBot"
            if cmd.startswith("content query"):
                return "/storage/emulated/0/Pictures/AstrBot/" + digest + ".gif"
            if "sha256sum" in cmd:
                return digest + "  saved.gif" if remote else ""
            return ""

        async def adb_run(args):
            pushed.append(args)
            remote["saved"] = True
            return ""

        service = SimpleNamespace(
            config=PhoneConfig.from_mapping(
                {
                    "browse": {
                        "auto_save_images": True,
                        "image_storage_location": "phone",
                    }
                }
            ),
            data_dir=tmp_path,
            guard_device=lambda _: None,
            traffic=None,
            actions=SimpleNamespace(
                foreground=lambda: asyncio.sleep(0, result=("test.app", "Post"))
            ),
            adb=SimpleNamespace(shell=shell, run=adb_run),
        )
        service.contents = ContentTasks(service)
        event = SimpleNamespace(unified_msg_origin="owner")
        task = await service.contents.begin(event, kind="post", content_id="post")
        task["summaries"] = ["已理解表情包"]
        path = service.contents.root / task["id"] / "image.gif"
        path.write_bytes(data)
        service.contents.post_material = lambda _: ("正文", [path])
        result = await save_content_image(
            service,
            event,
            index=0,
            reason="这张表情贴合心情",
            description="GIF 合成表情",
        )
        assert (
            result["saved_to"]
            == "/storage/emulated/0/Pictures/AstrBot/" + digest + ".gif"
        )
        await save_content_image(
            service, event, index=0, reason="喜欢", description="GIF 合成表情"
        )
        assert len(pushed) == 1
        assert any("MEDIA_SCANNER_SCAN_FILE" in cmd for cmd in commands)
        with pytest.raises(PhoneControlError, match="超出范围"):
            await save_content_image(
                service, event, index=1, reason="喜欢", description="GIF 合成表情"
            )
        service.config = PhoneConfig()
        with pytest.raises(PhoneControlError, match="已关闭"):
            await save_content_image(
                service, event, index=0, reason="喜欢", description="GIF 合成表情"
            )
        assert len(pushed) == 1
        await service.contents.close()

    asyncio.run(run())


def test_server_library_survives_task_cleanup_and_can_transfer_later(tmp_path):
    from core.saved_images import image_library

    async def run():
        data = b"GIF89a" + b"synthetic-sticker"
        digest = hashlib.sha256(data).hexdigest()
        commands = []

        async def shell(cmd):
            commands.append(cmd)
            if cmd.startswith("readlink"):
                return "/storage/emulated/0/Pictures/AstrBot"
            if "sha256sum" in cmd:
                return digest + "  saved.gif"
            if cmd.startswith("content query"):
                return "/storage/emulated/0/Pictures/AstrBot/" + digest + ".gif"
            return ""

        async def adb_run(args):
            commands.append(args)
            return ""

        s = SimpleNamespace(
            config=PhoneConfig.from_mapping({"browse": {"auto_save_images": True}}),
            data_dir=tmp_path,
            guard_device=lambda _: None,
            traffic=None,
            actions=SimpleNamespace(
                foreground=lambda: asyncio.sleep(0, result=("test.app", "Post"))
            ),
            adb=SimpleNamespace(shell=shell, run=adb_run),
        )
        s.contents = ContentTasks(s)
        e = SimpleNamespace(unified_msg_origin="owner")
        t = await s.contents.begin(e, kind="post", content_id="post-12")
        t["summaries"] = ["内容已理解"]
        path = s.contents.root / t["id"] / "sticker.gif"
        path.write_bytes(data)
        s.contents.post_material = lambda _: ("正文", [path])
        record = await save_content_image(
            s,
            e,
            index=0,
            description="开心眨眼表情包",
            reason="适合回复朋友",
            kind="sticker",
            tags=["开心", "眨眼"],
        )
        assert not commands, "服务端保存不应访问手机存储"
        target = Path(record["server_path"])
        assert target.read_bytes() == data
        await s.contents.finish_owner("owner", abnormal=True)
        assert target.is_file() and not path.exists()
        del s._image_library
        found = image_library(s).find("眨眼")
        assert len(found) == 1 and found[0]["sources"][0]["content_id"] == "post-12"
        assert found[0]["kind"] == "sticker" and found[0]["tags"] == ["开心", "眨眼"]
        sent = await image_library(s).send_to_phone(record["id"])
        assert sent["phone_path"].endswith(digest + ".gif")
        assert (
            image_library(s).find(record["id"])[0]["phone_path"] == sent["phone_path"]
        )

    asyncio.run(run())


def test_both_locations_and_catalog_preserve_partial_failure(tmp_path):
    from core.saved_images import image_library

    async def run():
        s = SimpleNamespace(
            config=PhoneConfig.from_mapping(
                {
                    "browse": {
                        "auto_save_images": True,
                        "image_storage_location": "phone",
                        "save_images_both": True,
                    }
                }
            ),
            data_dir=tmp_path,
            adb=SimpleNamespace(serial="test-device"),
            guard_device=lambda _: None,
            traffic=None,
            actions=SimpleNamespace(
                foreground=lambda: asyncio.sleep(0, result=("test.app", "Post"))
            ),
        )
        s.contents = ContentTasks(s)
        e = SimpleNamespace(unified_msg_origin="owner")
        t = await s.contents.begin(e, kind="post", content_id="post-13")
        t["summaries"] = ["已理解"]
        path = s.contents.root / t["id"] / "one.gif"
        path.write_bytes(b"GIF89aimage")
        s.contents.post_material = lambda _: ("正文", [path])
        lib = image_library(s)

        async def unavailable(*a):
            raise PhoneControlError("手机断开")

        lib.to_phone = unavailable
        with pytest.raises(PhoneControlError, match="手机断开"):
            await save_content_image(
                s, e, index=0, description="微笑表情包", reason="喜欢"
            )
        row = lib.find("微笑")[0]
        assert Path(row["server_path"]).is_file() and row["last_error"] == "手机断开"

        async def available(*a):
            return "/storage/emulated/0/Pictures/AstrBot/one.gif"

        lib.to_phone = available
        result = await save_content_image(
            s, e, index=0, description="微笑表情包", reason="喜欢"
        )
        assert result["storage_location"] == "both"
        assert result["phone_path"] and result["server_path"]
        assert result["saved_to"] == result["server_path"] and not result["last_error"]
        await s.contents.close()

    asyncio.run(run())


def test_storage_schema_hides_primary_location_when_both_enabled():
    import json
    from pathlib import Path

    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())[
        "browse"
    ]["items"]
    assert schema["image_storage_location"]["condition"] == {
        "auto_save_images": True,
        "save_images_both": False,
    }
    assert schema["image_storage_location"]["default"] == "server"
