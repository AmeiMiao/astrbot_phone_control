"""core.browse 单元测试：用假 DeviceActions 驱动浏览流程，不依赖真机。

覆盖两条采集路径：
* 通用文本摘要（一屏一条）；
* B 站卡片级抽取（content-desc 结构化解析、resource-id 兜底、一屏多卡、
  跨屏去重、单屏条数上限）：bilibili_* 用例。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from core import browse
from core.apps import APP_PROFILES, AppProfile
from core.browse import BrowseSession, item_signature
from core.config import BrowseSettings
from core.errors import (
    ActionFailedError,
    AppNotInstalledError,
    BrowseError,
    UiDumpError,
)
from core.models import BrowseRequest, Rect, ScreenSnapshot, UiNode

TEST_PACKAGE = "com.test.app"
BILI_PACKAGE = "tv.danmaku.bili"
TEST_PROFILE = AppProfile(
    key="testapp",
    label="测试应用",
    package=TEST_PACKAGE,
    aliases=("testapp", "测试应用"),
    feed_scroll_direction="up",
    settle_seconds=0.01,
    content_area=(0.0, 0.1, 1.0, 0.9),
    exclude_text_re=(r"^导航$",),
    item_signature_keys=("text",),
)


class FakeActions:
    """按设计契约实现的 DeviceActions 鸭子类型。"""

    def __init__(
        self,
        snapshots: list[ScreenSnapshot],
        *,
        installed: tuple[str, ...] = (TEST_PACKAGE,),
        foreground_package: str = TEST_PACKAGE,
        foreground_activity: str = ".MainActivity",
        screen_size: tuple[int, int] = (1080, 2400),
        screenshot_error: Exception | None = None,
        swipe_error_at: int | None = None,
        dump_failures: int = 0,
    ) -> None:
        self.snapshots = list(snapshots)
        self.installed = set(installed)
        self.foreground_package = foreground_package
        self.foreground_activity = foreground_activity
        self.activity_queue: list[str] = []
        self.on_tap = None
        self._screen_size = screen_size
        self.opened: list[str] = []
        self.swipes: list[tuple[str, int]] = []
        self.screenshots: list[str] = []
        self.taps: list[tuple[int, int]] = []
        self.double_taps = 0
        self.screenshot_error = screenshot_error
        self.swipe_error_at = swipe_error_at
        self.swipe_count = 0
        self.wait_calls = 0
        self.dump_failures = dump_failures

    async def is_installed(self, package: str) -> bool:
        return package in self.installed

    async def open_app(self, package: str) -> None:
        self.opened.append(package)
        self.foreground_package = package

    async def screen_size(self) -> tuple[int, int]:
        return self._screen_size

    async def tap(self, x: int, y: int) -> None:
        self.taps.append((int(x), int(y)))
        if self.on_tap is not None:
            self.on_tap(int(x), int(y))

    async def double_tap(self, x: int, y: int, *, interval_ms: int = 120) -> None:
        self.double_taps += 1
        raise AssertionError("暂停/恢复播放只能单击，双击可能触发点赞")

    async def foreground(self) -> tuple[str, str]:
        activity = self.foreground_activity
        if self.activity_queue:
            if len(self.activity_queue) > 1:
                activity = self.activity_queue.pop(0)
            else:
                activity = self.activity_queue[0]
        return self.foreground_package, activity

    async def wait_until_stable(
        self, *, timeout: float = 8.0, **_: object
    ) -> ScreenSnapshot:
        self.wait_calls += 1
        if self.dump_failures > 0:
            self.dump_failures -= 1
            raise UiDumpError("ERROR: could not get idle state.")
        if not self.snapshots:
            raise UiDumpError("没有更多屏幕快照")
        return self.snapshots.pop(0)

    async def swipe_direction(
        self, direction: str, *, duration_ms: int = 400, ratio: float = 0.6
    ) -> None:
        self.swipe_count += 1
        if self.swipe_error_at is not None and self.swipe_count == self.swipe_error_at:
            raise ActionFailedError("模拟滑动失败")
        self.swipes.append((direction, duration_ms))

    async def screenshot(self, path: object) -> object:
        if self.screenshot_error is not None:
            raise self.screenshot_error
        self.screenshots.append(str(path))
        return path


def make_snapshot(
    nodes: list[tuple[str, int]],
    *,
    package: str = TEST_PACKAGE,
    width: int = 1080,
    height: int = 2400,
) -> ScreenSnapshot:
    ui_nodes = [
        UiNode(
            ref=index,
            text=text,
            bounds=Rect(0, top, width, top + 120),
            package=package,
        )
        for index, (text, top) in enumerate(nodes)
    ]
    return ScreenSnapshot(
        package=package,
        activity=".MainActivity",
        width=width,
        height=height,
        nodes=ui_nodes,
        screen_id=f"screen-{len(ui_nodes)}-{nodes[0][0] if nodes else 'empty'}",
    )


def make_content_snapshot(*texts: str) -> ScreenSnapshot:
    """正文节点放在内容区域内（y=400 起，间隔 200）。"""
    return make_snapshot([(text, 400 + index * 200) for index, text in enumerate(texts)])


def make_node_snapshot(
    nodes: list[UiNode],
    *,
    package: str = TEST_PACKAGE,
    screen_id: str = "",
) -> ScreenSnapshot:
    return ScreenSnapshot(
        package=package,
        activity=".MainActivity",
        width=1080,
        height=2400,
        nodes=list(nodes),
        screen_id=screen_id or f"screen-{len(nodes)}",
    )


def card_node(
    ref: int,
    desc: str,
    top: int,
    *,
    left: int = 0,
    right: int = 1080,
    package: str = BILI_PACKAGE,
) -> UiNode:
    """B 站信息流卡片：结构化文本放在 content-desc。"""
    return UiNode(
        ref=ref,
        desc=desc,
        bounds=Rect(left, top, right, top + 400),
        package=package,
    )


def text_node(
    ref: int,
    text: str,
    resource_id: str,
    top: int,
    *,
    left: int = 0,
    right: int = 540,
    package: str = BILI_PACKAGE,
    clickable: bool = False,
) -> UiNode:
    return UiNode(
        ref=ref,
        text=text,
        resource_id=resource_id,
        bounds=Rect(left, top, right, top + 120),
        package=package,
        clickable=clickable,
    )


def bili_card_desc(title: str, author: str = "", *, play: str = "10万", danmaku: str = "20") -> str:
    author_part = f",UP主{author}" if author else ""
    return f"视频,{title},{play}观看,{danmaku}弹幕,时长1分钟2秒{author_part},"


@pytest.fixture(autouse=True)
def patch_resolve_app(monkeypatch):
    def fake_resolve(name: str):
        text = str(name or "").strip().lower()
        if text in {"testapp", "测试应用", TEST_PACKAGE}:
            return TEST_PROFILE
        if text in {"bilibili", "b站", "tv.danmaku.bili"}:
            return APP_PROFILES["bilibili"]
        return None

    monkeypatch.setattr(browse, "resolve_app", fake_resolve)


@pytest.fixture
def bilibili(monkeypatch):
    """把 resolve_app 固定成 B 站画像，并用真实包名构造假设备。"""
    profile = APP_PROFILES["bilibili"]
    monkeypatch.setattr(browse, "resolve_app", lambda name: profile)
    return profile


def run(coroutine):
    return asyncio.run(coroutine)


def bili_actions(snapshots: list[ScreenSnapshot], **kwargs) -> FakeActions:
    return FakeActions(
        snapshots,
        installed=(BILI_PACKAGE,),
        foreground_package=BILI_PACKAGE,
        **kwargs,
    )


def bili_session(actions: FakeActions, tmp_path, **settings) -> BrowseSession:
    return BrowseSession(
        actions,
        settings=BrowseSettings(**settings),
        screenshot_dir=tmp_path,
    )


# ----------------------------------------------------------------------
# 通用文本摘要路径（无卡片画像）
# ----------------------------------------------------------------------


def test_run_collects_requested_items(tmp_path):
    actions = FakeActions(
        [make_content_snapshot("标题一", "作者一"), make_content_snapshot("标题二")]
    )
    session = BrowseSession(
        actions,
        settings=BrowseSettings(settle_seconds=0.01),
        screenshot_dir=tmp_path,
    )
    result = run(session.run(BrowseRequest(app="testapp", items=2)))
    assert result.app == "测试应用"
    assert len(result.items) == 2
    assert result.steps == 2
    assert result.errors == []
    assert result.items[0].title == "标题一"
    assert result.items[1].title == "标题二"
    assert result.items[0].signature == item_signature(["标题一", "作者一"])
    assert result.elapsed >= 0
    assert actions.opened == [TEST_PACKAGE]
    # 第二屏即达到目标，最后不滑动
    assert len(actions.swipes) == 1
    assert len(actions.screenshots) == 2
    assert result.items[0].screenshot_path is not None
    assert result.items[0].meta["package"] == TEST_PACKAGE


def test_duplicate_signature_retries_and_recovers(tmp_path):
    actions = FakeActions(
        [
            make_content_snapshot("重复标题"),
            make_content_snapshot("重复标题"),
            make_content_snapshot("新标题"),
        ]
    )
    session = BrowseSession(
        actions,
        settings=BrowseSettings(settle_seconds=0.01),
        screenshot_dir=tmp_path,
    )
    result = run(
        session.run(
            BrowseRequest(app="testapp", items=2, collect_screenshots=False)
        )
    )
    assert [item.title for item in result.items] == ["重复标题", "新标题"]
    assert result.steps == 3
    assert len(actions.swipes) == 2
    assert result.errors == []


def test_persistent_duplicate_breaks_early(tmp_path):
    snapshots = [make_content_snapshot("重复标题") for _ in range(4)]
    actions = FakeActions(snapshots)
    session = BrowseSession(
        actions, settings=BrowseSettings(settle_seconds=0.01), screenshot_dir=tmp_path
    )
    result = run(
        session.run(
            BrowseRequest(app="testapp", items=2, collect_screenshots=False)
        )
    )
    assert len(result.items) == 1
    assert result.steps == 4
    assert result.errors and "连续多屏没有新内容" in result.errors[0]


def test_not_installed_raises(tmp_path):
    actions = FakeActions([make_content_snapshot("标题")], installed=())
    session = BrowseSession(
        actions, settings=BrowseSettings(settle_seconds=0.01), screenshot_dir=tmp_path
    )
    with pytest.raises(AppNotInstalledError):
        run(session.run(BrowseRequest(app="testapp", items=1)))
    assert actions.opened == []


def test_unknown_app_raises(tmp_path):
    actions = FakeActions([make_content_snapshot("标题")])
    session = BrowseSession(
        actions, settings=BrowseSettings(settle_seconds=0.01), screenshot_dir=tmp_path
    )
    with pytest.raises(BrowseError):
        run(session.run(BrowseRequest(app="不存在的应用", items=1)))


def test_content_area_and_noise_are_filtered(tmp_path):
    snapshot = make_snapshot(
        [
            ("顶部状态栏", 30),
            ("导航", 400),
            ("真正的正文标题", 800),
            ("底部快捷栏", 2300),
        ]
    )
    actions = FakeActions([snapshot])
    session = BrowseSession(
        actions, settings=BrowseSettings(settle_seconds=0.01), screenshot_dir=tmp_path
    )
    result = run(
        session.run(
            BrowseRequest(app="testapp", items=1, collect_screenshots=False)
        )
    )
    assert len(result.items) == 1
    assert result.items[0].texts == ["真正的正文标题"]
    assert "顶部状态栏" not in result.items[0].texts
    assert "导航" not in result.items[0].texts


def test_empty_screen_returns_no_items(tmp_path):
    snapshot = make_snapshot([("导航", 400)])
    actions = FakeActions([snapshot, snapshot, snapshot])
    session = BrowseSession(
        actions, settings=BrowseSettings(settle_seconds=0.01), screenshot_dir=tmp_path
    )
    result = run(
        session.run(
            BrowseRequest(app="testapp", items=1, collect_screenshots=False)
        )
    )
    assert result.items == []
    assert result.steps == 3
    assert result.errors and "连续多屏没有新内容" in result.errors[0]


def test_screenshot_failure_does_not_break_browse(tmp_path):
    actions = FakeActions(
        [make_content_snapshot("标题")],
        screenshot_error=ActionFailedError("截图失败"),
    )
    session = BrowseSession(
        actions, settings=BrowseSettings(settle_seconds=0.01), screenshot_dir=tmp_path
    )
    result = run(session.run(BrowseRequest(app="testapp", items=1)))
    assert len(result.items) == 1
    assert result.items[0].screenshot_path is None
    assert result.errors == []


def test_swipe_failure_is_recorded_and_browse_continues(tmp_path):
    actions = FakeActions(
        [make_content_snapshot("标题一"), make_content_snapshot("标题二")],
        swipe_error_at=1,
    )
    session = BrowseSession(
        actions, settings=BrowseSettings(settle_seconds=0.01), screenshot_dir=tmp_path
    )
    result = run(
        session.run(
            BrowseRequest(app="testapp", items=2, collect_screenshots=False)
        )
    )
    assert len(result.items) == 2
    assert len(result.errors) == 1
    assert "滑动失败" in result.errors[0]


def test_generic_profile_uses_foreground_package(tmp_path, monkeypatch):
    generic = APP_PROFILES["generic"]
    monkeypatch.setattr(browse, "resolve_app", lambda name: generic)
    actions = FakeActions([make_content_snapshot("任意应用标题")])
    session = BrowseSession(
        actions, settings=BrowseSettings(settle_seconds=0.01), screenshot_dir=tmp_path
    )
    result = run(
        session.run(
            BrowseRequest(app="generic", items=1, collect_screenshots=False)
        )
    )
    assert actions.opened == [TEST_PACKAGE]
    assert len(result.items) == 1
    assert result.items[0].title == "任意应用标题"


def test_item_count_is_clamped_to_settings_max(tmp_path):
    actions = FakeActions(
        [make_content_snapshot("标题一"), make_content_snapshot("标题二")]
    )
    session = BrowseSession(
        actions,
        settings=BrowseSettings(settle_seconds=0.01, max_items=2),
        screenshot_dir=tmp_path,
    )
    result = run(
        session.run(
            BrowseRequest(app="testapp", items=99, collect_screenshots=False)
        )
    )
    assert len(result.items) == 2
    assert result.steps == 2


def test_author_guessed_from_at_text(tmp_path):
    actions = FakeActions([make_content_snapshot("一段正文内容", "@小明")])
    session = BrowseSession(
        actions, settings=BrowseSettings(settle_seconds=0.01), screenshot_dir=tmp_path
    )
    result = run(
        session.run(
            BrowseRequest(app="testapp", items=1, collect_screenshots=False)
        )
    )
    assert result.items[0].author == "小明"
    assert result.items[0].title == "一段正文内容"


# ----------------------------------------------------------------------
# B 站卡片级抽取路径
# ----------------------------------------------------------------------


def test_bilibili_parses_structured_card_desc(bilibili, tmp_path):
    snapshot = make_node_snapshot(
        [
            card_node(
                0,
                "视频,沉浸式入境被制裁后的俄罗斯…,97.4万观看,747弹幕,"
                "时长14分钟12秒,UP主鱼十七Justin,",
                300,
            ),
            card_node(
                1,
                "竖版视频,第二个视频标题,12.3万观看,-弹幕,时长2分钟5秒,"
                "UP主作者乙,1.2万点赞,竖屏",
                900,
            ),
            card_node(
                2,
                "视频,大卡没有作者段,5万观看,10弹幕,时长30秒",
                1500,
            ),
            UiNode(ref=3, text="首页", bounds=Rect(0, 2280, 300, 2350), package=BILI_PACKAGE),
            UiNode(ref=4, text="顶部频道", bounds=Rect(0, 50, 600, 150), package=BILI_PACKAGE),
        ],
        package=BILI_PACKAGE,
    )
    actions = bili_actions([snapshot])
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="bilibili", items=3, collect_screenshots=False)
        )
    )
    assert len(result.items) == 3
    assert result.steps == 1
    assert result.errors == []
    assert actions.swipes == []
    first, second, third = result.items
    assert first.title == "沉浸式入境被制裁后的俄罗斯…"
    assert first.author == "鱼十七Justin"
    assert first.meta["play"] == "97.4万"
    assert first.meta["danmaku"] == "747"
    assert first.meta["duration"] == "14分钟12秒"
    assert second.title == "第二个视频标题"
    assert second.author == "作者乙"
    assert second.meta["danmaku"] == "-"
    assert second.meta["likes"] == "1.2万"
    assert third.title == "大卡没有作者段"
    assert third.author == ""


def test_bilibili_dedupes_overlapping_cards_across_screens(bilibili, tmp_path):
    def screen(*titles: str, screen_id: str) -> ScreenSnapshot:
        return make_node_snapshot(
            [
                card_node(index, bili_card_desc(title, author=f"作者{title}"), 300 + index * 500)
                for index, title in enumerate(titles)
            ],
            package=BILI_PACKAGE,
            screen_id=screen_id,
        )

    actions = bili_actions(
        [
            screen("A", "B", screen_id="s1"),
            screen("B", "C", "D", screen_id="s2"),
            screen("D", "E", "F", screen_id="s3"),
        ]
    )
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="bilibili", items=5, collect_screenshots=False)
        )
    )
    assert [item.title for item in result.items] == ["A", "B", "C", "D", "E"]
    assert result.steps == 3
    assert actions.swipes == [("up", 400), ("up", 400)]
    assert result.errors == []


def test_bilibili_resid_fallback_for_big_card(bilibili, tmp_path):
    snapshot = make_node_snapshot(
        [
            text_node(
                0,
                "大卡标题（无结构化 desc）",
                "tv.danmaku.bili:id/ugc_video_description",
                300,
                right=1080,
            ),
            text_node(
                1,
                "大卡UP主",
                "tv.danmaku.bili:id/real_desc",
                700,
                clickable=True,
            ),
        ],
        package=BILI_PACKAGE,
    )
    actions = bili_actions([snapshot])
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="bilibili", items=1, collect_screenshots=False)
        )
    )
    assert len(result.items) == 1
    assert result.items[0].title == "大卡标题（无结构化 desc）"
    assert result.items[0].author == "大卡UP主"
    assert result.items[0].meta["source"] == "res_id"


def test_bilibili_resid_fallback_uses_second_title_id(bilibili, tmp_path):
    snapshot = make_node_snapshot(
        [
            text_node(
                0,
                "双列卡标题",
                "tv.danmaku.bili:id/title",
                300,
            ),
            text_node(
                1,
                "双列卡作者",
                "tv.danmaku.bili:id/real_desc",
                700,
            ),
        ],
        package=BILI_PACKAGE,
    )
    actions = bili_actions([snapshot])
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="bilibili", items=1, collect_screenshots=False)
        )
    )
    assert len(result.items) == 1
    assert result.items[0].title == "双列卡标题"
    assert result.items[0].author == "双列卡作者"


def test_bilibili_max_cards_per_screen_limits_extraction(
    bilibili, tmp_path, monkeypatch
):
    limited = replace(APP_PROFILES["bilibili"], max_cards_per_screen=2)
    monkeypatch.setattr(browse, "resolve_app", lambda name: limited)
    snapshot = make_node_snapshot(
        [
            card_node(
                index,
                bili_card_desc(title, author=f"作者{title}"),
                300 + index * 500,
            )
            for index, title in enumerate(("A", "B", "C"))
        ],
        package=BILI_PACKAGE,
    )
    actions = bili_actions([snapshot, snapshot, snapshot, snapshot])
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="bilibili", items=3, collect_screenshots=False)
        )
    )
    assert len(result.items) == 2
    assert result.errors and "连续多屏没有新内容" in result.errors[0]


def test_bilibili_summary_fallback_without_card_markers(bilibili, tmp_path):
    snapshot = make_node_snapshot(
        [
            text_node(0, "普通文本标题", "", 300, right=1080),
            text_node(1, "普通文本作者", "", 700),
            UiNode(ref=2, text="首页", bounds=Rect(0, 2280, 300, 2350), package=BILI_PACKAGE),
        ],
        package=BILI_PACKAGE,
    )
    actions = bili_actions([snapshot])
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="bilibili", items=1, collect_screenshots=False)
        )
    )
    assert len(result.items) == 1
    assert result.items[0].title == "普通文本标题"
    assert result.items[0].meta["source"] == "summary"

# 真实 dump XML 片段（真机 B 站信息流首屏，900x1600 竖屏）：
# 6 张完整卡片 + 2 张底部被裁切卡片 + 底部导航「首页」；用于校验真实
# uiautomator 输出经 parse_ui_xml → 卡片抽取的完整链路。
_BILI_REAL_FEED_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0"><node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="tv.danmaku.bili" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,0][900,1600]">
  <node index="0" text="" resource-id="" class="android.view.ViewGroup" package="tv.danmaku.bili" content-desc="视频,沉浸式入境被制裁后的俄罗斯，究竟是一片破败还是歌舞升平！？,97.4万观看,747弹幕,时长14分钟12秒,UP主鱼十七Justin," checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[10,120][445,412]" />
  <node index="1" text="" resource-id="" class="android.view.ViewGroup" package="tv.danmaku.bili" content-desc="视频,拳打KTV，脚踢足疗店，网吧为啥复兴了？【阿Test正经比比】,107.2万观看,6684弹幕,时长17分钟59秒,UP主阿Test正经比比," checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[455,120][890,412]" />
  <node index="2" text="" resource-id="" class="android.view.ViewGroup" package="tv.danmaku.bili" content-desc="视频,爱奇艺这是要凉透了，从46跌到1块，纯纯自己作的,461.3万观看,1.4万弹幕,时长4分钟41秒,UP主爱德华说," checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[10,422][445,910]" />
  <node index="3" text="" resource-id="" class="android.view.ViewGroup" package="tv.danmaku.bili" content-desc="视频,海底一万米到底有什么？一条视频带你看懂深海一万米。 #科普 #远古生物,17.1万观看,105弹幕,时长16分钟29秒,UP主脑洞科普探索," checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[455,422][890,910]" />
  <node index="4" text="" resource-id="" class="android.view.ViewGroup" package="tv.danmaku.bili" content-desc="视频,2026 国产安卓新机 体验对比,20.1万观看,615弹幕,时长13分钟12秒,UP主七月的琦玉," checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[10,920][445,1408]" />
  <node index="5" text="" resource-id="" class="android.view.ViewGroup" package="tv.danmaku.bili" content-desc="竖版视频,12岁男孩不慎撞倒价值4万元的摩托车，尝试扶车27次；车主：勇于承担责任，不要他赔偿。网友：车主有温度，男孩有担当！,25.5万观看,166弹幕,时长0分钟43秒,UP主人民日报,竖屏," checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[455,920][890,1408]" />
  <node index="6" text="" resource-id="" class="android.view.ViewGroup" package="tv.danmaku.bili" content-desc="视频,一场没有终点的竞走比赛，停下来就死，吃喝拉撒都得边走边完成！,689.7万观看,1487弹幕,时长16分钟36秒,UP主摩斯神探," checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[10,1418][445,1600]" />
  <node index="7" text="" resource-id="" class="android.view.ViewGroup" package="tv.danmaku.bili" content-desc="视频,东南亚魔法大战？泰柬边境“咒术回战”是咋回事？,86.7万观看,4308弹幕,时长15分钟55秒,UP主世界见闻录," checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[455,1418][890,1600]" />
  <node index="9" text="首页" resource-id="" class="android.widget.TextView" package="tv.danmaku.bili" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,1530][180,1595]" />
</node></hierarchy>"""


