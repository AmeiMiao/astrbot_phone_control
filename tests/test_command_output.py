"""真实 AstrBot 消息链中的命令图片开关与渲染故障提示。"""

import asyncio
import sys
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
        render = AsyncMock(return_value="https://render.example/card")
        download = AsyncMock(return_value=str(tmp_path / "card.png"))
        monkeypatch.setattr(sys.modules[plugin._say_command.__module__], "download_command_image", download)
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
                assert render.call_args.kwargs["return_url"] is True
                download.assert_awaited_once_with("https://render.example/card")
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


def test_render_http_failure_uses_next_endpoint_and_delivers_valid_image(tmp_path, monkeypatch):
    """真实框架渲染策略：503错误页不能作为图片，继续下一个生成端点。"""
    import base64
    from io import BytesIO
    from aiohttp import web
    from PIL import Image
    from astrbot.core.utils.t2i.network_strategy import NetworkRenderStrategy
    from astrbot.core.utils import io

    async def scenario():
        image=BytesIO();Image.new('RGB',(4,4),'white').save(image,format='PNG');expected=image.getvalue()
        calls=[]
        async def bad(request):
            calls.append('bad');return web.Response(status=503,text='no available server\n')
        async def good(request):
            body=await request.json();assert body['json'] is True
            calls.append('good');return web.json_response({'data':{'id':'card'}})
        async def card(request):
            return web.Response(body=expected,content_type='image/png')
        app=web.Application(client_max_size=4*1024*1024);app.router.add_post('/bad/generate',bad);app.router.add_post('/good/generate',good);app.router.add_get('/good/card',card)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        strategy=NetworkRenderStrategy();strategy.endpoints=[f'http://127.0.0.1:{port}/bad',f'http://127.0.0.1:{port}/good']
        import astrbot.core.utils.t2i.network_strategy as network
        monkeypatch.setattr(network.random,'shuffle',lambda endpoints:None)
        monkeypatch.setattr(io,'get_astrbot_temp_path',lambda:str(tmp_path))
        plugin=_make_plugin(tmp_path,monkeypatch);plugin.raw_config['command']={'status_as_image':True}
        monkeypatch.setattr(plugin,'html_render',strategy.render_custom_template)
        event=SimpleNamespace(send=AsyncMock())
        try:
            await plugin._say_command(event,'手机设备状态','status')
            assert calls==['bad','good']
            chain=event.send.call_args.args[0].chain
            assert type(chain[0]).__name__=='Image'
            assert base64.b64decode(await chain[0].convert_to_base64())==expected
        finally:
            await plugin.terminate();await runner.cleanup()
    asyncio.run(scenario())


@pytest.mark.parametrize('status,body',[(200,b'no available server\n'),(503,b'no available server\n'),(200,b'\x89PNG\r\n\x1a\nbroken')])
def test_invalid_download_never_enters_image_message(tmp_path,monkeypatch,status,body):
    from aiohttp import web
    async def scenario():
        async def card(request):return web.Response(status=status,body=body)
        app=web.Application();app.router.add_get('/card',card)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start();port=site._server.sockets[0].getsockname()[1]
        plugin=_make_plugin(tmp_path,monkeypatch);plugin.raw_config['command']={'status_as_image':True}
        monkeypatch.setattr(plugin,'html_render',AsyncMock(return_value=f'http://127.0.0.1:{port}/card'))
        event=SimpleNamespace(send=AsyncMock())
        try:
            await plugin._say_command(event,'手机设备状态','status')
            event.send.assert_awaited_once()
            chain=event.send.call_args.args[0].chain
            assert type(chain[0]).__name__=='Plain'
            assert '图片渲染失败' in chain[0].text and '手机设备状态' in chain[0].text
        finally:
            await plugin.terminate();await runner.cleanup()
    asyncio.run(scenario())
