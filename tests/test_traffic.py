"""core.traffic 单元测试：抓包 addon 生成、JSONL 解析、打码与页面推断/渲染。"""

from __future__ import annotations

import json
import time

from core.traffic import (
    DEFAULT_BODY_CHARS,
    extract_share_links,
    DEFAULT_KEEP_SECONDS,
    DEFAULT_TRAFFIC_PORT,
    build_addon_script,
    infer_flow_hints,
    load_recent_flows,
    mask_url,
    normalize_body_chars,
    normalize_keep_seconds,
    parse_flow_line,
    prune_log,
    render_traffic,
)


def _flow(url: str, *, body: str = "", status: int = 200, ctype: str = "application/json", ts=None):
    return {
        "ts": time.time() if ts is None else ts,
        "method": "GET",
        "url": url,
        "status": status,
        "ctype": ctype,
        "reqbody": "",
        "body": body,
    }


def test_defaults():
    assert DEFAULT_TRAFFIC_PORT == 18080
    assert DEFAULT_BODY_CHARS == 1200
    assert DEFAULT_KEEP_SECONDS == 900


def test_normalize_body_chars_and_keep_seconds():
    assert normalize_body_chars(1200) == 1200
    assert normalize_body_chars(0) == 200
    assert normalize_body_chars(99999) == 8000
    assert normalize_body_chars("bad") == DEFAULT_BODY_CHARS
    assert normalize_keep_seconds(900) == 900
    assert normalize_keep_seconds(0) == 60
    assert normalize_keep_seconds(999999) == 86400
    assert normalize_keep_seconds(None) == DEFAULT_KEEP_SECONDS


def test_mask_url_hides_sensitive_query():
    url = "https://api.xiaoheihe.cn/bbs/app/feed?hkey=ABC&nonce=NNN&page=2&token=SECRET"
    masked = mask_url(url)
    assert "hkey=***" in masked
    assert "nonce=***" in masked
    assert "token=***" in masked
    assert "page=2" in masked
    assert "ABC" not in masked and "SECRET" not in masked
    # 无 query / 非法 URL 原样返回
    assert mask_url("https://api.bilibili.com/x/feed/index") == "https://api.bilibili.com/x/feed/index"
    assert mask_url("") == ""


def test_build_addon_script_contains_log_and_limits():
    source = build_addon_script("/tmp/flows.jsonl", 500)
    assert 'LOG_PATH = "/tmp/flows.jsonl"' in source
    assert "BODY_CHARS = 500" in source
    assert "class PhoneTrafficAddon" in source
    assert "addons = [PhoneTrafficAddon()]" in source
    # body 长度归一化
    assert "BODY_CHARS = 200" in build_addon_script("/tmp/x", 1)


def test_parse_flow_line_and_invalid():
    record = parse_flow_line(
        json.dumps(
            {
                "ts": 123.0,
                "method": "post",
                "url": "https://api.xiaoheihe.cn/bbs/app/feed?hkey=x",
                "status": "200",
                "ctype": "application/json",
                "body": "{}",
            }
        )
    )
    assert record is not None
    assert record["method"] == "POST"
    assert record["status"] == 200
    assert parse_flow_line("") is None
    assert parse_flow_line("not-json") is None
    assert parse_flow_line(json.dumps({"status": 200})) is None


