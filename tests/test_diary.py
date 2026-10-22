"""core.diary 单元测试：JSONL 追加、截断、检索与提示词渲染。"""

from __future__ import annotations

import asyncio
import json

from core.diary import DIARY_FILENAME, PhoneDiary
from core.models import BrowseResult, FeedItem


def _item(title: str, author: str = "", texts: list[str] | None = None) -> FeedItem:
    return FeedItem(
        app="douyin",
        signature=f"sig-{title}",
        texts=texts if texts is not None else [title, author or "内容正文"],
        title=title,
        author=author,
        screenshot_path=None,
    )


def _result(app: str, titles: list[str]) -> BrowseResult:
    return BrowseResult(
        app=app,
        items=[_item(title) for title in titles],
        steps=len(titles),
        elapsed=1.5,
    )


def test_add_browse_appends_jsonl_entry(tmp_path):
    diary = PhoneDiary(tmp_path)
    entry_id = diary.add_browse(_result("抖音", ["第一条", "第二条"]), note="顺手记录")
    assert entry_id
    path = tmp_path / DIARY_FILENAME
    assert path.exists()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["id"] == entry_id
    assert record["app"] == "抖音"
    assert record["note"] == "顺手记录"
    assert len(record["items"]) == 2
    assert record["items"][0]["title"] == "第一条"
    assert record["items"][0]["text"].startswith("第一条")
    assert "浏览 抖音" in record["summary"]


def test_recent_returns_newest_first(tmp_path):
    diary = PhoneDiary(tmp_path)
    diary.add_browse(_result("抖音", ["一"]))
    diary.add_browse(_result("哔哩哔哩", ["二"]))
    diary.add_browse(_result("小黑盒", ["三"]))
    recent = diary.recent(2)
    assert [entry["app"] for entry in recent] == ["小黑盒", "哔哩哔哩"]
    assert diary.recent(0) == []


def test_max_entries_trims_oldest(tmp_path):
    diary = PhoneDiary(tmp_path, max_entries=3)
    for index in range(5):
        diary.add_browse(_result("抖音", [f"第{index}条"]))
    entries = diary.all_entries()
    assert len(entries) == 3
    titles = [entry["items"][0]["title"] for entry in entries]
    assert titles == ["第2条", "第3条", "第4条"]
    assert diary.stats()["entries"] == 3


def test_search_matches_title_author_and_note(tmp_path):
    diary = PhoneDiary(tmp_path)
    diary.add_browse(_result("抖音", ["原神攻略"]))
    diary.add_browse(_result("哔哩哔哩", ["崩坏星穹铁道"]), note="米哈游专场")
    assert [entry["app"] for entry in diary.search("原神")] == ["抖音"]
    assert [entry["app"] for entry in diary.search("米哈游")] == ["哔哩哔哩"]
    assert [entry["app"] for entry in diary.search("崩坏")] == ["哔哩哔哩"]
    assert diary.search("不存在的内容") == []


def test_search_empty_query_returns_recent(tmp_path):
    diary = PhoneDiary(tmp_path)
    diary.add_browse(_result("抖音", ["一"]))
    diary.add_browse(_result("抖音", ["二"]))
    assert len(diary.search("", limit=5)) == 2
    assert len(diary.search("   ", limit=1)) == 1


def test_render_for_prompt_contains_entries_and_respects_max_chars(tmp_path):
    diary = PhoneDiary(tmp_path)
    diary.add_browse(_result("抖音", ["一个很长很长的标题"]), note="测试备注")
    text = diary.render_for_prompt(5)
    assert "【最近浏览记忆】" in text
    assert "抖音" in text
    assert "一个很长很长的标题" in text
    assert "测试备注" in text
    assert len(text) <= 1200
    clipped = diary.render_for_prompt(5, max_chars=20)
    assert len(clipped) <= 20
    assert clipped.endswith("…")
    assert PhoneDiary(tmp_path / "empty").render_for_prompt(5) == ""


def test_stats_counts_entries_items_and_apps(tmp_path):
    diary = PhoneDiary(tmp_path)
    diary.add_browse(_result("抖音", ["一", "二"]))
    diary.add_browse(_result("抖音", ["三"]))
    diary.add_browse(_result("小黑盒", ["四"]))
    stats = diary.stats()
    assert stats["entries"] == 3
    assert stats["items"] == 4
    assert stats["apps"] == {"抖音": 2, "小黑盒": 1}
    assert stats["max_entries"] == 500
    assert stats["last_ts"] > 0
    assert stats["path"].endswith(DIARY_FILENAME)


