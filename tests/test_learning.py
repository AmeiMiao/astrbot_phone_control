"""core.learning 单元测试：JSONL 写入、校验、环形上限、检索与提示词渲染。"""

from __future__ import annotations

import json
import re

import pytest

from core.learning import (
    DEFAULT_MAX_ENTRIES,
    LEARNING_FILENAME,
    LearningError,
    LearningStore,
)


def _write_lines(path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def test_record_appends_and_normalizes(tmp_path):
    store = LearningStore(tmp_path)
    record = store.record(
        content="  今天   学会了\nPython 装饰器  ",
        source=" 哔哩哔哩 ",
        tags=[" AI ", "", "学习", "AI"],
        ts=1_760_000_000.0,
    )
    assert len(record["id"]) == 32
    assert record == {
        "id": record["id"],
        "ts": 1_760_000_000.0,
        "content": "今天 学会了 Python 装饰器",
        "source": "哔哩哔哩",
        "tags": ["AI", "学习"],
    }
    path = tmp_path / LEARNING_FILENAME
    assert path.exists()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


def test_record_rejects_invalid_content(tmp_path):
    store = LearningStore(tmp_path)
    for bad in ("", "   ", "\n\t", None):
        with pytest.raises(LearningError):
            store.record(content=bad)
    with pytest.raises(LearningError):
        store.record(content="字" * 501)
    assert store.entries() == []
    # 边界值 1 与 500 字合法
    assert store.record(content="字")["content"] == "字"
    assert store.record(content="字" * 500)["content"] == "字" * 500


def test_tags_are_split_deduped_and_capped(tmp_path):
    store = LearningStore(tmp_path)
    assert store.record(content="一", tags="AI, 学习，AI")["tags"] == ["AI", "学习"]
    assert store.record(content="二", tags=" , ，")["tags"] == []
    assert store.record(content="三", tags=123)["tags"] == []
    long_tag = "标" * 100
    tags = store.record(content="四", tags=[long_tag])["tags"]
    assert len(tags[0]) <= 50 and tags[0].endswith("…")
    many = store.record(content="五", tags=[f"t{i}" for i in range(20)])["tags"]
    assert len(many) == 10


def test_entries_and_recent_keep_chronological_order(tmp_path):
    store = LearningStore(tmp_path)
    for index in range(3):
        store.record(content=f"第{index}条", ts=1_000.0 + index)
    assert [entry["content"] for entry in store.entries()] == ["第0条", "第1条", "第2条"]
    assert [entry["content"] for entry in store.entries(2)] == ["第1条", "第2条"]
    assert store.entries(0) == []
    assert store.entries(-1) == []
    assert [entry["content"] for entry in store.recent(2)] == ["第1条", "第2条"]
    assert store.recent(0) == []
    assert len(store.recent()) == 3


def test_recent_and_search_order_by_timestamp(tmp_path):
    store = LearningStore(tmp_path)
    store.record(content="第三条时间", ts=300.0)
    store.record(content="第一条时间", ts=100.0)
    store.record(content="第二条时间", ts=200.0)
    # entries 按写入顺序；recent/search 按时间排序
    assert [entry["content"] for entry in store.entries()] == [
        "第三条时间",
        "第一条时间",
        "第二条时间",
    ]
    assert [entry["content"] for entry in store.recent(5)] == [
        "第一条时间",
        "第二条时间",
        "第三条时间",
    ]
    assert [entry["content"] for entry in store.recent(2)] == [
        "第二条时间",
        "第三条时间",
    ]
    assert [entry["content"] for entry in store.search("时间", limit=2)] == [
        "第二条时间",
        "第三条时间",
    ]


def test_ring_limit_rewrites_file(tmp_path):
    store = LearningStore(tmp_path, max_entries=3)
    for index in range(5):
        store.record(content=f"第{index}条", ts=1_000.0 + index)
    entries = store.entries()
    assert [entry["content"] for entry in entries] == ["第2条", "第3条", "第4条"]
    path = tmp_path / LEARNING_FILENAME
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 3
    assert [json.loads(line)["content"] for line in lines] == ["第2条", "第3条", "第4条"]
    stats = store.stats()
    assert stats["entries"] == 3
    assert stats["max_entries"] == 3


def test_invalid_max_entries_falls_back_to_default(tmp_path):
    for value in (0, -10, "bad", None):
        store = LearningStore(tmp_path, max_entries=value)
        assert store.max_entries == DEFAULT_MAX_ENTRIES


def test_search_matches_content_source_tags_case_insensitive(tmp_path):
    store = LearningStore(tmp_path)
    store.record(
        content="学习 Python 装饰器", source="B站", tags=["编程"], ts=1_000.0
    )
    store.record(content="看了原神攻略", source="抖音", tags=["游戏"], ts=2_000.0)
    store.record(
        content="AI 视频剪辑技巧", source="哔哩哔哩", tags=["AI"], ts=3_000.0
    )
    assert [entry["content"] for entry in store.search("python")] == [
        "学习 Python 装饰器"
    ]
    assert [entry["content"] for entry in store.search("b站")] == [
        "学习 Python 装饰器"
    ]
    assert [entry["content"] for entry in store.search("编程")] == [
        "学习 Python 装饰器"
    ]
    assert [entry["content"] for entry in store.search("ai")] == ["AI 视频剪辑技巧"]
    assert [entry["content"] for entry in store.search("原神")] == ["看了原神攻略"]
    assert store.search("不存在的内容") == []
    # 空关键词等价 recent（旧→新）
    assert [entry["content"] for entry in store.search("", limit=2)] == [
        "看了原神攻略",
        "AI 视频剪辑技巧",
    ]
    assert store.search("   ", limit=1) == store.recent(1)


def test_search_takes_latest_matches_then_returns_chronological(tmp_path):
    store = LearningStore(tmp_path)
    for index in range(5):
        store.record(content=f"共同兴趣 第{index}条", ts=1_000.0 + index)
    matched = store.search("共同兴趣", limit=2)
    assert [entry["content"] for entry in matched] == ["共同兴趣 第3条", "共同兴趣 第4条"]
    assert store.search("共同兴趣", limit=0) == []


def test_render_for_prompt_format_and_truncation(tmp_path):
    assert LearningStore(tmp_path).render_for_prompt(5) == ""
    store = LearningStore(tmp_path)
    store.record(
        content="字" * 200, source="哔哩哔哩", tags=["AI", "学习"], ts=1_760_000_000.0
    )
    text = store.render_for_prompt(5)
    assert text.startswith("【最近学到】")
    lines = text.splitlines()
    assert len(lines) == 2
    line = lines[1]
    assert re.match(r"^- （\d{2}-\d{2} \d{2}:\d{2}）\[哔哩哔哩\] ", line)
    assert line.endswith("（#AI #学习）")
    content = line.split("[哔哩哔哩] ", 1)[1].rsplit("（#", 1)[0]
    assert len(content) <= 120 and content.endswith("…")
    # 只渲染最近 N 条
    store.record(content="第二条", ts=1_760_000_100.0)
    latest = store.render_for_prompt(1)
    assert "第二条" in latest and "字" * 20 not in latest
    # 整段长度限制
    clipped = store.render_for_prompt(5, max_chars=20)
    assert len(clipped) <= 20 and clipped.endswith("…")


def test_render_omits_empty_source_and_tags(tmp_path):
    store = LearningStore(tmp_path)
    store.record(content="没有来源和标签", ts=1_760_000_000.0)
    text = store.render_for_prompt(5)
    assert "[]" not in text
    assert "（#" not in text
    assert "没有来源和标签" in text


def test_stats_counts_entries_and_sources(tmp_path):
    store = LearningStore(tmp_path)
    assert store.stats()["entries"] == 0
    assert store.stats()["last_ts"] == 0.0
    store.record(content="一", source="抖音", ts=1_000.0)
    store.record(content="二", source="抖音", ts=2_000.0)
    store.record(content="三", ts=3_000.0)
    stats = store.stats()
    assert stats["entries"] == 3
    assert stats["sources"] == {"抖音": 2, "未知": 1}
    assert stats["last_ts"] == 3_000.0
    assert stats["max_entries"] == DEFAULT_MAX_ENTRIES
    assert stats["path"].endswith(LEARNING_FILENAME)


def test_corrupt_lines_are_ignored(tmp_path):
    path = tmp_path / LEARNING_FILENAME
    path.write_text(
        json.dumps({"ts": 1.0, "content": "旧记录", "source": "", "tags": []})
        + "\n不是 JSON\n"
        + json.dumps({"ts": 2.0, "content": "   ", "source": "", "tags": []})
        + "\n",
        encoding="utf-8",
    )
    store = LearningStore(tmp_path)
    assert [entry["content"] for entry in store.entries()] == ["旧记录"]
    store.record(content="新记录", ts=3.0)
    assert [entry["content"] for entry in store.entries()] == ["旧记录", "新记录"]


def test_persists_across_instances_and_custom_filename(tmp_path):
    store = LearningStore(tmp_path)
    store.record(content="持久化内容", source="B站", tags=["AI"], ts=1_000.0)
    reloaded = LearningStore(tmp_path)
    assert reloaded.entries() == store.entries()
    custom = LearningStore(tmp_path, filename="other.jsonl")
    assert custom.path.name == "other.jsonl"
    custom.record(content="另一个文件", ts=2_000.0)
    assert (tmp_path / "other.jsonl").exists()
    assert len(store.entries()) == 1


def test_storage_errors_are_swallowed(tmp_path):
    # 档案路径被目录占用：写入/读取都失败，但接口不抛异常
    (tmp_path / LEARNING_FILENAME).mkdir()
    store = LearningStore(tmp_path)
    record = store.record(content="写入失败也要返回记录", ts=1.0)
    assert record["content"] == "写入失败也要返回记录"
    assert len(store.entries()) == 1
    assert store.stats()["entries"] == 1
    assert store.search("失败")[0]["content"] == "写入失败也要返回记录"
    assert "写入失败也要返回记录" in store.render_for_prompt(5)


def test_threaded_records_keep_consistent(tmp_path):
    store = LearningStore(tmp_path)
    import threading

    def worker(index: int) -> None:
        for offset in range(5):
            store.record(content=f"线程{index}-{offset}", ts=1_000.0 + offset)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(store.entries()) == 20
    path = tmp_path / LEARNING_FILENAME
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 20


def test_record_keeps_attribution_fields(tmp_path):
    store = LearningStore(tmp_path)
    record = store.record(
        content="用户让我记住的内容",
        source="B站",
        tags=["测试"],
        origin="phone_background_task",
        origin_detail="job-1",
        attribution="user",
        initiator_user_id="42",
        requested_by="42",
    )
    assert record["attribution"] == "user"
    assert record["origin"] == "phone_background_task"
    assert record["initiator_user_id"] == "42"
    assert record["requested_by"] == "42"
    # 重新加载后字段保留。
    reloaded = LearningStore(tmp_path).recent(1)[0]
    assert reloaded["attribution"] == "user"
    assert reloaded["initiator_user_id"] == "42"


def test_record_without_attribution_omits_fields(tmp_path):
    store = LearningStore(tmp_path)
    record = store.record(content="普通学习")
    assert "attribution" not in record
    assert "origin" not in record
