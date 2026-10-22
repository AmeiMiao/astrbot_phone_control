"""core.apps 单元测试：内置画像、名称/包名解析与 B 站实测画像回填。"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError

import pytest

from core.apps import (
    KNOWN_APP_LABELS,
    UNKNOWN_APP_HINT_TEMPLATE,
    APP_PROFILES,
    PreferredAppMatch,
    all_app_keys,
    app_label,
    app_label_for_package,
    format_preferred_apps,
    match_preferred_app,
    pick_random_app,
    parse_package_list,
    resolve_app,
    resolve_package,
)


def test_builtin_profiles_have_expected_packages():
    assert APP_PROFILES["douyin"].package == "com.ss.android.ugc.aweme"
    assert APP_PROFILES["bilibili"].package == "tv.danmaku.bili"
    assert APP_PROFILES["xiaoheihe"].package == "com.max.xiaoheihe"
    assert APP_PROFILES["generic"].package == ""
    for profile in APP_PROFILES.values():
        assert profile.aliases
        assert profile.settle_seconds > 0
        assert profile.feed_scroll_direction in {"up", "down", "left", "right"}
        assert len(profile.content_area) == 4


def test_all_app_keys_keeps_display_order():
    assert all_app_keys() == ["douyin", "bilibili", "xiaoheihe", "generic"]


def test_resolve_app_by_key_case_insensitive():
    assert resolve_app("douyin").key == "douyin"
    assert resolve_app("DOUYIN").key == "douyin"
    assert resolve_app(" bilibili ").key == "bilibili"
    assert resolve_app("XiaoHeiHe").key == "xiaoheihe"


def test_resolve_app_by_alias_and_label():
    assert resolve_app("抖音").key == "douyin"
    assert resolve_app("B站").key == "bilibili"
    assert resolve_app("哔哩哔哩").key == "bilibili"
    assert resolve_app("小黑盒").key == "xiaoheihe"
    assert resolve_app("抖音").label == "抖音"


def test_resolve_app_by_package_name():
    assert resolve_app("com.ss.android.ugc.aweme").key == "douyin"
    assert resolve_app("tv.danmaku.bili").key == "bilibili"
    assert resolve_app("com.max.xiaoheihe").key == "xiaoheihe"


def test_resolve_app_unknown_package_uses_generic_profile():
    profile = resolve_app("com.example.notinstalled")
    assert profile is not None
    assert profile.key == "com.example.notinstalled"
    assert profile.package == "com.example.notinstalled"
    assert profile.content_area == (0.0, 0.0, 1.0, 1.0)


def test_resolve_app_returns_none_for_unknown_name():
    assert resolve_app("不存在的应用") is None
    assert resolve_app("") is None
    assert resolve_app(None) is None


def test_app_label_unknown_returns_input():
    assert app_label("抖音") == "抖音"
    assert app_label("unknown-app") == "unknown-app"


def test_bilibili_profile_backfilled_from_emu_lab():
    profile = APP_PROFILES["bilibili"]
    assert profile.activity == "tv.danmaku.bili/.MainActivityV2"
    assert profile.settle_seconds == 2.0
    assert profile.content_area == (0.0, 0.10, 1.0, 0.94)
    assert profile.title_res_id == "tv.danmaku.bili:id/ugc_video_description"
    assert profile.title_res_id_fallback == "tv.danmaku.bili:id/title"
    assert profile.author_res_id == "tv.danmaku.bili:id/real_desc"
    assert profile.max_cards_per_screen == 8
    assert profile.swipe_duration_ms == 400
    assert profile.dump_retries == 2
    assert profile.pause_before_dump is False
    assert profile.skip_login_activity == ""
    assert profile.exclude_text_re
    assert profile.card_desc_re


def test_bilibili_card_desc_regex_parses_structured_desc():
    pattern = re.compile(APP_PROFILES["bilibili"].card_desc_re)
    match = pattern.match(
        "视频,沉浸式入境被制裁后的俄罗斯…,97.4万观看,747弹幕,"
        "时长14分钟12秒,UP主鱼十七Justin,"
    )
    assert match is not None
    assert match.group("title") == "沉浸式入境被制裁后的俄罗斯…"
    assert match.group("play") == "97.4万"
    assert match.group("danmaku") == "747"
    assert match.group("duration") == "14分钟12秒"
    assert match.group("author") == "鱼十七Justin"


def test_bilibili_card_desc_regex_tolerates_variants():
    pattern = re.compile(APP_PROFILES["bilibili"].card_desc_re)
    # 竖版视频前缀 + 弹幕数为 "-" + 点赞段 + 竖屏尾巴
    vertical = pattern.match(
        "竖版视频,竖版标题,12.3万观看,-弹幕,时长2分钟5秒,UP主作者乙,1.2万点赞,竖屏"
    )
    assert vertical is not None
    assert vertical.group("title") == "竖版标题"
    assert vertical.group("author") == "作者乙"
    assert vertical.group("danmaku") == "-"
    assert vertical.group("likes") == "1.2万"
    # 大卡缺 UP主 段
    no_author = pattern.match("视频,大卡没有作者段,5万观看,10弹幕,时长30秒")
    assert no_author is not None
    assert no_author.group("title") == "大卡没有作者段"
    assert no_author.group("author") is None


def test_douyin_profile_backfilled():
    profile = APP_PROFILES["douyin"]
    assert profile.activity == "com.ss.android.ugc.aweme/.splash.SplashActivity"
    assert profile.settle_seconds == 1.5
    assert profile.content_area == (0.0, 0.10, 1.0, 0.93)
    assert profile.title_res_id == "com.ss.android.ugc.aweme:id/desc"
    assert profile.title_res_id_fallback == ""
    assert profile.author_res_id == "com.ss.android.ugc.aweme:id/title"
    assert profile.author_prefix == "@"
    assert profile.max_cards_per_screen == 1
    assert profile.swipe_duration_ms == 300
    # 视频自动播放时界面永不 idle：dump 前必须单击中心暂停（不能双击，双击会点赞）
    assert profile.pause_before_dump is True
    assert profile.pause_tap_ratio == (0.5, 0.5)
    assert profile.dump_retries == 2
    assert profile.skip_login_activity == ""
    assert profile.exclude_text_re


def test_xiaoheihe_profile_backfilled():
    profile = APP_PROFILES["xiaoheihe"]
    assert profile.activity == "com.max.xiaoheihe/.SplashActivity"
    assert profile.settle_seconds == 2.0
    assert profile.content_area == (0.0, 0.10, 1.0, 0.93)
    assert profile.title_res_id == "com.max.xiaoheihe:id/tv_title"
    assert profile.title_res_id_fallback == "com.max.xiaoheihe:id/tv_content"
    assert profile.author_res_id == "com.max.xiaoheihe:id/bbs_name"
    assert profile.author_prefix == ""
    assert profile.max_cards_per_screen == 3
    assert profile.swipe_duration_ms == 300
    # 冷启动必经 Splash → 验证码登录页；点左上角 X 进游客信息流（不能按 BACK）
    assert profile.skip_login_activity == "RegisterOrLoginActivityV2"
    assert profile.skip_login_tap_ratio == (0.091, 0.094)
    assert profile.pause_before_dump is False
    assert profile.dump_retries == 2
    assert profile.exclude_text_re


def test_generic_profile_keeps_placeholder_card_fields():
    profile = APP_PROFILES["generic"]
    assert profile.card_desc_re == ""
    assert profile.title_res_id == ""
    assert profile.title_res_id_fallback == ""
    assert profile.author_res_id == ""
    assert profile.author_prefix == ""
    assert profile.max_cards_per_screen == 0
    assert profile.swipe_duration_ms == 0
    assert profile.pause_before_dump is False
    assert profile.skip_login_activity == ""
    assert profile.dump_retries == 0


# ----------------------------------------------------------------------
# 任意已安装 App：中文名映射与包名解析
# ----------------------------------------------------------------------

_REQUIRED_LABELS = {
    "com.tencent.mm": "微信",
    "com.tencent.mobileqq": "QQ",
    "com.taobao.taobao": "淘宝",
    "com.jingdong.app.mall": "京东",
    "com.eg.android.AlipayGphone": "支付宝",
    "com.zhihu.android": "知乎",
    "com.sina.weibo": "微博",
    "com.xingin.xhs": "小红书",
    "com.smile.gifmaker": "快手",
    "com.douban.frodo": "豆瓣",
    "com.baidu.tieba": "百度贴吧",
    "com.netease.cloudmusic": "网易云音乐",
    "com.tencent.qqmusic": "QQ音乐",
    "com.kugou.android": "酷狗音乐",
    "com.autonavi.minimap": "高德地图",
    "com.baidu.BaiduMap": "百度地图",
    "com.sankuai.meituan": "美团",
    "me.ele": "饿了么",
    "tv.danmaku.bili": "哔哩哔哩",
    "com.bilibili.comic": "哔哩哔哩漫画",
    "com.ss.android.ugc.aweme": "抖音",
    "com.max.xiaoheihe": "小黑盒",
    "com.valvesoftware.android.steam.community": "Steam",
}


def test_known_app_labels_cover_common_apps():
    for package, label in _REQUIRED_LABELS.items():
        assert KNOWN_APP_LABELS.get(package) == label, package
    assert len(KNOWN_APP_LABELS) >= 40
    labels = list(KNOWN_APP_LABELS.values())
    assert len(labels) == len(set(labels)), "中文名不应重复"
    for package, label in KNOWN_APP_LABELS.items():
        assert "." in package and label.strip()


def test_resolve_package_by_package_label_and_alias():
    # 精确包名
    assert resolve_package("com.tencent.mm") == "com.tencent.mm"
    assert resolve_package("com.xingin.xhs") == "com.xingin.xhs"
    # 中文名
    assert resolve_package("微信") == "com.tencent.mm"
    assert resolve_package("小红书") == "com.xingin.xhs"
    assert resolve_package("哔哩哔哩漫画") == "com.bilibili.comic"
    # 内置画像别名 / 中文标签
    assert resolve_package("bilibili") == "tv.danmaku.bili"
    assert resolve_package("B站") == "tv.danmaku.bili"
    assert resolve_package("抖音") == "com.ss.android.ugc.aweme"
    assert resolve_package("小黑盒") == "com.max.xiaoheihe"


def test_resolve_package_direct_package_and_installed_filter():
    assert resolve_package("com.example.newapp") == "com.example.newapp"
    installed = ["com.example.newapp", "com.tencent.mm"]
    assert resolve_package("com.example.newapp", installed) == "com.example.newapp"
    assert resolve_package("com.not.installed", installed) is None
    assert resolve_package("微信", installed) == "com.tencent.mm"


def test_resolve_package_unknown_returns_none():
    assert resolve_package("完全没听说过的应用") is None
    assert resolve_package("") is None
    assert resolve_package(None) is None


def test_unknown_app_hint_guides_discovery():
    hint = UNKNOWN_APP_HINT_TEMPLATE.format(name="未知应用")
    assert "未知应用" in hint
    assert "phone_list_apps" in hint
    assert "phone_key home" in hint


def test_parse_package_list():
    output = (
        "package:com.tencent.mm\n"
        "package:tv.danmaku.bili\n"
        "package:com.tencent.mm\n"
        "package:\n"
        "garbage line\n"
    )
    assert parse_package_list(output) == ["com.tencent.mm", "tv.danmaku.bili"]
    assert parse_package_list("") == []
    assert parse_package_list("package:no_dot_package") == []


def test_app_label_for_package():
    assert app_label_for_package("com.tencent.mm") == "微信"
    assert app_label_for_package("com.unknown.app") == ""


# ----------------------------------------------------------------------
# 持续刷手机偏好 App：中文名/别名/包名解析
# ----------------------------------------------------------------------


def test_match_preferred_app_by_profile_label_alias_and_key():
    for name in ("哔哩哔哩", "B站", "b站", "bilibili", "BILIBILI", "bili"):
        match = match_preferred_app(name)
        assert match.matched is True, name
        assert match.package == "tv.danmaku.bili", name
        assert match.label == "哔哩哔哩", name
    for name in ("抖音", "douyin", "DOUYIN", "dy"):
        match = match_preferred_app(name)
        assert match.matched is True, name
        assert match.package == "com.ss.android.ugc.aweme", name
        assert match.label == "抖音", name
    xiaoheihe = match_preferred_app("小黑盒")
    assert xiaoheihe.matched is True
    assert xiaoheihe.package == "com.max.xiaoheihe"
    assert xiaoheihe.label == "小黑盒"


def test_match_preferred_app_by_known_app_label_table():
    assert match_preferred_app("微信").package == "com.tencent.mm"
    assert match_preferred_app("微信").label == "微信"
    assert match_preferred_app("小红书").package == "com.xingin.xhs"
    assert match_preferred_app("哔哩哔哩漫画").package == "com.bilibili.comic"
    # 中文名表里的英文标签大小写不敏感
    steam = match_preferred_app("steam")
    assert steam.matched is True
    assert steam.package == "com.valvesoftware.android.steam.community"
    assert steam.label == "Steam"


def test_match_preferred_app_by_package_name():
    match = match_preferred_app("tv.danmaku.bili")
    assert match.matched is True
    assert match.package == "tv.danmaku.bili"
    assert match.label == "哔哩哔哩"
    # 包名大小写不敏感，命中内置画像时返回规范包名
    assert match_preferred_app("TV.DANMAKU.BILI").package == "tv.danmaku.bili"
    # 中文名表里的包名（含大写段）返回规范写法
    alipay = match_preferred_app("com.eg.android.alipaygphone")
    assert alipay.package == "com.eg.android.AlipayGphone"
    assert alipay.label == "支付宝"
    # 未知但合法的包名：按包名识别，可读名退回原文
    custom = match_preferred_app("com.example.customapp")
    assert custom.matched is True
    assert custom.package == "com.example.customapp"
    assert custom.label == "com.example.customapp"


def test_match_preferred_app_unknown_stays_unmatched():
    match = match_preferred_app("完全没听说过的应用")
    assert match.raw == "完全没听说过的应用"
    assert match.label == "完全没听说过的应用"
    assert match.matched is False
    assert match.package == ""
    assert match.installed is None
    # 提供已安装列表也不臆测未识别名称的安装状态
    assert match_preferred_app("完全没听说过的应用", []).installed is None
    # generic 是通用占位画像而非具体 App，按未识别处理
    assert match_preferred_app("generic").matched is False
    assert match_preferred_app("").matched is False
    assert match_preferred_app("").label == ""
    assert match_preferred_app(None).matched is False


def test_match_preferred_app_strips_whitespace():
    match = match_preferred_app("  B站  ")
    assert match.raw == "B站"
    assert match.label == "哔哩哔哩"
    assert match.package == "tv.danmaku.bili"


def test_match_preferred_app_installed_flag():
    installed = ["com.tencent.mm", "tv.danmaku.bili"]
    # 已安装
    bili = match_preferred_app("B站", installed)
    assert bili.matched is True
    assert bili.installed is True
    # 未安装：仍识别为已知 App
    douyin = match_preferred_app("抖音", installed)
    assert douyin.matched is True
    assert douyin.installed is False
    # 直接填包名同样按列表判断
    assert match_preferred_app("com.ss.android.ugc.aweme", installed).installed is False
    assert match_preferred_app("com.tencent.mm", installed).installed is True
    # 未提供列表与空列表语义不同
    assert match_preferred_app("抖音").installed is None
    assert match_preferred_app("抖音", ()).installed is False
    # 已安装列表大小写不敏感
    assert match_preferred_app("bilibili", ["TV.DANMAKU.BILI"]).installed is True


def test_preferred_app_display_variants():
    assert (
        match_preferred_app("B站", ["tv.danmaku.bili"]).display
        == "哔哩哔哩（tv.danmaku.bili，已安装）"
    )
    assert match_preferred_app("抖音", []).display == "抖音（未安装，跳过）"
    assert match_preferred_app("小黑盒").display == "小黑盒（com.max.xiaoheihe）"
    assert match_preferred_app("完全没听说过的应用").display == "完全没听说过的应用"
    # 直接填包名且无中文映射时不重复展示包名
    custom = match_preferred_app("com.example.customapp")
    assert custom.display == "com.example.customapp"
    assert (
        match_preferred_app("com.example.customapp", ["com.example.customapp"]).display
        == "com.example.customapp（已安装）"
    )
    assert (
        match_preferred_app("com.example.customapp", []).display
        == "com.example.customapp（未安装，跳过）"
    )


def test_format_preferred_apps_joins_and_truncates():
    installed = ["tv.danmaku.bili", "com.ss.android.ugc.aweme"]
    matches = [
        match_preferred_app("B站", installed),
        match_preferred_app("抖音", installed),
        match_preferred_app("小黑盒", installed),
    ]
    all_text = (
        "哔哩哔哩（tv.danmaku.bili，已安装）、抖音（com.ss.android.ugc.aweme，已安装）、"
        "小黑盒（未安装，跳过）"
    )
    assert format_preferred_apps(matches) == all_text
    assert format_preferred_apps(matches, limit=3) == all_text
    assert format_preferred_apps(iter(matches), limit=1) == (
        "哔哩哔哩（tv.danmaku.bili，已安装）等 3 个"
    )
    assert format_preferred_apps(matches, limit=2) == (
        "哔哩哔哩（tv.danmaku.bili，已安装）、"
        "抖音（com.ss.android.ugc.aweme，已安装）等 3 个"
    )
    assert format_preferred_apps([], limit=2) == ""
    # 空输入解析结果没有可展示文本，应被忽略
    assert format_preferred_apps([match_preferred_app("")]) == ""
    # limit<=0 时不展示明细，只报总数
    assert format_preferred_apps(matches, limit=0) == "等 3 个"


def test_preferred_app_match_is_frozen_and_hashable():
    match = match_preferred_app("抖音")
    assert isinstance(match, PreferredAppMatch)
    assert hash(match) == hash(match_preferred_app("抖音"))
    with pytest.raises(FrozenInstanceError):
        match.label = "其他"  # type: ignore[misc]


def test_pick_random_app_uses_injected_rng_and_handles_empty():
    import random

    class FakeRng:
        def __init__(self, value: str) -> None:
            self.value = value

        def choice(self, items):
            assert items
            return self.value

    assert pick_random_app(["com.a", "com.b"], rng=FakeRng("com.b")) == "com.b"
    assert pick_random_app([], rng=FakeRng("x")) == ""
    assert pick_random_app(None) == ""
    assert pick_random_app(["  ", ""]) == ""
    # 默认 rng 从列表中取值
    random.seed(2026)
    assert pick_random_app(["com.a", "com.b", "com.c"]) in {
        "com.a",
        "com.b",
        "com.c",
    }
