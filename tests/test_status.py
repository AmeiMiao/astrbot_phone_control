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
