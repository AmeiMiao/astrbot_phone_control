"""真实 AstrBot 下的数据迁移入口、写入隔离及运行缓存回归。"""

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

astrbot = pytest.importorskip("astrbot")
if getattr(astrbot, "_acat_local_stub", False):
    pytest.skip("本地无真实 AstrBot", allow_module_level=True)

helper = importlib.import_module("test_main_config_hot_apply")
plugin_main = helper.plugin_main


def setup(tmp_path, monkeypatch):
    parent = tmp_path / "plugin_data"
    parent.mkdir()
    target = parent / plugin_main.PLUGIN_NAME
    p = helper._make_plugin(target, monkeypatch)
    p.raw_config["auto"].update(
        enabled=False, random_enabled=False, continuous_enabled=False
    )
    p.raw_config["wireguard"]["enabled"] = False
    p.raw_config["traffic"]["enabled"] = False
    source = parent / "astrbot_phone_control"
    source.mkdir()
    (source / "learning.jsonl").write_text(
        json.dumps(
            {
                "id": "source",
                "ts": 1,
                "content": "source knowledge",
                "tags": [],
                "source": "test",
            }
        )
        + "\n"
    )
    (source / "app_memory.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "app": "example.app",
                        "note": "source app experience",
                        "page": "",
                        "updated_at": 1,
                    }
                ],
            }
        )
    )
    (source / "novelty.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "key": "topic:source",
                        "label": "source",
                        "fatigue": 20,
                        "hits": 1,
                        "last_seen": 1,
                    }
                ],
            }
        )
    )
    (source / "metrics.jsonl").write_text(
        json.dumps({"kind": "screenshot", "ts": 1, "ms": 100}) + "\n"
    )
    monkeypatch.setattr(
        p.context, "get_registered_star", lambda name: None, raising=False
    )
    monkeypatch.setattr(plugin_main, "json_response", lambda data=None, **kw: data)
    monkeypatch.setattr(
        plugin_main, "error_response", lambda message, **kw: {"error": message, **kw}
    )
    request(monkeypatch)
    return p, source


def request(monkeypatch, body=None, keys="false"):
    class Request:
        query = {"include_keys": keys}

        async def json(self):
            return body

    monkeypatch.setattr(plugin_main, "request", Request())


def test_routes_and_real_runtime_caches_and_repeat(tmp_path, monkeypatch):
    p, source = setup(tmp_path, monkeypatch)
    original = {f.name: f.read_bytes() for f in source.iterdir()}

    async def run():
        try:
            routes = p.context.web_routes
            prefix = f"/{plugin_main.PLUGIN_NAME}/dashboard/data-migration"
            assert (prefix, ("GET",)) in routes and (prefix, ("POST",)) in routes
            preview = await p.dashboard_data_migration_preview()
            assert preview["count"] == 4
            request(monkeypatch, {"token": preview["token"], "include_keys": False})
            imported = await p.dashboard_data_migration_apply()
            assert imported["count"] == 4, imported
            assert p.service.learning._entries[0]["content"] == "source knowledge"
            assert p.app_memory._entries[0].note == "source app experience"
            assert p.service.novelty._entries[0].key == "topic:source"
            assert p.telemetry._entries[0]["ms"] == 100
            assert (
                p.service.telemetry is p.telemetry
                and p.service.app_memory is p.app_memory
            )
            assert (await p.dashboard_data_migration_preview())["count"] == 0
            # 缓存下一次写入必须保留已导入记录。
            p.telemetry.record_screenshot(ms=200, source="adb")
            assert len((p.data_dir / "metrics.jsonl").read_text().splitlines()) == 2
            assert {f.name: f.read_bytes() for f in source.iterdir()} == original
        finally:
            await p.terminate()

    asyncio.run(run())


@pytest.mark.parametrize(
    "condition",
    [
        "source_active",
        "lookup_missing",
        "auto",
        "wireguard",
        "traffic",
        "busy",
        "agent",
    ],
)
def test_running_writer_guards_preserve_target(tmp_path, monkeypatch, condition):
    p, source = setup(tmp_path, monkeypatch)

    async def run():
        try:
            plan = await p.dashboard_data_migration_preview()
            if condition == "source_active":
                monkeypatch.setattr(
                    p.context,
                    "get_registered_star",
                    lambda name: SimpleNamespace(activated=True),
                )
            elif condition == "lookup_missing":
                monkeypatch.setattr(p.context, "get_registered_star", None)
            elif condition in {"auto", "wireguard", "traffic"}:
                p.raw_config[condition]["enabled"] = True
            elif condition == "busy":
                p.device_gate._lease = object()
            else:
                p._active_agent_tasks["test"] = (asyncio.current_task(), "", "")
            before = {
                str(f.relative_to(p.data_dir)): f.read_bytes()
                for f in p.data_dir.rglob("*")
                if f.is_file()
            }
            request(monkeypatch, {"token": plan["token"]})
            result = await p.dashboard_data_migration_apply()
            assert result["status_code"] == 400 and "error" in result, result
            after = {
                str(f.relative_to(p.data_dir)): f.read_bytes()
                for f in p.data_dir.rglob("*")
                if f.is_file()
            }
            assert before == after
        finally:
            p.device_gate._lease = None
            p._active_agent_tasks.clear()
            await p.terminate()

    asyncio.run(run())


def test_lock_wait_rechecks_busy_before_writing(tmp_path, monkeypatch):
    p, source = setup(tmp_path, monkeypatch)

    async def run():
        try:
            plan = await p.dashboard_data_migration_preview()
            request(monkeypatch, {"token": plan["token"]})
            await p.diary._lock.acquire()
            apply = asyncio.create_task(p.dashboard_data_migration_apply())
            await asyncio.sleep(0)
            p.device_gate._lease = object()
            p.diary._lock.release()
            result = await apply
            assert result["status_code"] == 400
            assert not (p.data_dir / "learning.jsonl").exists()
        finally:
            p.device_gate._lease = None
            await p.terminate()

    asyncio.run(run())


@pytest.mark.parametrize(
    "body", [None, [], {}, {"token": 3}, {"token": "x", "include_keys": "true"}]
)
def test_bad_request_is_visible_error(tmp_path, monkeypatch, body):
    p, source = setup(tmp_path, monkeypatch)

    async def run():
        try:
            request(monkeypatch, body)
            result = await p.dashboard_data_migration_apply()
            assert result["status_code"] == 400
            assert not (p.data_dir / "learning.jsonl").exists()
        finally:
            await p.terminate()

    asyncio.run(run())
