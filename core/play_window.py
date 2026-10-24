"""自主活动的每日多时段校验与执行判定。"""

from datetime import datetime
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WINDOW_ORIGINS = {"phone_random_play": "random_windows", "phone_continuous": "continuous_windows"}


def validate_timezone(value):
    """时区必须是可解析的 IANA 名称。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("auto.play_timezone 必须填写 IANA 时区，例如 Asia/Shanghai")
    try:
        ZoneInfo(value.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"auto.play_timezone 无效：{value}") from exc
    return value.strip()


def parse_windows(value, field="活动时段"):
    """列表每项 HH:MM-HH:MM，开始包含、结束排除；空列表表示全天。"""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} 必须是时间段列表，例如 ['08:00-10:00', '19:00-22:00']")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field} 每项必须是 HH:MM-HH:MM")
        text = item.strip()
        match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)", text)
        if not match:
            raise ValueError(f"{field} 时间段无效：{text}；请填写 HH:MM-HH:MM")
        h1, m1, h2, m2 = map(int, match.groups())
        if (h1, m1) == (h2, m2):
            raise ValueError(f"{field} 开始与结束不能相同；全天请清空列表")
        if text not in result:
            result.append(text)
    return tuple(result)


def in_windows(windows, tz_name="Asia/Shanghai", now=None):
    """按指定时区判定当日多段或跨午夜时段。"""
    if not windows:
        return True
    current = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo(tz_name))
    minute = current.hour * 60 + current.minute
    for text in windows:
        start, end = text.split("-")
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        low, high = sh * 60 + sm, eh * 60 + em
        if (low <= minute < high) if low < high else (minute >= low or minute < high):
            return True
    return False


def activity_allowed(auto, origin, now=None):
    """用户任务独立执行，自主随机与持续轮次受各自时间段约束。"""
    field = WINDOW_ORIGINS.get(origin)
    return field is None or in_windows(getattr(auto, field), auto.play_timezone, now)
