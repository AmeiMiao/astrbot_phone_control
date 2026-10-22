"""信息流浏览默认 App 留空时的随机选择（服务层）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.errors import AppNotInstalledError
from core.service import PhoneService


class _FakeAdb:
    def __init__(self, packages: list[str]) -> None:
        self.packages = list(packages)

    async def shell(self, command: str, timeout: float = 0):
        assert command == "pm list packages -3"
        return "\n".join(f"package:{name}" for name in self.packages)


def _service(tmp_path, packages: list[str]) -> PhoneService:
    return PhoneService(
        raw_config={},
        adb=_FakeAdb(packages),
        actions=None,
        diary=None,  # type: ignore[arg-type]
        data_dir=tmp_path,
    )


def test_random_browse_app_returns_installed_package_and_label(tmp_path):
    service = _service(tmp_path, ["com.ss.android.ugc.aweme", "tv.danmaku.bili"])
    package, label = asyncio.run(service.random_browse_app())
    assert package in {"com.ss.android.ugc.aweme", "tv.danmaku.bili"}
    assert label
    # 已安装列表被缓存，重复调用不再访问设备
    package2, _label2 = asyncio.run(service.random_browse_app())
    assert package2 in {"com.ss.android.ugc.aweme", "tv.danmaku.bili"}


def test_random_browse_app_raises_when_no_apps(tmp_path):
    service = _service(tmp_path, [])
    with pytest.raises(AppNotInstalledError):
        asyncio.run(service.random_browse_app())