def test_corrupt_line_is_ignored(tmp_path):
    path = tmp_path / DIARY_FILENAME
    path.write_text(
        '{"id": "old", "ts": 1.0, "app": "抖音", "items": []}\n不是 JSON\n',
        encoding="utf-8",
    )
    diary = PhoneDiary(tmp_path)
    diary.add_browse(_result("抖音", ["新条目"]))
    entries = diary.all_entries()
    assert len(entries) == 2
    assert entries[-1]["items"][0]["title"] == "新条目"


def test_async_wrappers_keep_entries_consistent(tmp_path):
    diary = PhoneDiary(tmp_path)

    async def run() -> None:
        await asyncio.gather(
            diary.add_browse_async(_result("抖音", ["并发一"])),
            diary.add_browse_async(_result("抖音", ["并发二"])),
            diary.add_browse_async(_result("抖音", ["并发三"])),
        )
        found = await diary.search_async("并发", limit=10)
        assert len(found) == 3

    asyncio.run(run())
    assert diary.stats()["entries"] == 3


def test_round_id_is_recorded_and_legacy_entries_have_no_round(tmp_path):
    diary = PhoneDiary(tmp_path)
    diary.add_browse(_result("抖音", ["一"]), round_id="round-a", round_kind="user")
    diary.add_browse(_result("抖音", ["二"]))
    entries = list(diary.all_entries())
    assert entries[0]["round_id"] == "round-a"
    assert entries[0]["round_kind"] == "user"
    assert "round_id" not in entries[1]


def test_trim_entries_protects_recent_rounds(tmp_path):
    from core.diary import protected_round_ids, trim_entries

    entries = []
    for index in range(12):
        round_id = f"r{index // 3}"  # 每 3 条一轮，共 4 轮
        entries.append({"id": str(index), "round_id": round_id, "items": []})
    assert protected_round_ids(entries, 2) == ["r3", "r2"]
    trimmed = trim_entries(entries, max_entries=6, keep_rounds=2)
    # 最近两轮共 6 条全部保留；更早的 r1/r0 被删除。
    assert [record["id"] for record in trimmed] == ["6", "7", "8", "9", "10", "11"]
    # 保护轮次总量超过上限时也不清理（示例：上限 2、保护 2 轮=6 条）。
    kept = trim_entries(entries, max_entries=2, keep_rounds=2)
    assert [record["id"] for record in kept] == ["6", "7", "8", "9", "10", "11"]
    # keep_rounds=0 时按普通上限截断。
    assert len(trim_entries(entries, max_entries=4, keep_rounds=0)) == 4


def test_apply_limits_writes_and_reports(tmp_path):
    diary = PhoneDiary(tmp_path)
    for index in range(10):
        diary.add_browse(
            _result("抖音", [f"第{index}条"]),
            round_id=f"r{index // 5}",
            round_kind="autonomous",
        )
    report = diary.apply_limits(max_entries=4, keep_rounds=1)
    assert report["before"] == 10
    assert report["after"] == 5  # 最近一轮 5 条全部受保护
    assert report["removed"] == 5
    assert report["protected_rounds"] == ["r1"]
    assert len(diary.all_entries()) == 5


def test_page_paginates_and_searches_newest_first(tmp_path):
    diary = PhoneDiary(tmp_path)
    for index in range(7):
        diary.add_browse(_result("抖音", [f"标题{index}"]), note=f"备注{index}")
    page1 = diary.page("", limit=3, offset=0)
    assert page1["total"] == 7 and page1["limit"] == 3 and page1["offset"] == 0
    assert [entry["items"][0]["title"] for entry in page1["entries"]] == [
        "标题6",
        "标题5",
        "标题4",
    ]
    page2 = diary.page("", limit=3, offset=3)
    assert [entry["items"][0]["title"] for entry in page2["entries"]] == [
        "标题3",
        "标题2",
        "标题1",
    ]
    searched = diary.page("标题2", limit=20, offset=0)
    assert searched["total"] == 1
    assert searched["entries"][0]["items"][0]["title"] == "标题2"
    assert "screenshot" not in searched["entries"][0]["items"][0]


def test_async_apply_limits_and_page(tmp_path):
    async def scenario() -> None:
        diary = PhoneDiary(tmp_path)
        for index in range(6):
            await diary.add_browse_async(
                _result("抖音", [f"第{index}条"]), round_id=f"r{index // 3}"
            )
        report = await diary.apply_limits_async(max_entries=3, keep_rounds=1)
        assert report["after"] == 3
        page = await diary.page_async("", limit=2, offset=0)
        assert page["total"] == 3

    asyncio.run(scenario())
