"""core.page_content 单元测试：前台 App 匹配、动作窗口、新鲜度、分组去重与渲染。"""

from __future__ import annotations

import time

from core.page_content import (
    PAGE_FLOW_MAX_AGE_SECONDS,
    build_page_traffic,
    flow_app,
    flow_matches_foreground,
    foreground_app,
    page_traffic_footer,
    render_page_flows,
    select_page_flows,
)

BILI_PACKAGE = "tv.danmaku.bili"
BILI_FEED = "https://api.bilibili.com/x/feed/index?page=1"
BILI_VIEW = "https://api.bilibili.com/x/web-interface/view?bvid=BV1xx"
DOUYIN_FEED = "https://api.douyin.com/aweme/v1/feed/?count=6"
XHS_FEED = "https://api.xiaohongshu.com/api/sns/v1/feed"
UNKNOWN_HOST = "https://api.example.com/data"


def _flow(
    url: str,
    ts: float,
    *,
    body: str = "",
    reqbody: str = "",
    status: int = 200,
    ctype: str = "application/json",
    method: str = "GET",
) -> dict:
    return {
        "ts": ts,
        "method": method,
        "url": url,
        "status": status,
        "ctype": ctype,
        "reqbody": reqbody,
        "body": body,
    }


def test_flow_app_maps_known_hosts_to_package():
    assert flow_app(_flow(BILI_FEED, 0)) == ("B站", BILI_PACKAGE)
    assert flow_app(_flow(DOUYIN_FEED, 0))[1] == "com.ss.android.ugc.aweme"
    assert flow_app(_flow(XHS_FEED, 0))[1] == "com.xingin.xhs"
    assert flow_app(_flow(UNKNOWN_HOST, 0)) == ("", "")


def test_flow_matches_foreground_three_states():
    assert flow_matches_foreground(_flow(BILI_FEED, 0), BILI_PACKAGE) is True
    assert flow_matches_foreground(_flow(DOUYIN_FEED, 0), BILI_PACKAGE) is False
    # 未收录域名无法归属 → None；无前台包名同样 None。
    assert flow_matches_foreground(_flow(UNKNOWN_HOST, 0), BILI_PACKAGE) is None
    assert flow_matches_foreground(_flow(BILI_FEED, 0), "") is None


def test_foreground_app_maps_package_label():
    assert foreground_app(BILI_PACKAGE) == (BILI_PACKAGE, "哔哩哔哩")
    assert foreground_app("com.example.custom")[1] == "com.example.custom"
    assert foreground_app("") == ("", "")


def test_select_keeps_only_foreground_app_flows_in_time_order():
    now = time.time()
    flows = [
        _flow(BILI_FEED, now - 10, body="feed"),
        _flow(DOUYIN_FEED, now - 6, body="douyin"),
        _flow(BILI_VIEW, now - 3, body="view"),
    ]
    selection = select_page_flows(flows, foreground_package=BILI_PACKAGE, now=now)
    assert selection.matched is True
    assert selection.unknown_foreground is False
    assert [flow["url"] for flow in selection.flows] == [BILI_FEED, BILI_VIEW]
    assert selection.newest_age_seconds == 3.0


def test_select_reports_other_app_traffic():
    now = time.time()
    selection = select_page_flows(
        [_flow(DOUYIN_FEED, now - 5)],
        foreground_package=BILI_PACKAGE,
        now=now,
    )
    assert selection.flows == ()
    assert selection.matched is False
    assert "不属于当前前台 App" in selection.reason


def test_select_reports_unknown_domain_when_attribution_impossible():
    now = time.time()
    selection = select_page_flows(
        [_flow(UNKNOWN_HOST, now - 5)],
        foreground_package=BILI_PACKAGE,
        now=now,
    )
    assert selection.flows == ()
    assert "域名未收录" in selection.reason


def test_select_without_foreground_falls_back_to_recent_flows():
    now = time.time()
    selection = select_page_flows(
        [_flow(BILI_FEED, now - 5), _flow(DOUYIN_FEED, now - 2)],
        now=now,
    )
    assert len(selection.flows) == 2
    assert selection.unknown_foreground is True
    assert any("无法确认当前前台 App" in note for note in selection.notes)


