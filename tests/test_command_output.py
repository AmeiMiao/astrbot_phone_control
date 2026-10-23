"""真实 AstrBot 消息链中的命令图片开关与渲染故障提示。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

astrbot = pytest.importorskip("astrbot")
if getattr(astrbot, "_acat_local_stub", False):
    pytest.skip("需要真实 AstrBot 消息链", allow_module_level=True)

from test_main_fake_enabled import _make_plugin


@pytest.mark.parametrize("sub", ["status", "help"])
@pytest.mark.parametrize("as_image", [False, True])
def test_command_output_switch(tmp_path, monkeypatch, sub, as_image):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        plugin.raw_config["command"] = {f"{sub}_as_image": as_image}
        render = AsyncMock(return_value=str(tmp_path / "card.png"))
        monkeypatch.setattr(plugin, "html_render", render)
        event = SimpleNamespace(send=AsyncMock())
        try:
            await plugin._say_command(event, "手机帮助 <测试>\n下一行", sub)
            chain = event.send.call_args.args[0].chain
            assert type(chain[0]).__name__ == ("Image" if as_image else "Plain")
            assert render.await_count == int(as_image)
            if as_image:
                assert "{{ text | e }}" in render.call_args.args[0]
                assert "white-space: pre-wrap" in render.call_args.args[0]
                assert render.call_args.args[1]["text"] == "手机帮助 <测试>\n下一行"
                assert render.call_args.kwargs["return_url"] is False
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


def test_command_render_failure_is_visible(tmp_path, monkeypatch):
    async def scenario():
        plugin = _make_plugin(tmp_path, monkeypatch)
        monkeypatch.setattr(plugin, "html_render", AsyncMock(side_effect=RuntimeError("render offline")))
        event = SimpleNamespace(send=AsyncMock())
        try:
            await plugin._say_command(event, "手机帮助", "help")
            text = event.send.call_args.args[0].chain[0].text
            assert "图片渲染失败" in text and "手机帮助" in text
        finally:
            await plugin.terminate()
    asyncio.run(scenario())


def test_command_card_preserves_literal_parameters_and_escapes_html():
    from jinja2 import Template
    from core.command_card import COMMAND_CARD_TEMPLATE

    text = "/phone open <app>\n/phone task <instruction>\n<script>bad()</script>"
    rendered = Template(COMMAND_CARD_TEMPLATE).render(title="手机帮助", text=text)
    assert "/phone open &lt;app&gt;\n/phone task &lt;instruction&gt;" in rendered
    assert "&lt;script&gt;bad()&lt;/script&gt;" in rendered
    assert "<script>" not in rendered
    assert "white-space: pre-wrap" in rendered
