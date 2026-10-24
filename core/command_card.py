"""命令图片的纯文字模板，正文按原始字符与换行显示。"""

COMMAND_CARD_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
html, body { margin: 0; padding: 0; width: 800px; background: #f4f6fb; }
main { padding: 28px; color: #20304a; font-family: Arial, "Noto Sans CJK SC", sans-serif; }
h1 { margin: 0 0 18px; font-size: 28px; }
pre { margin: 0; padding: 24px; border-radius: 16px; background: white;
      white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word;
      font: 22px/1.7 Arial, "Noto Sans CJK SC", sans-serif; }
</style></head><body><main><h1>{{ title | e }}</h1><pre>{{ text | e }}</pre></main></body></html>"""


MAX_COMMAND_IMAGE_BYTES = 8 * 1024 * 1024


def validate_command_image(data: bytes) -> None:
    """渲染结果须能完整解码，错误文本和损坏文件不得进入图片消息链。"""
    from io import BytesIO
    from PIL import Image, UnidentifiedImageError

    if not data or len(data) > MAX_COMMAND_IMAGE_BYTES:
        raise ValueError("文字转图结果为空或超过 8 MiB")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("文字转图结果格式须为 PNG、JPEG 或 WebP")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("文字转图服务未返回完整可解码的图片") from exc


async def download_command_image(url: str) -> str:
    """下载官方渲染接口返回的图片资源，校验响应后交给框架管理临时文件。"""
    from urllib.parse import urlsplit
    import aiohttp
    from astrbot.core.utils.http_ssl import build_tls_connector
    from astrbot.core.utils.io import save_temp_img

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("文字转图服务未返回有效的图片资源地址")
    async with aiohttp.ClientSession(
        trust_env=True,
        connector=build_tls_connector(),
        timeout=aiohttp.ClientTimeout(total=30),
    ) as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise ValueError(f"文字转图图片下载失败：HTTP {response.status}")
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > MAX_COMMAND_IMAGE_BYTES:
                    raise ValueError("文字转图结果超过 8 MiB")
    image_bytes = bytes(data)
    validate_command_image(image_bytes)
    return save_temp_img(image_bytes)