def _real_bili_snapshot() -> ScreenSnapshot:
    from core.ui import parse_ui_xml

    nodes = parse_ui_xml(_BILI_REAL_FEED_XML, package=BILI_PACKAGE)
    return ScreenSnapshot(
        package=BILI_PACKAGE,
        activity=".MainActivityV2",
        width=900,
        height=1600,
        nodes=nodes,
        screen_id="real-bili-feed",
    )


def test_bilibili_real_dump_fragment_card_extraction(bilibili, tmp_path):
    snapshot = _real_bili_snapshot()
    actions = bili_actions([snapshot])
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="bilibili", items=6, collect_screenshots=False)
        )
    )
    # 6 张卡片中心落在内容区域内 → 一屏采满 6 条；底部被裁切的 2 条被区域过滤。
    assert len(result.items) == 6
    assert result.steps == 1
    assert result.errors == []
    assert actions.swipes == []
    titles = [item.title for item in result.items]
    assert titles[0].startswith("沉浸式入境被制裁后的俄罗斯")
    assert titles[1].startswith("拳打KTV")
    assert titles[-1].startswith("12岁男孩")  # 竖版视频卡片
    authors = [item.author for item in result.items]
    assert authors[0] == "鱼十七Justin"
    assert authors[-1] == "人民日报"
    assert result.items[0].meta["play"] == "97.4万"
    assert result.items[0].meta["danmaku"] == "747"
    assert result.items[-1].meta["duration"] == "0分钟43秒"
    assert all(item.signature for item in result.items)

