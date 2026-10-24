"""文字定位选择具体目标与所有触控后端的边界守卫。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.models import Rect, UiNode
from core.service import PhoneService
from core.errors import PhoneControlError


def test_exact_visible_button_precedes_page_title_or_offscreen_duplicate(tmp_path):
    async def run():
        service = PhoneService(raw_config={}, adb=None, actions=SimpleNamespace(screen_size=AsyncMock(return_value=(720, 1280))), diary=None, data_dir=tmp_path)
        page = UiNode(ref=1, text="UAT EDGE TEST", bounds=Rect(0, 0, 720, 1280))
        hidden = UiNode(ref=2, text="EDGE", bounds=Rect(-100, 100, -10, 200), clickable=True)
        button = UiNode(ref=3, text="EDGE", bounds=Rect(-200, 100, 60, 200), clickable=True)
        assert await service._visible_node([page, hidden, button], text="EDGE") is button
        assert await service.node_click_point(button) == (30, 150)
        with pytest.raises(PhoneControlError, match="屏幕外"):
            await service.node_click_point(hidden)
    asyncio.run(run())


def test_invalid_coordinate_never_reaches_control_backend(tmp_path):
    async def run():
        service = PhoneService(raw_config={}, adb=None, actions=SimpleNamespace(screen_size=AsyncMock(return_value=(720, 1280))), diary=None, data_dir=tmp_path)
        service._control_backend_or_none = AsyncMock()
        with pytest.raises(PhoneControlError, match="坐标越界"):
            await service.tap(721, 100)
        service._control_backend_or_none.assert_not_awaited()
    asyncio.run(run())
