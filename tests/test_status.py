"""设备状态完整输出与图片保存配置回归。"""

import asyncio

import pytest

from core.models import DeviceInfo
from core.service import PhoneService


class StatusAdb:
    async def ensure_ready(self):
        pass

    async def device_info(self):
        return DeviceInfo(serial="uat-device", model="测试手机", adb_connected=True)


@pytest.mark.parametrize("image, expected", [
    ({}, "图片保存：关闭；存储位置：AstrBot 服务端"),
    ({"auto_save_images": True, "image_storage_location": "phone"}, "图片保存：开启；存储位置：手机"),
    ({"save_images_both": True}, "图片保存：关闭；存储位置：两地"),
])
def test_connected_status_reads_image_settings(tmp_path, image, expected):
    service = PhoneService(
        raw_config={"adb": {"host": "device", "port": 5555}, "image": image},
        adb=StatusAdb(), actions=None, diary=None, data_dir=tmp_path,
    )
    text = asyncio.run(service.status_text())
    assert "连接：已连接" in text
    assert "测试手机" in text
    assert expected in text
    assert "App经验：" in text


def test_command_output_defaults_match_schema_and_allow_independent_switches():
    import json
    from pathlib import Path
    from core.config import PhoneConfig

    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text())
    assert list(schema).index("command") > list(schema).index("verification")
    for key in ("status_as_image", "help_as_image"):
        assert schema["command"]["items"][key]["default"] is True
    config = PhoneConfig.from_mapping({"command": {"status_as_image": False}})
    assert config.command.status_as_image is False
    assert config.command.help_as_image is True
