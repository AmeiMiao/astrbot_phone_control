"""存图开关、来源边界、手机校验与同图去重。"""

import asyncio
import hashlib
from types import SimpleNamespace

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
            config=PhoneConfig.from_mapping({"browse": {"auto_save_images": True}}),
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
            service, event, index=0, reason="这张表情贴合心情"
        )
        assert (
            result["saved_to"]
            == "/storage/emulated/0/Pictures/AstrBot/" + digest + ".gif"
        )
        await save_content_image(service, event, index=0, reason="喜欢")
        assert len(pushed) == 1
        assert any("MEDIA_SCANNER_SCAN_FILE" in cmd for cmd in commands)
        with pytest.raises(PhoneControlError, match="超出范围"):
            await save_content_image(service, event, index=1, reason="喜欢")
        service.config = PhoneConfig()
        with pytest.raises(PhoneControlError, match="已关闭"):
            await save_content_image(service, event, index=0, reason="喜欢")
        assert len(pushed) == 1
        await service.contents.close()

    asyncio.run(run())
