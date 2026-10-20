"""core.app_memory 单元测试：App 归一化、经验覆盖、持久化、渲染与淘汰。"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import FrozenInstanceError

import pytest

from core.app_memory import (
    DEFAULT_MAX_ENTRIES,
    MEMORY_FILENAME,
    AppMemoryEntry,
    AppMemoryError,
    AppMemoryStore,
)
from core.errors import PhoneControlError


def _read_payload(tmp_path) -> dict:
    """读取落盘档案（JSON 对象）。"""
    path = tmp_path / MEMORY_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def test_app_names_normalize_to_package(tmp_path):
    """中文名 / 别名 / 包名 / 大小写输入都归一为同一条经验。"""
    store = AppMemoryStore(tmp_path)
    for name in ("抖音", "dy", "aweme", "com.ss.android.ugc.aweme", "DouYin"):
        entry = store.remember(
            app=name, note="右下角 + 号进入发布页", page="发布", ts=1_000.0
        )
        assert entry.app == "com.ss.android.ugc.aweme"
        assert entry.label == "抖音"
    # 同 (app, page) 覆盖，只有一条经验
    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].version == 5
    # 各种写法都能查到同一条
    for name in ("抖音", "dy", "com.ss.android.ugc.aweme", "DOUYIN"):
        found = store.lookup(name)
        assert len(found) == 1
        assert found[0].note == "右下角 + 号进入发布页"


def test_known_app_label_mapping(tmp_path):
    """内置中文名映射（微信）也能归一成包名与可读名。"""
    store = AppMemoryStore(tmp_path)
    entry = store.remember(app="微信", note="发现页入口在「我」-「设置」", ts=1_000.0)
    assert entry.app == "com.tencent.mm"
    assert entry.label == "微信"
    assert store.lookup("com.tencent.mm")[0].app == "com.tencent.mm"


def test_remember_overwrites_same_app_page_and_keeps_hits(tmp_path):
    """同 (app, page) 覆盖更新：version+1、保留 hits、不同 page 独立。"""
    store = AppMemoryStore(tmp_path)
    first = store.remember(app="抖音", note="旧布局：右下角 + 号", page="发布", ts=1_000.0)
    assert first.version == 1
    assert first.hits == 0
    assert store.lookup("抖音", page="发布")[0].hits == 1

    second = store.remember(
        app="com.ss.android.ugc.aweme",
        note="新布局：  中间底部 + 号 ",
        page="发布",
        ts=2_000.0,
    )
    assert second.version == 2
    assert second.hits == 1
    assert second.note == "新布局： 中间底部 + 号"
    assert second.updated_at == 2_000.0
    assert len(store.entries()) == 1

    other = store.remember(app="抖音", note="私信在顶部", page="消息", ts=3_000.0)
    assert other.version == 1
    assert len(store.entries()) == 2


def test_lookup_prioritizes_page_then_recency(tmp_path):
    """page 精确匹配优先，其余按 updated_at 倒序。"""
    store = AppMemoryStore(tmp_path)
    store.remember(app="抖音", note="通用经验", ts=1_000.0)
    store.remember(app="抖音", note="首页经验", page="首页", ts=2_000.0)
    store.remember(app="抖音", note="发布页经验", page="发布", ts=3_000.0)
    store.remember(app="微信", note="别的 App", ts=4_000.0)

    result = store.lookup("抖音", page="发布", limit=5)
    assert [entry.page for entry in result] == ["发布", "首页", ""]
    assert result[0].note == "发布页经验"

    assert [entry.page for entry in store.lookup("抖音", limit=5)] == ["发布", "首页", ""]
    assert [entry.page for entry in store.lookup("抖音", limit=1)] == ["发布"]
    assert store.lookup("抖音", limit=0) == []
    assert store.lookup("不存在的 App") == []


def test_steps_normalized_and_capped(tmp_path):
    """步骤去空白、单条截断、总条数上限与字符串拆分。"""
    store = AppMemoryStore(tmp_path)
    entry = store.remember(
        app="抖音",
        note="步骤经验",
        steps=["  点击右下角 + 号 ", "", "选择相册；发布"],
        ts=1.0,
    )
    assert entry.steps == ("点击右下角 + 号", "选择相册；发布")

    long_step = "步" * 250
    capped = store.remember(
        app="抖音", page="长步骤", note="长步骤", steps=[long_step], ts=2.0
    )
    assert len(capped.steps) == 1
    assert len(capped.steps[0]) == 200
    assert capped.steps[0].endswith("…")

    many = store.remember(
        app="抖音",
        page="多步骤",
        note="多步骤",
        steps=[f"第{index}步" for index in range(30)],
        ts=3.0,
    )
    assert len(many.steps) == 20

    text_steps = store.remember(
        app="微信", note="字符串步骤", steps="打开我；进入设置\n点击通用", ts=4.0
    )
    assert text_steps.steps == ("打开我", "进入设置", "点击通用")

    bad = store.remember(app="微信", page="坏步骤", note="坏步骤", steps=123, ts=5.0)
    assert bad.steps == ()


def test_invalid_note_and_app_raise_app_memory_error(tmp_path):
    """非法 App 名 / 空正文 / 超长正文抛 AppMemoryError。"""
    store = AppMemoryStore(tmp_path)
    for bad in ("", "   ", "\n\t", None):
        with pytest.raises(AppMemoryError):
            store.remember(app="抖音", note=bad)
    with pytest.raises(AppMemoryError):
        store.remember(app="抖音", note="字" * 501)
    with pytest.raises(AppMemoryError):
        store.remember(app="   ", note="经验")
    with pytest.raises(AppMemoryError):
        store.remember(app=None, note="经验")
    assert issubclass(AppMemoryError, PhoneControlError)

    # 边界值 1 字 / 500 字合法
    assert store.remember(app="抖音", note="字", ts=1.0).note == "字"
    five_hundred = store.remember(app="抖音", page="长文", note="字" * 500, ts=2.0)
    assert len(five_hundred.note) == 500


def test_persists_to_json_object_and_reloads(tmp_path):
    """JSON 对象（entries 列表）持久化、原子写无临时文件、重载一致。"""
    store = AppMemoryStore(tmp_path)
    store.remember(
        app="抖音",
        note="右下角 + 号发布",
        page="发布",
        steps=["点击 + 号"],
        ts=1_760_000_000.0,
    )
    store.remember(app="微信", note="发现页在「我」", ts=1_760_000_100.0)

    payload = _read_payload(tmp_path)
    assert isinstance(payload, dict)
    assert isinstance(payload["entries"], list)
    assert {entry["app"] for entry in payload["entries"]} == {
        "com.ss.android.ugc.aweme",
        "com.tencent.mm",
    }
    for raw in payload["entries"]:
        assert {"app", "label", "page", "note", "steps", "updated_at", "hits", "version"} <= set(raw)
    # 原子写：不残留 .tmp 临时文件
    assert not list(tmp_path.glob("*.tmp"))

    reloaded = AppMemoryStore(tmp_path)
    assert [entry.as_dict() for entry in reloaded.entries()] == [
        entry.as_dict() for entry in store.entries()
    ]

    custom = AppMemoryStore(tmp_path, filename="other_memory.json")
    assert custom.path.name == "other_memory.json"
    custom.remember(app="微信", note="另一个文件", ts=2.0)
    assert (tmp_path / "other_memory.json").exists()
    assert len(store.entries()) == 2


def test_overflow_evicts_oldest_by_updated_at(tmp_path):
    """超过 max_entries 时按 updated_at 淘汰最旧，文件同步收敛。"""
    store = AppMemoryStore(tmp_path, max_entries=3)
    for index in range(5):
        store.remember(
            app=f"com.example.app{index}",
            note=f"第{index}个经验",
            ts=1_000.0 + index,
        )
    assert [entry.app for entry in store.entries()] == [
        "com.example.app4",
        "com.example.app3",
        "com.example.app2",
    ]
    assert len(_read_payload(tmp_path)["entries"]) == 3
    assert store.stats()["entries"] == 3

    # 淘汰依据是 updated_at 而不是写入顺序
    second = AppMemoryStore(tmp_path / "second", max_entries=2)
    second.remember(app="com.example.a", note="a", ts=300.0)
    second.remember(app="com.example.b", note="b", ts=100.0)
    second.remember(app="com.example.c", note="c", ts=200.0)
    assert {entry.app for entry in second.entries()} == {
        "com.example.a",
        "com.example.c",
    }


def test_lookup_accumulates_hits_and_persists(tmp_path):
    """查询命中累加 hits，并落盘供重载后继续累计。"""
    store = AppMemoryStore(tmp_path)
    store.remember(app="抖音", note="经验", ts=1_000.0)
    assert store.entries()[0].hits == 0
    assert store.lookup("抖音")[0].hits == 1
    assert store.lookup("抖音")[0].hits == 2
    assert store.entries()[0].hits == 2
    reloaded = AppMemoryStore(tmp_path)
    assert reloaded.entries()[0].hits == 2
    assert reloaded.lookup("抖音")[0].hits == 3


def test_forget_removes_app_or_page(tmp_path):
    """forget 支持按 App 全删与按页面精确删除。"""
    store = AppMemoryStore(tmp_path)
    store.remember(app="抖音", note="通用", ts=1.0)
    store.remember(app="抖音", note="首页", page="首页", ts=2.0)
    store.remember(app="微信", note="微信经验", ts=3.0)

    assert store.forget("dy", page="首页") == 1
    assert [entry.page for entry in store.lookup("抖音", limit=5)] == [""]
    assert store.forget("抖音") == 1
    assert store.lookup("抖音") == []
    assert store.forget("不存在") == 0
    assert store.forget("微信") == 1
    assert store.entries() == []
    assert _read_payload(tmp_path)["entries"] == []


def test_unknown_app_can_be_remembered_with_label_hint(tmp_path):
    """无法解析包名的 App 也可记录，key 为去空白小写输入、label 回退调用方值。"""
    store = AppMemoryStore(tmp_path)
    entry = store.remember(
        app="  我的记账本  ", note="记一笔在首页右下角", label="记账本", ts=1.0
    )
    assert entry.app == "我的记账本"
    assert entry.label == "记账本"
    assert store.lookup("我的记账本")[0].note == "记一笔在首页右下角"

    package_entry = store.remember(
        app="com.example.memorytest",
        note="首页左上角菜单",
        label="记忆测试",
        ts=2.0,
    )
    assert package_entry.app == "com.example.memorytest"
    assert package_entry.label == "记忆测试"
    assert store.lookup("COM.EXAMPLE.MEMORYTEST")[0].app == "com.example.memorytest"
    assert store.stats()["apps"] == 2


def test_render_app_includes_page_time_hits_and_steps(tmp_path):
    """render_app 渲染页面、更新时间、使用次数与步骤；支持截断。"""
    store = AppMemoryStore(tmp_path)
    assert store.render_app("抖音") == ""
    store.remember(
        app="抖音",
        note="右下角 + 号进入发布页",
        page="发布",
        steps=["点击 + 号", "选择相册"],
        ts=1_760_000_000.0,
    )
    store.lookup("抖音")

    text = store.render_app("抖音")
    assert text.startswith("【抖音 使用经验】")
    assert "（发布）" in text
    assert re.search(r"（\d{2}-\d{2} \d{2}:\d{2}，已用 1 次）", text)
    assert "右下角 + 号进入发布页" in text
    assert "步骤：点击 + 号；选择相册" in text

    assert store.render_app("抖音", limit=0) == ""
    clipped = store.render_app("抖音", max_chars=20)
    assert len(clipped) <= 20
    assert clipped.endswith("…")

    store.remember(app="微信", note="通用经验", ts=1.0)
    assert "（通用）" in store.render_app("微信")


def test_render_for_prompt_groups_apps_and_truncates(tmp_path):
    """提示词摘要按最近 App 分组、每个 App 最多 2 条、总量截断。"""
    store = AppMemoryStore(tmp_path)
    assert store.render_for_prompt() == ""
    store.remember(
        app="抖音",
        note="发布页在右下角",
        page="发布",
        steps=["点击 + 号", "选择相册"],
        ts=1_000.0,
    )
    store.remember(app="抖音", note="消息在顶部", page="消息", ts=2_000.0)
    store.remember(app="抖音", note="第三条经验", page="我的", ts=3_000.0)
    store.remember(app="微信", note="发现页在「我」", ts=4_000.0)
    store.remember(app="哔哩哔哩", note="首页推荐在上方", ts=5_000.0)

    text = store.render_for_prompt()
    assert text.startswith("【App 使用经验】")
    assert "抖音" in text and "微信" in text and "哔哩哔哩" in text
    # 每个 App 最多 2 条：抖音 3 条只渲染最近 2 条
    assert text.count("抖音") == 2
    assert "发布页在右下角" not in text
    assert "消息在顶部" in text
    assert "第三条经验" in text

    # 只渲染最近 2 个 App（哔哩哔哩、微信）
    limited = store.render_for_prompt(limit=2)
    assert "哔哩哔哩" in limited and "微信" in limited
    assert "抖音" not in limited

    # 总量截断
    clipped = store.render_for_prompt(max_chars=40)
    assert len(clipped) <= 40
    assert clipped.endswith("…")


def test_entry_as_dict_and_frozen(tmp_path):
    """AppMemoryEntry.as_dict 返回 JSON 结构，且不可变。"""
    store = AppMemoryStore(tmp_path)
    entry = store.remember(app="抖音", note="经验", steps=["第一步"], ts=1.0)
    assert isinstance(entry, AppMemoryEntry)
    data = entry.as_dict()
    assert set(data) == {
        "app",
        "label",
        "page",
        "note",
        "steps",
        "updated_at",
        "hits",
        "version",
    }
    assert data["steps"] == ["第一步"]
    assert data["app"] == "com.ss.android.ugc.aweme"
    with pytest.raises(FrozenInstanceError):
        entry.note = "改"  # type: ignore[misc]


def test_stats_and_defaults(tmp_path):
    """stats 返回条目数 / App 数 / 容量 / 路径；非法容量回退默认值。"""
    store = AppMemoryStore(tmp_path, max_entries=0)
    assert store.max_entries == DEFAULT_MAX_ENTRIES
    assert store.stats() == {
        "entries": 0,
        "apps": 0,
        "max_entries": DEFAULT_MAX_ENTRIES,
        "path": str(tmp_path / MEMORY_FILENAME),
    }


def test_corrupt_file_is_ignored(tmp_path):
    """损坏 JSON 文件不影响使用，下次写入重建为合法档案。"""
    path = tmp_path / MEMORY_FILENAME
    path.write_text("不是 JSON", encoding="utf-8")
    store = AppMemoryStore(tmp_path)
    assert store.entries() == []
    store.remember(app="抖音", note="新经验", ts=1.0)
    reloaded = AppMemoryStore(tmp_path)
    assert [entry.note for entry in reloaded.entries()] == ["新经验"]


def test_invalid_entries_are_skipped_on_load(tmp_path):
    """加载时跳过不可用条目，非法数值回落默认值。"""
    payload = {
        "version": 1,
        "entries": [
            "不是对象",
            {"app": "", "note": "没有 app"},
            {"app": "com.example.a", "note": "   "},
            {
                "app": "com.example.b",
                "page": "首页",
                "note": "有效经验",
                "updated_at": "bad",
                "hits": -3,
                "version": 0,
            },
        ],
    }
    (tmp_path / MEMORY_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    store = AppMemoryStore(tmp_path)
    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].app == "com.example.b"
    assert entries[0].updated_at == 0.0
    assert entries[0].hits == 0
    assert entries[0].version == 1


def test_storage_errors_are_swallowed(tmp_path):
    """档案路径不可写时读写异常吞掉，内存接口仍然可用。"""
    (tmp_path / MEMORY_FILENAME).mkdir()
    store = AppMemoryStore(tmp_path)
    entry = store.remember(app="抖音", note="写入失败也要返回", ts=1.0)
    assert entry.note == "写入失败也要返回"
    assert len(store.entries()) == 1
    assert store.lookup("抖音")[0].hits == 1
    assert "写入失败也要返回" in store.render_app("抖音")
    assert store.stats()["entries"] == 1
    assert store.forget("抖音") == 1
    assert store.entries() == []


def test_threaded_remembers_are_consistent(tmp_path):
    """多线程写入后内存与文件条目数一致。"""
    store = AppMemoryStore(tmp_path)

    def worker(index: int) -> None:
        for offset in range(5):
            store.remember(
                app=f"com.example.thread{index}_{offset}",
                note=f"经验{index}-{offset}",
                ts=1_000.0 + offset,
            )

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(store.entries()) == 20
    assert len(_read_payload(tmp_path)["entries"]) == 20