# ----------------------------------------------------------------------
# 抖音 / 小黑盒 / dump 重试适配
# ----------------------------------------------------------------------

DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"


def douyin_snapshot() -> ScreenSnapshot:
    return make_node_snapshot(
        [
            text_node(
                0,
                "@作者小明",
                "com.ss.android.ugc.aweme:id/title",
                1000,
                right=500,
                package=DOUYIN_PACKAGE,
            ),
            text_node(
                1,
                "视频文案内容 #话题",
                "com.ss.android.ugc.aweme:id/desc",
                1120,
                right=900,
                package=DOUYIN_PACKAGE,
            ),
            # 同一作者 id 下的『相关搜索』噪声，必须被 author_prefix 过滤
            text_node(
                2,
                "相关搜索",
                "com.ss.android.ugc.aweme:id/title",
                1300,
                right=300,
                package=DOUYIN_PACKAGE,
            ),
        ],
        package=DOUYIN_PACKAGE,
    )


def test_douyin_pause_before_dump_taps_center_once_each(monkeypatch, tmp_path):
    profile = APP_PROFILES["douyin"]
    monkeypatch.setattr(browse, "resolve_app", lambda name: profile)
    monkeypatch.setattr(browse, "_PAUSE_SETTLE_SECONDS", 0.01)
    actions = FakeActions(
        [douyin_snapshot()],
        installed=(DOUYIN_PACKAGE,),
        foreground_package=DOUYIN_PACKAGE,
        screen_size=(1080, 2400),
    )
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="douyin", items=1, collect_screenshots=False)
        )
    )
    assert len(result.items) == 1
    assert result.items[0].title == "视频文案内容 #话题"
    assert result.items[0].author == "作者小明"
    # dump 前暂停一次 + dump 后恢复一次，都是单击（不能双击点赞），且未滑动
    assert actions.taps == [(540, 1200), (540, 1200)]
    assert actions.double_taps == 0
    assert actions.swipes == []


