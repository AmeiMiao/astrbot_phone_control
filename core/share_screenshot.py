"""帖子截图：在同一页面向顶部滚动，确认边界稳定后捕获。"""

from __future__ import annotations

import asyncio
import time

from .errors import ActionFailedError
from .ui import OBSERVATION_OK, assess_observation, dump_ui, screen_signature


async def capture_post_top(service, *, max_swipes: int = 12):
    """两次向下滑动均未改变内容才确认顶部；页面切换或超限时阻止分享。"""
    actions = service.actions
    origin = await actions.foreground()
    if not origin[0] or origin[0] in {"android", "com.android.systemui", "com.android.intentresolver"}:
        raise ActionFailedError("请关闭分享面板并打开需要分享的帖子")
    deadline = time.monotonic() + 45
    previous = ""
    stable = 0
    for step in range(max_swipes + 1):
        service.guard_device("share_post_top")
        if await actions.foreground() != origin:
            raise ActionFailedError("回到帖子顶部时页面已切换，请重新确认分享内容")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        snapshot = await dump_ui(service.adb, timeout=min(5, remaining), max_retries=0)
        if snapshot.package != origin[0] or assess_observation(snapshot) != OBSERVATION_OK:
            raise ActionFailedError("帖子页面不可读，无法确认顶部；请先关闭弹窗或分享面板")
        signature = screen_signature(snapshot)
        stable = stable + 1 if signature == previous else 0
        if stable >= 2:
            if await actions.foreground() != origin:
                raise ActionFailedError("截图前页面已切换，请重新确认分享内容")
            data, path, meta = await service.capture(expected_package=origin[0])
            if meta.get("mismatch") or await actions.foreground() != origin:
                raise ActionFailedError("截图前台与帖子不一致，未分享该图片")
            return data, path, {**meta, "position": "post_top", "swipes": step}
        scrollables = [
            n for n in snapshot.nodes if n.scrollable and n.enabled
            and n.bounds.height > 100 and n.bounds.width > 50
            and n.package == origin[0]
        ]
        if not scrollables:
            raise ActionFailedError("无法识别帖子滚动区域；请手动确认顶部后用 content_type=screen 分享")
        if step >= max_swipes:
            break
        area = max(scrollables, key=lambda n: n.bounds.height * n.bounds.width).bounds
        previous = signature
        await actions.swipe(
            area.center_x, area.top + int(area.height * .25),
            area.center_x, area.top + int(area.height * .80), duration_ms=350,
        )
        await asyncio.sleep(.35)
    raise ActionFailedError("在限定滑动次数或时间内未确认帖子顶部，未分享中途截图")
