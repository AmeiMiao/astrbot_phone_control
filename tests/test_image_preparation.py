"""配图准备复用原图和本轮预约，不执行发布。"""

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.config import PhoneConfig
from core.errors import PhoneControlError
from core.interactions import InteractionStore, interaction_run
from core.saved_images import SavedImages


def setup_library(tmp_path, storage="phone"):
    data = b"GIF89aimage"
    digest = hashlib.sha256(data).hexdigest()
    destination = f"/storage/emulated/0/Pictures/AstrBot/{digest}.gif"
    commands, pushes = [], []

    async def shell(command):
        commands.append(command)
        if command.startswith("readlink"):
            return "/storage/emulated/0/Pictures/AstrBot"
        if "sha256sum" in command:
            return digest + "  " + destination
        if command.startswith("content query"):
            return destination
        return ""

    async def run(args):
        pushes.append(args)

    service = SimpleNamespace(
        data_dir=tmp_path,
        config=PhoneConfig.from_mapping(
            {
                "image": {"auto_save_images": True},
                "security": {
                    "allow_posts": True,
                    "allow_comments": True,
                    "allow_passive_replies": True,
                },
            }
        ),
        adb=SimpleNamespace(serial="test-device", shell=shell, run=run),
        guard_device=lambda _: None,
        interactions=InteractionStore(tmp_path),
    )
    extra = {}
    event = SimpleNamespace(
        unified_msg_origin="test-owner",
        get_extra=extra.get,
        set_extra=lambda k, v: extra.__setitem__(k, v),
    )
    library = SavedImages(service)
    path = library.root / "files" / (digest + ".gif")
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    record = dict(
        id=digest,
        sha256=digest,
        description="开心猫咪",
        tags=["开心"],
        server_path=str(path) if storage == "server" else "",
        phone_path=destination if storage == "phone" else "",
        phone_device="test-device",
    )
    library.write({digest: record})
    return library, service, event, digest, commands, pushes


@pytest.mark.parametrize("storage", ["server", "phone"])
@pytest.mark.parametrize("kind", ["post", "comment", "passive_reply"])
def test_prepare_preserves_reservation_and_uses_selected_storage(
    tmp_path, storage, kind
):
    async def run():
        library, service, event, digest, commands, pushes = setup_library(
            tmp_path, storage
        )
        service.interactions.reserve(
            kind=kind,
            app="test",
            session=event.unified_msg_origin,
            run=interaction_run(event),
        )
        result = await library.prepare_for_interaction(
            event, digest, kind=kind, reason="用开心猫咪回应好消息", storage=storage
        )
        assert result["prepared_only"] and result["source_storage"] == storage
        assert result["phone_path"].endswith(digest + ".gif")
        assert (
            service.interactions.get_reservation(event.unified_msg_origin, kind)
            is not None
        )
        assert any(c.startswith("content query") for c in commands)
        assert not pushes  # 已存在且哈希一致，无需重复推送。
        assert len(library.find("开心", storage=storage)) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure",
    [
        "off",
        "permission",
        "no_reservation",
        "other_run",
        "empty_reason",
        "device",
        "hash",
        "path",
    ],
)
def test_prepare_refuses_invalid_context_before_image_selection(tmp_path, failure):
    async def run():
        library, service, event, digest, commands, pushes = setup_library(tmp_path)
        service.interactions.reserve(
            kind="post",
            app="test",
            session=event.unified_msg_origin,
            run=interaction_run(event),
        )
        reason = "回应好消息"
        if failure == "off":
            service.config = replace(
                service.config,
                image=replace(service.config.image, auto_save_images=False),
            )
        elif failure == "permission":
            service.config = replace(
                service.config,
                security=replace(service.config.security, allow_posts=False),
            )
        elif failure == "no_reservation":
            service.interactions.release(session=event.unified_msg_origin, kind="post")
        elif failure == "other_run":
            interaction_run(event, restart=True)
        elif failure == "empty_reason":
            reason = " "
        elif failure == "device":
            service.adb.serial = "other-device"
        elif failure == "hash":

            async def shell(_):
                return "0" * 64 + "  file"

            service.adb.shell = shell
        elif failure == "path":
            records = library.load()
            records[digest]["phone_path"] = "/sdcard/private.gif"
            library.write(records)
        with pytest.raises(PhoneControlError):
            await library.prepare_for_interaction(
                event, digest, kind="post", reason=reason
            )
        assert not pushes
        assert not any(c.startswith("am broadcast") for c in commands)

    asyncio.run(run())