def test_dump_failure_retries_then_succeeds(monkeypatch, tmp_path):
    profile = replace(APP_PROFILES["bilibili"], dump_retries=2)
    monkeypatch.setattr(browse, "resolve_app", lambda name: profile)
    monkeypatch.setattr(browse, "_DUMP_RETRY_DELAY", 0.01)
    snapshot = make_node_snapshot(
        [card_node(0, bili_card_desc("重试后成功的标题", author="作者甲"), 300)],
        package=BILI_PACKAGE,
    )
    actions = FakeActions(
        [snapshot],
        installed=(BILI_PACKAGE,),
        foreground_package=BILI_PACKAGE,
        dump_failures=2,
    )
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="bilibili", items=1, collect_screenshots=False)
        )
    )
    assert len(result.items) == 1
    assert result.errors == []
    assert actions.wait_calls == 3


def test_dump_failure_exhausted_records_errors(monkeypatch, tmp_path):
    profile = replace(APP_PROFILES["bilibili"], dump_retries=2)
    monkeypatch.setattr(browse, "resolve_app", lambda name: profile)
    monkeypatch.setattr(browse, "_DUMP_RETRY_DELAY", 0.01)
    actions = FakeActions(
        [],
        installed=(BILI_PACKAGE,),
        foreground_package=BILI_PACKAGE,
        dump_failures=100,
    )
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="bilibili", items=1, collect_screenshots=False)
        )
    )
    assert result.items == []
    # 3 次尝试/屏 × 连续 3 屏失败后提前结束
    assert actions.wait_calls == 9
    assert any("连续失败" in error for error in result.errors)
    assert any("idle state" in error for error in result.errors)


