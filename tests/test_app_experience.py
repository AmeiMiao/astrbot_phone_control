"""App 经验本地档案：记录/查询/渲染、自动总结待写队列与索引刷新通知。"""

import asyncio
from types import SimpleNamespace

import pytest

from core.app_experience import AppExperience
from core.app_memory import AppMemoryError, AppMemoryStore
from core.config import PhoneConfig


def setup(tmp_path, enabled=True):
    service = SimpleNamespace(
        config=PhoneConfig.from_mapping(
            {"prompt": {"app_memory_enabled": enabled}}
        ),
        app_memory=AppMemoryStore(tmp_path),
        data_dir=tmp_path,
        app_context=None,
    )
    refreshes: list[bool] = []
    service.app_recall_refresh_hook = lambda: refreshes.append(True)
    return AppExperience(service), service, refreshes


def test_local_archive_is_the_only_destination(tmp_path):
    async def run():
        repo, service, refreshes = setup(tmp_path)
        first = await repo.remember(app="B站", page="首页", note="底部导航五项")
        assert first.version == 1
        second = await repo.remember(app="哔哩哔哩", page="首页", note="更新经验")
        assert second.version == 2
        assert [e.note for e in service.app_memory.entries()] == ["更新经验"]
        found = await repo.lookup("B站")
        assert [e.note for e in found] == ["更新经验"]
        assert "更新经验" in await repo.render_app("B站")
        assert (tmp_path / "app_memory.json").is_file()
        # 每次写入后请求刷新独立索引
        assert refreshes == [True, True]

    asyncio.run(run())


def test_disabled_memory_blocks_record_and_query(tmp_path):
    async def run():
        repo, service, _refreshes = setup(tmp_path, enabled=False)
        with pytest.raises(AppMemoryError, match="未开启"):
            await repo.remember(app="B站", note="经验")
        with pytest.raises(AppMemoryError, match="未开启"):
            await repo.lookup("B站")
        assert service.app_memory.entries() == []

    asyncio.run(run())


def test_automatic_verified_experience_retries_local_queue(tmp_path):
    async def run():
        repo, service, refreshes = setup(tmp_path)

        class Context:
            async def llm_generate(self, **kwargs):
                return SimpleNamespace(
                    completion_text='{"app":"B站","page":"搜索","note":"搜索框位于顶部，输入后进入结果页","steps":["点击顶部搜索框","查看搜索结果"]}',
                    usage=None,
                )

        service.app_context = Context()

        async def resolve(event, config):
            return "model", "model"

        service._resolve_media_vision_provider_id = resolve
        service.record_internal_usage = lambda *a, **kw: None
        event = SimpleNamespace(unified_msg_origin="owner")
        repo.observe(
            event, "phone_screen", {}, "首页：顶部搜索入口，下面是推荐列表。" * 3
        )
        repo.observe(event, "phone_tap", {"text": "搜索"}, "已点击搜索")
        repo.observe(
            event, "phone_screen", {}, "搜索页：搜索输入框、历史搜索、搜索按钮。" * 3
        )
        # 模拟写入失败：队列保留并记录重试信息
        repo.remember = _failing_remember(repo.remember)
        await repo.finish(event)
        queue = repo.load_queue()
        assert len(queue) == 1 and queue[0]["attempts"] == 1
        assert not service.app_memory.entries()
        # 恢复后重试成功并触发索引刷新
        repo.remember = AppExperience.remember.__get__(repo, AppExperience)
        queue[0]["retry_at"] = 0
        repo.save_queue(queue)
        await repo.flush()
        assert repo.load_queue() == []
        entries = await repo.lookup("B站")
        assert entries[0].page == "搜索"
        assert entries[0].steps == ("点击「搜索」",)
        assert "待写=0" in repo.status_text()
        assert refreshes

    asyncio.run(run())


def _failing_remember(original):
    async def failing(**_kwargs):
        raise RuntimeError("磁盘写入失败")

    return failing


def test_failed_or_unobserved_actions_do_not_create_experience(tmp_path):
    async def run():
        repo, service, refreshes = setup(tmp_path)
        event = SimpleNamespace(unified_msg_origin="owner")
        repo.observe(event, "phone_tap", {"text": "搜索"}, "error: 未找到")
        repo.observe(
            event,
            "phone_screen",
            {},
            "页面文字足够长但没有成功的操作，不构成经验。" * 3,
        )
        await repo.finish(event)
        assert not repo.load_queue()
        assert service.app_memory.entries() == []
        assert refreshes == []

    asyncio.run(run())