def test_load_recent_flows_filters_since_and_limit(tmp_path):
    log = tmp_path / "flows.jsonl"
    now = time.time()
    lines = [
        json.dumps({"ts": now - 500, "method": "GET", "url": "https://a.com/old", "status": 200}),
        json.dumps({"ts": now - 30, "method": "GET", "url": "https://a.com/new", "status": 200}),
        "bad-line",
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    flows = load_recent_flows(log, limit=10, since_seconds=60, now=now)
    assert [flow["url"] for flow in flows] == ["https://a.com/new"]
    flows = load_recent_flows(log, limit=10, since_seconds=0, now=now)
    assert len(flows) == 2
    assert load_recent_flows(tmp_path / "missing.jsonl") == []


def test_prune_log_keeps_recent_lines(tmp_path):
    log = tmp_path / "flows.jsonl"
    lines = [json.dumps({"ts": i, "method": "GET", "url": f"https://a.com/{i}", "status": 200}) for i in range(50)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    removed = prune_log(log, max_bytes=1, keep_lines=10)
    assert removed == 40
    remaining = log.read_text(encoding="utf-8").splitlines()
    assert len(remaining) == 10
    assert "https://a.com/49" in remaining[-1]
    # 未超过阈值不裁剪
    assert prune_log(log, max_bytes=10_000_000, keep_lines=10) == 0


def test_infer_flow_hints_known_apps():
    def hint(url: str):
        return infer_flow_hints({"url": url})

    assert hint("https://api.xiaoheihe.cn/bbs/app/feed/list?page=1") == "小黑盒·社区信息流"
    assert hint("https://api.xiaoheihe.cn/bbs/app/link/tree?link_id=1") == "小黑盒·帖子详情"
    assert hint("https://api.bilibili.com/x/feed/index?build=1") == "B站·首页推荐"
    assert hint("https://api.bilibili.com/x/web-interface/view?bvid=BV1") == "B站·视频详情"
    assert hint("https://api.xiaoheihe.cn/unknown/path") == "小黑盒·接口"
    assert hint("https://api.bilibili.com/unknown") == "B站·接口"
    assert hint("https://example.com/api") == "example.com 接口"
    assert hint("") == ""


def test_render_traffic_includes_hint_body_and_masking():
    flows = [
        _flow(
            "https://api.xiaoheihe.cn/bbs/app/feed/list?page=1&hkey=SECRET",
            body='{"result":{"links":[{"title":"测试帖子标题"}]}}',
        ),
        _flow(
            "https://api.bilibili.com/x/web-interface/view?bvid=BV1",
            body="x" * 5000,
        ),
    ]
    text = render_traffic(flows, body_chars=300)
    assert "最近抓包（2 条" in text
    assert "hkey=***" in text and "SECRET" not in text
    assert "小黑盒·社区信息流" in text
    assert "测试帖子标题" in text
    assert "B站·视频详情" in text
    assert "截断，原 5000 字符" in text
    assert "多模态截图" in text


def test_render_traffic_empty():
    text = render_traffic([])
    assert "没有抓到可识别的接口流量" in text
    assert "phone_screen" in text


def test_extract_share_links_from_share_api_response():
    flow = _flow(
        "https://api.xiaoheihe.cn/bbs/app/link/share/info",
        body=(
            '{"result":{"share_url":"https://www.xiaoheihe.cn/app/bbs/link/123",'
            '"jump_url":"https://xiaoheihe.cn/share/abc",'
            '"image":"https://img.example.com/a.jpg",'
            '"nested":{"shareLink":"https://example.com/share/xyz"}}}'
        ),
    )
    links = extract_share_links(flow)
    assert "https://www.xiaoheihe.cn/app/bbs/link/123" in links
    assert "https://xiaoheihe.cn/share/abc" in links
    assert "https://example.com/share/xyz" in links
    assert not any("a.jpg" in item for item in links)


def test_extract_share_links_filters_static_and_non_json():
    assert extract_share_links(_flow("https://a.com/x", body="not-json")) == []
    assert extract_share_links(_flow("https://a.com/x", body='{"share_url":"https://a.com/x.zip"}')) == []
    assert extract_share_links(_flow("https://a.com/x", body='{"share_url":"ftp://a.com/x"}')) == []
    # 普通字段 url 也接受；重复去重
    flow = _flow(
        "https://api.bilibili.com/x/share",
        body='{"data":{"url":"https://www.bilibili.com/video/BV1xx","url":"https://www.bilibili.com/video/BV1xx"}}',
    )
    assert extract_share_links(flow) == ["https://www.bilibili.com/video/BV1xx"]


def test_render_traffic_includes_share_links_and_hint():
    flow = _flow(
        "https://api.xiaoheihe.cn/bbs/app/link/share/info",
        body='{"result":{"share_url":"https://www.xiaoheihe.cn/app/bbs/link/123"}}',
    )
    text = render_traffic([flow], body_chars=300)
    assert "可分享链接：https://www.xiaoheihe.cn/app/bbs/link/123" in text
    assert "优先用响应里的可分享链接" in text