def test_xiaoheihe_skips_login_page_by_tapping_x(monkeypatch, tmp_path):
    profile = APP_PROFILES["xiaoheihe"]
    monkeypatch.setattr(browse, "resolve_app", lambda name: profile)
    monkeypatch.setattr(browse, "_SKIP_LOGIN_TAP_DELAY", 0.01)
    monkeypatch.setattr(browse, "_SKIP_LOGIN_RECHECK_DELAY", 0.01)
    monkeypatch.setattr(browse, "_SKIP_LOGIN_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(browse, "_SKIP_LOGIN_TIMEOUT", 5.0)
    snapshot = make_node_snapshot(
        [
            text_node(
                0,
                "盒友甲",
                "com.max.xiaoheihe:id/bbs_name",
                300,
                package="com.max.xiaoheihe",
            ),
            text_node(
                1,
                "帖子标题一",
                "com.max.xiaoheihe:id/tv_title",
                380,
                right=880,
                package="com.max.xiaoheihe",
            ),
        ],
        package="com.max.xiaoheihe",
    )
    actions = FakeActions(
        [snapshot],
        installed=("com.max.xiaoheihe",),
        foreground_package="com.max.xiaoheihe",
        screen_size=(900, 1600),
    )
    actions.activity_queue = [
        "com.max.xiaoheihe/.SplashActivity",
        "com.max.xiaoheihe/.RegisterOrLoginActivityV2",
    ]

    def on_tap(x: int, y: int) -> None:
        actions.activity_queue.clear()
        actions.foreground_activity = "com.max.xiaoheihe/.MainActivity"

    actions.on_tap = on_tap
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="xiaoheihe", items=1, collect_screenshots=False)
        )
    )
    assert len(result.items) == 1
    assert result.items[0].title == "帖子标题一"
    # 按 900x1600 折算左上角 X：(81, 150)
    assert actions.taps == [(81, 150)]
    assert actions.opened == ["com.max.xiaoheihe"]


