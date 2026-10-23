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
