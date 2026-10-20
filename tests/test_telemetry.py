"""core.telemetry 单元测试：记录、环形上限、序列聚合与最新值。"""

from __future__ import annotations

import json
import time

from core.telemetry import BUCKET_SECONDS, MetricsStore


def test_record_and_latest(tmp_path):
    store = MetricsStore(tmp_path)
    now = time.time()
    shot = store.record_screenshot(ms=512.3, source="scrcpy", method="jpeg", ts=now)
    assert shot["ms"] == 512.3
    llm = store.record_llm(tokens=1500, ms=2000, session="s1", ts=now)
    assert llm["tps"] == 750.0
    latest_shot = store.latest_screenshot()
    assert latest_shot["source"] == "scrcpy" and latest_shot["method"] == "jpeg"
    assert "kind" not in latest_shot
    latest_llm = store.latest_llm()
    assert latest_llm["tokens"] == 1500 and latest_llm["tps"] == 750.0
    assert "kind" not in latest_llm


def test_recent_series_window_and_buckets(tmp_path):
    store = MetricsStore(tmp_path)
    now = 1_000_000.0
    store.record_screenshot(ms=100, source="adb", ts=now - 5)
    store.record_screenshot(ms=200, source="scrcpy", ts=now - 3600)
    store.record_llm(tokens=100, ms=1000, ts=now - 25)
    store.record_llm(tokens=300, ms=1000, ts=now - 22)
    store.record_llm(tokens=50, ms=500, ts=now - 3600)
    series = store.recent_series(1, now=now)
    assert series["minutes"] == 1
    assert len(series["screenshots"]) == 1
    assert series["screenshots"][0]["source"] == "adb"
    assert len(series["llm"]) == 2
    assert len(series["buckets"]) == 1
    bucket = series["buckets"][0]
    assert bucket["tokens"] == 400
    # 桶内两次调用各 1000ms → tps = 400 tok / 2s = 200 tok/s（按时长加权）
    assert bucket["tps"] == 200.0
    assert bucket["ms"] == 2000.0
    assert bucket["ts"] % BUCKET_SECONDS == 0


def test_series_caps(tmp_path):
    store = MetricsStore(tmp_path)
    now = time.time()
    for index in range(700):
        store.record_screenshot(ms=1 + index, source="adb", ts=now - 0.01 * index)
    for index in range(700):
        store.record_llm(tokens=10, ms=100, ts=now - 0.01 * index)
    series = store.recent_series(30, now=now)
    assert len(series["screenshots"]) == 600
    assert len(series["llm"]) == 600
    assert len(series["buckets"]) <= 360


def test_ring_limit_and_file_rewrite(tmp_path):
    store = MetricsStore(tmp_path, max_entries=100)
    now = time.time()
    for index in range(150):
        store.record_screenshot(ms=index, source="adb", ts=now + index)
    assert len(store.entries()) == 100
    lines = [
        line for line in store.path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert len(lines) == 100
    first = json.loads(lines[0])
    assert first["ts"] == now + 50


def test_reload_from_disk(tmp_path):
    store = MetricsStore(tmp_path, max_entries=100)
    store.record_screenshot(ms=12, source="adb", ts=100.0)
    store.record_llm(tokens=20, ms=200, ts=101.0)
    reloaded = MetricsStore(tmp_path, max_entries=100)
    assert reloaded.latest_screenshot()["ms"] == 12
    assert reloaded.latest_llm()["tokens"] == 20
    stats = reloaded.stats()
    assert stats["entries"] == 2 and stats["screenshots"] == 1 and stats["llm"] == 1


def test_invalid_records_ignored(tmp_path):
    path = tmp_path / "metrics.jsonl"
    path.write_text(
        "not-json\n"
        + json.dumps({"kind": "unknown", "ts": 1})
        + "\n"
        + json.dumps({"kind": "screenshot", "ts": "bad"})
        + "\n",
        encoding="utf-8",
    )
    store = MetricsStore(tmp_path)
    assert store.entries() == []