def test_xiaoheihe_pure_text_post_uses_content_first_line(monkeypatch, tmp_path):
    profile = APP_PROFILES["xiaoheihe"]
    monkeypatch.setattr(browse, "resolve_app", lambda name: profile)
    snapshot = make_node_snapshot(
        [
            text_node(
                0,
                "盒友甲",
                "com.max.xiaoheihe:id/bbs_name",
                300,
                package="com.max.xiaoheihe",
            ),
            text_node(
                1,
                "帖子标题一",
                "com.max.xiaoheihe:id/tv_title",
                380,
                right=880,
                package="com.max.xiaoheihe",
            ),
            text_node(
                2,
                "盒友乙",
                "com.max.xiaoheihe:id/bbs_name",
                800,
                package="com.max.xiaoheihe",
            ),
            text_node(
                3,
                "纯文字第一行\n第二行正文",
                "com.max.xiaoheihe:id/tv_content",
                900,
                right=880,
                package="com.max.xiaoheihe",
            ),
        ],
        package="com.max.xiaoheihe",
    )
    actions = FakeActions(
        [snapshot],
        installed=("com.max.xiaoheihe",),
        foreground_package="com.max.xiaoheihe",
    )
    result = run(
        bili_session(actions, tmp_path).run(
            BrowseRequest(app="xiaoheihe", items=2, collect_screenshots=False)
        )
    )
    assert [item.title for item in result.items] == ["帖子标题一", "纯文字第一行"]
    assert [item.author for item in result.items] == ["盒友甲", "盒友乙"]
    assert result.items[0].meta["source"] == "res_id"
    assert result.items[1].meta["source"] == "res_id_fallback"
    assert actions.taps == []
    assert actions.swipes == []