def test_select_action_window_excludes_old_page_flows():
    now = time.time()
    flows = [
        _flow(BILI_FEED, now - 40, body="旧页面"),
        _flow(BILI_VIEW, now - 5, body="新页面"),
    ]
    selection = select_page_flows(
        flows,
        foreground_package=BILI_PACKAGE,
        now=now,
        since_ts=now - 20,
    )
    assert selection.action_window is True
    assert [flow["url"] for flow in selection.flows] == [BILI_VIEW]


def test_select_action_window_without_new_flows_reports_reason():
    now = time.time()
    selection = select_page_flows(
        [_flow(BILI_FEED, now - 40)],
        foreground_package=BILI_PACKAGE,
        now=now,
        since_ts=now - 20,
    )
    assert selection.flows == ()
    assert "设备动作之后还没有新的接口流量" in selection.reason


def test_select_drops_stale_flows():
    now = time.time()
    old = now - PAGE_FLOW_MAX_AGE_SECONDS - 30
    selection = select_page_flows(
        [_flow(BILI_FEED, old)],
        foreground_package=BILI_PACKAGE,
        now=now,
    )
    assert selection.flows == ()
    assert "已过期" in selection.reason
    assert selection.newest_age_seconds is not None
    assert selection.newest_age_seconds > PAGE_FLOW_MAX_AGE_SECONDS


def test_select_dedupes_same_page_and_keeps_latest():
    now = time.time()
    flows = [
        _flow(BILI_FEED + "&page=1", now - 9, body="第一页"),
        _flow(BILI_FEED + "&page=2", now - 4, body="第二页"),
        _flow(BILI_VIEW, now - 2, body="详情"),
    ]
    selection = select_page_flows(flows, foreground_package=BILI_PACKAGE, now=now)
    urls = [flow["url"] for flow in selection.flows]
    assert urls == [BILI_FEED + "&page=2", BILI_VIEW]


def test_render_page_flows_masks_url_and_truncates_body():
    now = time.time()
    flow = _flow(
        "https://api.bilibili.com/x/feed/index?hkey=SECRET&page=1",
        now - 5,
        body="A" * 200,
        reqbody="B" * 50,
    )
    text = render_page_flows([flow], now=now, body_chars=60)
    assert "hkey=***" in text
    assert "SECRET" not in text
    assert "B站·首页推荐" in text
    assert "响应：" in text
    assert "…" in text
    assert "5s 前" in text


def test_render_page_flows_includes_share_links():
    now = time.time()
    flow = _flow(
        BILI_FEED,
        now - 2,
        body='{"data":{"share_url":"https://www.bilibili.com/video/BV1xx411c7mD"}}',
    )
    text = render_page_flows([flow], now=now)
    assert "可分享链接：" in text
    assert "https://www.bilibili.com/video/BV1xx411c7mD" in text


def test_render_page_flows_uses_request_body_when_response_empty():
    now = time.time()
    flow = _flow(
        "https://api.bilibili.com/x/web-interface/search/type?keyword=CS2",
        now - 3,
        body="",
        reqbody='{"keyword":"CS2"}',
        method="POST",
    )
    text = render_page_flows([flow], now=now)
    assert "请求：" in text
    assert "CS2" in text


def test_render_page_flows_caps_total_chars():
    now = time.time()
    flows = [
        _flow(f"https://api.bilibili.com/x/feed/index?page={index}", now - index, body="X" * 500)
        for index in range(1, 5)
    ]
    text = render_page_flows(flows, now=now, body_chars=200, max_total_chars=400)
    assert "省略" in text


def test_build_page_traffic_unavailable_keeps_reason():
    snapshot = build_page_traffic(running=False, error="设备无法获取 root")
    assert snapshot.running is False
    assert snapshot.has_content is False
    assert "无法获取 root" in snapshot.reason
    assert "以屏幕/截图识别为准" in page_traffic_footer(snapshot)


def test_build_page_traffic_ok_and_footer():
    now = time.time()
    snapshot = build_page_traffic(
        flows=[_flow(BILI_FEED, now - 4, body='{"title":"测试视频"}')],
        running=True,
        foreground_package=BILI_PACKAGE,
        activity=".MainActivity",
        now=now,
    )
    assert snapshot.has_content is True
    assert snapshot.matched is True
    assert snapshot.newest_age_seconds == 4.0
    assert "测试视频" in snapshot.text
    footer = page_traffic_footer(snapshot, detailed=True)
    assert "多模态截图" in footer
    assert "当前前台" in footer
    assert "哔哩哔哩" in footer
    assert "多模态截图" not in page_traffic_footer(snapshot)
