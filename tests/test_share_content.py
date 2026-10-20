"""core.share_content 单元测试：链接校验、抓包取链接与分享决策矩阵。"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from core.share_content import (
    DEFAULT_SHARE_LINK_LIMIT,
    SHARE_LINK_WINDOW_SECONDS,
    TRAFFIC_SHARE_SOURCE,
    SharePlan,
    is_probably_share_page,
    join_share_text,
    pick_share_link,
    plan_share,
    validate_http_link,
)

# 固定时间基准，避免依赖真实时钟。
NOW = 1_760_000_000.0


def _flow(
    url: str,
    *,
    body: str = "",
    ts: float | None = NOW - 10,
    status: int = 200,
) -> dict:
    """构造 ``TrafficCapture.recent()`` 形态的原始 flow。"""
    record: dict = {
        "method": "GET",
        "url": url,
        "status": status,
        "ctype": "application/json",
        "reqbody": "",
        "body": body,
    }
    if ts is not None:
        record["ts"] = ts
    return record


def _json_flow(url: str, payload: dict, *, ts: float = NOW - 10) -> dict:
    return _flow(url, body=json.dumps(payload, ensure_ascii=False), ts=ts)


# ----------------------------------------------------------------------
# 常量与链接校验
# ----------------------------------------------------------------------


def test_constants():
    assert SHARE_LINK_WINDOW_SECONDS == 180
    assert DEFAULT_SHARE_LINK_LIMIT == 5
    assert TRAFFIC_SHARE_SOURCE == "抓包（页面推断）"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.bilibili.com/video/BV1xx", "https://www.bilibili.com/video/BV1xx"),
        (" http://example.com/share?a=1 ", "http://example.com/share?a=1"),
        ("HTTPS://example.com/x", "HTTPS://example.com/x"),
        ("http://127.0.0.1:8080/a", "http://127.0.0.1:8080/a"),
    ],
)
def test_validate_http_link_accepts_http_and_strips(raw, expected):
    assert validate_http_link(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        None,
        "example.com/share",
        "ftp://example.com/x",
        "http://",
        "https:///path",
        "//example.com/x",
        "javascript:alert(1)",
        123,
    ],
)
def test_validate_http_link_rejects_invalid(raw):
    assert validate_http_link(raw) == ""


# ----------------------------------------------------------------------
# 抓包链接提取
# ----------------------------------------------------------------------


def test_pick_share_link_extracts_share_and_jump_url():
    flow = _json_flow(
        "https://api.bilibili.com/x/share",
        {"data": {"share_url": "https://www.bilibili.com/video/BV1xx", "jump_url": "https://b23.tv/abc"}},
    )
    link, source = pick_share_link([flow], now=NOW)
    assert link == "https://www.bilibili.com/video/BV1xx"
    assert source == "抓包（页面推断）"


def test_pick_share_link_orders_by_ts_desc():
    older = _json_flow(
        "https://api.bilibili.com/x/share",
        {"data": {"share_url": "https://www.bilibili.com/video/OLD"}},
        ts=NOW - 100,
    )
    newer = _json_flow(
        "https://aweme.snssdk.com/aweme/v1/share",
        {"data": {"share_url": "https://www.douyin.com/video/NEW"}},
        ts=NOW - 5,
    )
    link, _source = pick_share_link([older, newer], now=NOW)
    assert link == "https://www.douyin.com/video/NEW"


def test_pick_share_link_filters_expired_window():
    expired = _json_flow(
        "https://api.bilibili.com/x/share",
        {"data": {"share_url": "https://www.bilibili.com/video/OLD"}},
        ts=NOW - SHARE_LINK_WINDOW_SECONDS - 1,
    )
    fresh = _json_flow(
        "https://aweme.snssdk.com/aweme/v1/share",
        {"data": {"share_url": "https://www.douyin.com/video/NEW"}},
        ts=NOW - 5,
    )
    link, _source = pick_share_link([expired, fresh], now=NOW)
    assert link == "https://www.douyin.com/video/NEW"
    assert pick_share_link([expired], now=NOW) == ("", "")


def test_pick_share_link_window_seconds_override():
    flow = _json_flow(
        "https://api.bilibili.com/x/share",
        {"share_url": "https://www.bilibili.com/video/BV1"},
        ts=NOW - 30,
    )
    assert pick_share_link([flow], now=NOW, window_seconds=60)[0] == "https://www.bilibili.com/video/BV1"
    assert pick_share_link([flow], now=NOW, window_seconds=10) == ("", "")


def test_pick_share_link_prefers_app_hint_by_page_inference():
    bilibili = _json_flow(
        "https://api.bilibili.com/x/share/click?aid=1",
        {"data": {"share_url": "https://www.bilibili.com/video/BV_HINT"}},
        ts=NOW - 120,
    )
    douyin = _json_flow(
        "https://aweme.snssdk.com/aweme/v1/share/?x=1",
        {"data": {"share_url": "https://www.douyin.com/video/FRESH"}},
        ts=NOW - 5,
    )
    link, _source = pick_share_link([douyin, bilibili], now=NOW, app_hint="B站")
    assert link == "https://www.bilibili.com/video/BV_HINT"


def test_pick_share_link_matches_chinese_label_alias():
    """App 中文名与抓包 host 中英文错位时按别名匹配（哔哩哔哩→bilibili）。"""
    bilibili = _json_flow(
        "https://api.bilibili.com/x/share/click",
        {"data": {"share_url": "https://www.bilibili.com/video/BV_ALIAS"}},
        ts=NOW - 30,
    )
    douyin = _json_flow(
        "https://aweme.snssdk.com/aweme/v1/share/",
        {"data": {"share_url": "https://www.douyin.com/video/X"}},
        ts=NOW - 5,
    )
    link, _source = pick_share_link([douyin, bilibili], now=NOW, app_hint="哔哩哔哩")
    assert link == "https://www.bilibili.com/video/BV_ALIAS"


def test_pick_share_link_prefers_app_hint_by_host():
    target = _json_flow(
        "https://share.api.example.com/link",
        {"data": {"share_url": "https://example.com/page/1"}},
        ts=NOW - 150,
    )
    other = _json_flow(
        "https://aweme.snssdk.com/aweme/v1/share/",
        {"data": {"share_url": "https://www.douyin.com/video/2"}},
        ts=NOW - 5,
    )
    link, _source = pick_share_link([other, target], now=NOW, app_hint="example")
    assert link == "https://example.com/page/1"


def test_pick_share_link_app_hint_never_falls_back_to_other_apps():
    """app_hint 未命中时宁可不返回链接（改用截图），也不附其他 App 的旧链接。"""
    older = _json_flow(
        "https://api.bilibili.com/x/share/click",
        {"data": {"share_url": "https://www.bilibili.com/video/OLD"}},
        ts=NOW - 100,
    )
    newer = _json_flow(
        "https://aweme.snssdk.com/aweme/v1/share/",
        {"data": {"share_url": "https://www.douyin.com/video/NEW"}},
        ts=NOW - 5,
    )
    assert pick_share_link([older, newer], now=NOW, app_hint="zhihu") == ("", "")


def test_pick_share_link_ignores_non_share_endpoints():
    """信息流/详情响应里也常带 share_url，但它不对应当前内容，不用于自动分享。"""
    feed = _json_flow(
        "https://api.bilibili.com/x/feed/index",
        {"data": {"share_url": "https://www.bilibili.com/video/WRONG"}},
        ts=NOW - 5,
    )
    share = _json_flow(
        "https://api.bilibili.com/x/share/click",
        {"data": {"share_url": "https://www.bilibili.com/video/RIGHT"}},
        ts=NOW - 30,
    )
    link, _source = pick_share_link([feed, share], now=NOW)
    assert link == "https://www.bilibili.com/video/RIGHT"


@pytest.mark.parametrize(
    "static_url",
    [
        "https://example.com/cover.png",
        "https://example.com/video.mp4?x=1",
        "https://example.com/app.js",
        "https://example.com/data.json",
    ],
)
def test_pick_share_link_skips_static_resources(static_url):
    flow = _json_flow("https://api.bilibili.com/x/share", {"share_url": static_url})
    assert pick_share_link([flow], now=NOW) == ("", "")


def test_pick_share_link_empty_and_malformed_flows():
    assert pick_share_link([], now=NOW) == ("", "")
    assert pick_share_link([None, 42, {"url": "https://example.com/x"}], now=NOW) == ("", "")
    bad_ts = dict(_flow("https://api.bilibili.com/x/share"), ts="bad")
    assert pick_share_link([bad_ts], now=NOW) == ("", "")


def test_pick_share_link_accepts_non_json_body_as_empty():
    flow = _flow("https://api.bilibili.com/x/share", body="<html>no share</html>")
    assert pick_share_link([flow], now=NOW) == ("", "")


def test_pick_share_link_deduplicates_across_flows():
    same = {"share_url": "https://www.bilibili.com/video/SAME"}
    flows = [
        _json_flow("https://api.bilibili.com/x/share", same, ts=NOW - 100),
        _json_flow("https://api.bilibili.com/x/share", same, ts=NOW - 5),
    ]
    link, source = pick_share_link(flows, now=NOW)
    assert (link, source) == ("https://www.bilibili.com/video/SAME", TRAFFIC_SHARE_SOURCE)


# ----------------------------------------------------------------------
# 分享决策矩阵
# ----------------------------------------------------------------------


def test_plan_share_explicit_link():
    plan = plan_share(text="看看这个", link=" https://www.bilibili.com/video/BV1 ")
    assert isinstance(plan, SharePlan)
    assert plan.text == "看看这个"
    assert plan.link == "https://www.bilibili.com/video/BV1"
    assert plan.link_source == "manual"
    assert plan.screenshot_required is False
    assert plan.use_existing_screenshot is False
    assert plan.notes == ()
    assert plan.error == ""


def test_plan_share_traffic_link():
    plan = plan_share(
        text="看看这个",
        traffic_link="https://b23.tv/abc",
        traffic_source=TRAFFIC_SHARE_SOURCE,
    )
    assert plan.link == "https://b23.tv/abc"
    assert plan.link_source == "traffic"
    assert plan.screenshot_required is False
    assert plan.use_existing_screenshot is False
    assert any("抓包" in note for note in plan.notes)
    assert plan.error == ""


def test_plan_share_without_link_requires_screenshot():
    plan = plan_share(text="只有文字")
    assert plan.link == ""
    assert plan.link_source == ""
    assert plan.screenshot_required is True
    assert plan.use_existing_screenshot is False
    assert plan.notes == ("未获取到链接，已自动附截图兜底",)
    assert plan.error == ""


def test_plan_share_uses_existing_screenshot_when_no_link():
    plan = plan_share(text="只有文字", screenshot_path="/data/shot.png")
    assert plan.link == ""
    assert plan.screenshot_required is True
    assert plan.use_existing_screenshot is True
    assert plan.notes == ("未获取到链接，使用已有截图",)


def test_plan_share_link_plus_screenshot():
    plan = plan_share(
        text="图文一起",
        link="https://example.com/a",
        include_screenshot=True,
    )
    assert plan.link_source == "manual"
    assert plan.screenshot_required is True
    assert plan.use_existing_screenshot is False
    assert any("截图" in note for note in plan.notes)
    assert plan.error == ""


def test_plan_share_link_plus_existing_screenshot():
    plan = plan_share(
        text="图文一起",
        link="https://example.com/a",
        include_screenshot=True,
        screenshot_path="/data/shot.png",
    )
    assert plan.screenshot_required is True
    assert plan.use_existing_screenshot is True


def test_plan_share_invalid_link_falls_back_to_traffic():
    plan = plan_share(
        text="正文",
        link="not-a-url",
        traffic_link="https://b23.tv/ok",
        traffic_source=TRAFFIC_SHARE_SOURCE,
    )
    assert plan.link == "https://b23.tv/ok"
    assert plan.link_source == "traffic"
    assert any("显式链接" in note for note in plan.notes)
    assert plan.screenshot_required is False


def test_plan_share_invalid_link_without_traffic_falls_back_to_screenshot():
    plan = plan_share(text="正文", link="ftp://example.com/x")
    assert plan.link == ""
    assert plan.link_source == ""
    assert plan.screenshot_required is True
    assert any("显式链接" in note for note in plan.notes)
    assert any("自动附截图" in note for note in plan.notes)


def test_plan_share_invalid_traffic_link_falls_back_to_screenshot():
    plan = plan_share(text="正文", traffic_link="ftp://example.com/x")
    assert plan.link == ""
    assert plan.screenshot_required is True
    assert any("抓包链接无效" in note for note in plan.notes)


def test_plan_share_never_text_only_even_when_text_and_screenshot_path_empty():
    plan = plan_share(text="", link="", traffic_link="", screenshot_path="")
    assert plan.screenshot_required is True
    assert plan.use_existing_screenshot is False
    assert plan.error == ""
    assert plan.notes == ("未获取到链接，已自动附截图兜底",)


def test_plan_share_keeps_raw_text_and_does_not_join_link():
    plan = plan_share(text=" 原始正文 \n", link="https://example.com/x")
    assert plan.text == " 原始正文 \n"
    assert "链接" not in plan.text


def test_share_plan_is_frozen():
    plan = plan_share(text="x")
    with pytest.raises(FrozenInstanceError):
        plan.link = "https://example.com/x"  # type: ignore[misc]


# ----------------------------------------------------------------------
# 文本拼接与分享页判断
# ----------------------------------------------------------------------


def test_join_share_text_with_both():
    assert join_share_text("正文", "https://example.com/x") == "正文\n链接：https://example.com/x"


def test_join_share_text_without_text():
    assert join_share_text("", "https://example.com/x") == "链接：https://example.com/x"


def test_join_share_text_without_link():
    assert join_share_text("正文", "") == "正文"
    assert join_share_text("", "") == ""


def test_join_share_text_strips_link_whitespace():
    assert join_share_text("正文", " https://example.com/x ") == "正文\n链接：https://example.com/x"


def test_is_probably_share_page_by_url():
    assert is_probably_share_page(_flow("https://api.example.com/x/share?target=1")) is True
    assert is_probably_share_page(_flow("https://api.example.com/x/feed")) is False


def test_is_probably_share_page_ignores_response_body():
    """只按 URL 判断分享接口；详情/信息流响应体里的 jump_url 不算分享页。"""
    flow = _json_flow("https://api.example.com/x/detail", {"data": {"jump_url": "https://example.com/p/1"}})
    assert is_probably_share_page(flow) is False
    assert is_probably_share_page(_json_flow("https://api.example.com/x/detail", {"code": 0})) is False
    assert is_probably_share_page(_flow("https://api.example.com/x/share?target=1")) is True


def test_is_probably_share_page_handles_non_dict():
    assert is_probably_share_page(None) is False  # type: ignore[arg-type]
