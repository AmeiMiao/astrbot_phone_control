"""core.chat_memory 单元测试：记录过滤、单会话上限、全局上限与会话列表。"""

from __future__ import annotations

import time

from core.chat_memory import ChatMemory


def _record(memory: ChatMemory, umo: str, text: str, **kwargs):
    return memory.record(
        umo=umo,
        text=text,
        session_id=kwargs.get("session_id", "sid"),
        platform=kwargs.get("platform", "aiocqhttp"),
        sender_id=kwargs.get("sender_id", "1001"),
        sender_name=kwargs.get("sender_name", "小明"),
        is_group=kwargs.get("is_group", False),
        self_id=kwargs.get("self_id", ""),
        ts=kwargs.get("ts"),
    )


def test_record_filters_empty_command_and_bot(tmp_path):
    memory = ChatMemory(tmp_path)
    assert _record(memory, "umo:a", "") is None
    assert _record(memory, "umo:a", "   ") is None
    assert _record(memory, "umo:a", "/phone status") is None
    assert _record(memory, "umo:a", "hi", sender_id="bot", self_id="bot") is None
    entry = _record(memory, "umo:a", "你好呀")
    assert entry is not None
    assert entry["text"] == "你好呀"
    assert memory.stats()["entries"] == 1


def test_recent_returns_session_messages_in_order(tmp_path):
    memory = ChatMemory(tmp_path)
    base = time.time()
    _record(memory, "umo:a", "第一条", ts=base)
    _record(memory, "umo:a", "第二条", ts=base + 1)
    _record(memory, "umo:b", "别的会话", ts=base + 2)
    recent = memory.recent("umo:a", limit=5)
    assert [entry["text"] for entry in recent] == ["第一条", "第二条"]
    assert memory.recent("umo:a", limit=1)[0]["text"] == "第二条"
    assert memory.recent("umo:none") == []


def test_sessions_list_latest_message(tmp_path):
    memory = ChatMemory(tmp_path)
    base = time.time()
    _record(memory, "umo:a", "A1", ts=base)
    _record(memory, "umo:a", "A2", ts=base + 2)
    _record(memory, "umo:group", "群消息", is_group=True, ts=base + 5)
    sessions = memory.sessions(limit=5)
    assert [entry["umo"] for entry in sessions] == ["umo:group", "umo:a"]
    assert sessions[0]["text"] == "群消息"
    assert sessions[0]["is_group"] is True


def test_per_session_limit(tmp_path):
    memory = ChatMemory(tmp_path, per_session=3)
    for index in range(5):
        _record(memory, "umo:a", f"消息{index}", ts=100 + index)
    recent = memory.recent("umo:a", limit=10)
    assert [entry["text"] for entry in recent] == ["消息2", "消息3", "消息4"]


def test_global_limit(tmp_path):
    memory = ChatMemory(tmp_path, max_entries=100)
    for index in range(120):
        _record(memory, f"umo:{index}", f"消息{index}", ts=100 + index)
    stats = memory.stats()
    assert stats["entries"] == 100
    assert memory.recent("umo:119", limit=5)[0]["text"] == "消息119"
    assert memory.recent("umo:0", limit=5) == []


def test_session_summaries_and_last_ts(tmp_path):
    memory = ChatMemory(tmp_path)
    base = time.time()
    _record(memory, "umo:group", "群第一条", is_group=True, ts=base)
    _record(memory, "umo:group", "群第二条", is_group=True, ts=base + 5)
    _record(memory, "umo:user", "私聊消息", sender_name="小红", ts=base + 10)
    summaries = memory.session_summaries(limit=10)
    assert [item["umo"] for item in summaries] == ["umo:user", "umo:group"]
    group = summaries[1]
    assert group["is_group"] is True
    assert group["last_text"] == "群第二条"
    assert group["count"] == 2
    assert memory.last_ts("umo:group") == base + 5
    assert memory.last_ts("umo:none") == 0
    assert memory.is_known_session("umo:user") is True
    assert memory.is_known_session("umo:none") is False
    assert memory.session_summaries(limit=0) == []
