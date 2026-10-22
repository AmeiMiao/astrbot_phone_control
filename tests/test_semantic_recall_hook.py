"""聊天语义找回注入 hook 的容器级测试（需真实 AstrBot；本地自动跳过）。

覆盖：普通私聊注入临时内容块、同轮去重、未启用/未授权/任务/群聊/命令不注入、
Embedding Provider 缺失降级、Reader 异常不影响对话、未配置 ADB（假开启）仍可用、
索引维护任务在假开启状态下启动并在停用时取消、学习写入触发后台刷新。

复用 ``tests/test_main_0130_features.py`` 的 FakeContext/FakeEvent 模式：用假事件与
假 Context 直接调用 ``main.py`` 的 hook，不连接真实设备。
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_astrbot = pytest.importorskip("astrbot")
if getattr(_astrbot, "_acat_local_stub", False):
    pytest.skip("本地无真实 AstrBot，跳过容器级用例", allow_module_level=True)


def _load_plugin_main():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "astrbot_phone_control_semantic_recall_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.main", plugin_dir / "main.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin_main = _load_plugin_main()
PhoneConfig = plugin_main.PhoneConfig
RECALL_HEADER = "【相关记忆（按当前聊天语义找回）】"


class FakeEmbeddingProvider:
    """最小 Embedding Provider（按文本返回固定向量）。"""

    provider_config = {"id": "fake-embed", "embedding_model": "fake-model"}

    def __init__(self, dim: int = 3, vectors: dict[str, list[float]] | None = None) -> None:
        self.dim = dim
        self.vectors = vectors or {}
        self.calls: list[str] = []

    def get_dim(self) -> int:
        return self.dim

    def get_model(self) -> str:
        return "fake-model"

    async def get_embedding(self, text: str) -> list[float]:
        self.calls.append(text)
        return list(self.vectors.get(text, [1.0] * self.dim))

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        return [await self.get_embedding(text) for text in texts]


class FakeContext:
    """最小 Star Context：装配需要 + Embedding Provider 查询。"""

    cron_manager = None

    def __init__(self, providers: list[FakeEmbeddingProvider] | None = None) -> None:
        self.tools: list = []
        self.web_routes: list[tuple[str, tuple[str, ...]]] = []
        self._providers = list(providers or [])

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)

    def register_web_api(self, route, handler, methods=None, desc=None) -> None:
        self.web_routes.append((str(route), tuple(methods or [])))

    def get_all_embedding_providers(self):
        return list(self._providers)

    def get_provider_by_id(self, provider_id: str):
        for provider in self._providers:
            if provider.provider_config.get("id") == provider_id:
                return provider
        return None


class FakeEvent:
    """最小事件：覆盖权限/任务/消息类型判定与 extra 读写。"""

    def __init__(
        self,
        *,
        umo: str = "webchat:FriendMessage:1",
        text: str = "",
        platform: str = "webchat",
        role: str = "admin",
        payload: dict | None = None,
        sender_id: str = "1",
        message_type: str = "FRIEND_MESSAGE",
    ) -> None:
        self.unified_msg_origin = umo
        self._text = text
        self._platform = platform
        self.role = role
        self._payload = payload or {}
        self._extras: dict = {}
        self._sender_id = sender_id
        self._message_type = message_type

    def get_platform_name(self):
        return self._platform

    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "tester"

    def get_self_id(self):
        return "bot"

    def get_message_type(self):
        return types.SimpleNamespace(name=self._message_type)

    def get_session_id(self):
        return "1"

    def get_extra(self, key, default=None):
        if key == "cron_payload":
            return self._payload.get("cron_payload", default)
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra_key(self, key, default=None):
        return self.get_extra(key, default)


class FakeReader:
    """记录查询文本的语义找回 Reader 替身（可注入异常）。"""

    def __init__(self, section: str = RECALL_HEADER + "\n- [学习｜] 测试记忆", error: Exception | None = None) -> None:
        self.section = section
        self.error = error
        self.queries: list[str] = []

    async def render(self, query: str) -> str:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.section


def _raw_config(*, recall_enabled: bool = True) -> dict:
    """默认配置 + 仅保留「假开启」（ADR 未配置）与 recall 开关。"""
    raw = dataclasses.asdict(PhoneConfig.from_mapping(None))
    raw["adb"] = {
        "host": "",
        "port": 0,
        "path": "",
        "serial": "",
        "connect_timeout": 2,
        "command_timeout": 3,
    }
    raw["recall"]["enabled"] = recall_enabled
    return raw


def _make_plugin(tmp_path, monkeypatch, *, recall_enabled: bool = True, providers=None):
    monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
    context = FakeContext(providers)
    plugin = plugin_main.PhoneControlPlugin(context, _raw_config(recall_enabled=recall_enabled))
    monkeypatch.setattr(
        plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
    )
    return plugin


async def _cleanup(plugin) -> None:
    try:
        await plugin.terminate()
    except Exception:  # noqa: BLE001 - 清理失败不影响断言
        pass


def _req() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        system_prompt="系统提示词",
        prompt="",
        contexts=[],
        extra_user_content_parts=[],
    )


def _joined_parts(req) -> str:
    return "\n".join(
        str(getattr(part, "text", part)) for part in req.extra_user_content_parts
    )


# ----------------------------------------------------------------------
# 注入主路径
# ----------------------------------------------------------------------


def test_hook_injects_temp_part_for_private_chat_without_adb(tmp_path, monkeypatch):
    """普通私聊注入临时 part；ADB 未配置（假开启）时同样可用且无设备副作用。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch, recall_enabled=True)
        reader = FakeReader()
        plugin.semantic_reader = reader
        try:
            assert plugin.service.config.adb.configured is False
            event = FakeEvent(text="还记得之前B站转正答题的事吗")
            req = _req()
            await plugin.inject_semantic_recall(event, req)
            assert reader.queries == ["还记得之前B站转正答题的事吗"]
            assert len(req.extra_user_content_parts) == 1
            part = req.extra_user_content_parts[0]
            assert getattr(part, "text", "") == reader.section
            # mark_as_temp：只给本轮模型看，不写入会话历史
            assert getattr(part, "_no_save", False) is True
            # 不写 system_prompt（保护前缀缓存）
            assert req.system_prompt == "系统提示词"
            # 普通聊天只读记忆：不登记任务、不预热 scrcpy/ADB、不启动后台循环
            assert getattr(plugin, "_scrcpy_warmup_task", None) is None
            assert getattr(plugin, "_random_task", None) is None
            assert getattr(plugin, "_continuous_task", None) is None
            assert getattr(plugin, "_traffic_keepalive_task", None) is None
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_hook_does_not_duplicate_injection_in_same_request(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch, recall_enabled=True)
        reader = FakeReader()
        plugin.semantic_reader = reader
        try:
            event = FakeEvent(text="B站转正答题")
            req = _req()
            await plugin.inject_semantic_recall(event, req)
            await plugin.inject_semantic_recall(event, req)
            assert len(req.extra_user_content_parts) == 1
            assert reader.queries == ["B站转正答题"]
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_hook_skips_when_disabled_or_unauthorized(tmp_path, monkeypatch):
    async def scenario() -> None:
        disabled = _make_plugin(tmp_path, monkeypatch, recall_enabled=False)
        disabled_reader = FakeReader()
        disabled.semantic_reader = disabled_reader
        try:
            req = _req()
            await disabled.inject_semantic_recall(FakeEvent(text="随便聊聊"), req)
            assert req.extra_user_content_parts == []
            assert disabled_reader.queries == []
        finally:
            await _cleanup(disabled)

        plugin = _make_plugin(tmp_path / "second", monkeypatch, recall_enabled=True)
        reader = FakeReader()
        plugin.semantic_reader = reader
        try:
            # 普通成员且不在白名单：不注入（手机记忆仅对授权用户开放）
            member = FakeEvent(text="B站转正答题", role="member", sender_id="999")
            req = _req()
            await plugin.inject_semantic_recall(member, req)
            assert req.extra_user_content_parts == []
            assert reader.queries == []
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_hook_skips_task_cron_group_and_command(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch, recall_enabled=True)
        reader = FakeReader()
        plugin.semantic_reader = reader
        try:
            variants = [
                FakeEvent(text="B站转正答题", platform="cron"),
                FakeEvent(
                    text="B站转正答题",
                    payload={"cron_payload": {"origin": "phone_play", "play_task": True}},
                ),
                FakeEvent(text="B站转正答题", message_type="GROUP_MESSAGE"),
                FakeEvent(text="/phone status"),
            ]
            for event in variants:
                req = _req()
                await plugin.inject_semantic_recall(event, req)
                assert req.extra_user_content_parts == []
            assert reader.queries == []
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_hook_uses_request_prompt_when_message_text_empty(tmp_path, monkeypatch):
    """语音/图片消息没有文本时，回退到本轮请求文本作为查询。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch, recall_enabled=True)
        reader = FakeReader()
        plugin.semantic_reader = reader
        try:
            event = FakeEvent(text="")
            req = _req()
            req.prompt = "帮我回忆一下B站转正答题"
            await plugin.inject_semantic_recall(event, req)
            assert reader.queries == ["帮我回忆一下B站转正答题"]
            assert len(req.extra_user_content_parts) == 1
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 失败降级
# ----------------------------------------------------------------------


def test_hook_degrades_when_embedding_provider_missing(tmp_path, monkeypatch):
    """没有 Embedding Provider 时不注入、不抛异常（真实 Reader 降级）。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch, recall_enabled=True, providers=[])
        try:
            from core.semantic_recall import SemanticReader

            plugin.semantic_reader = SemanticReader(
                plugin.semantic_index,
                plugin.semantic_resolver,
                lambda: plugin.service.config.recall,
            )
            req = _req()
            await plugin.inject_semantic_recall(FakeEvent(text="B站转正答题"), req)
            assert req.extra_user_content_parts == []
            assert req.system_prompt == "系统提示词"
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_hook_swallows_reader_errors(tmp_path, monkeypatch):
    """Reader 异常/超时不得影响对话：hook 静默返回。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch, recall_enabled=True)
        plugin.semantic_reader = FakeReader(error=RuntimeError("向量服务异常"))
        try:
            req = _req()
            await plugin.inject_semantic_recall(FakeEvent(text="B站转正答题"), req)
            assert req.extra_user_content_parts == []
            assert req.system_prompt == "系统提示词"
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 端到端（真实 Reader + 本地索引）
# ----------------------------------------------------------------------


def test_hook_injects_from_local_index_in_fake_enabled_state(tmp_path, monkeypatch):
    """建好本地索引后，普通聊天可召回注入；全程不需要 ADB 配置。"""

    async def scenario() -> None:
        provider = FakeEmbeddingProvider(dim=3, vectors={})
        plugin = _make_plugin(
            tmp_path, monkeypatch, recall_enabled=True, providers=[provider]
        )
        try:
            assert plugin.service.config.adb.configured is False
            plugin.data_dir.mkdir(parents=True, exist_ok=True)
            (plugin.data_dir / "learning.jsonl").write_text(
                json.dumps(
                    {
                        "id": "learn-1",
                        "ts": 1_760_000_000.0,
                        "content": "B站转正答题要 60 分",
                        "source": "哔哩哔哩",
                        "tags": [],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            assert await plugin.semantic_maintainer.refresh() is True
            assert plugin.adb.configured is False

            req = _req()
            await plugin.inject_semantic_recall(
                FakeEvent(text="B站转正答题要多少分"), req
            )
            injected = _joined_parts(req)
            assert injected.startswith(RECALL_HEADER)
            assert "B站转正答题要 60 分" in injected
            assert req.system_prompt == "系统提示词"
            # 查询文本只用于 embedding 查询
            assert provider.calls[-1] == "B站转正答题要多少分"
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 后台索引维护任务生命周期
# ----------------------------------------------------------------------


def test_maintenance_task_starts_and_stops_in_fake_enabled_state(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch, recall_enabled=True)
        try:
            await plugin.initialize()
            task = plugin._semantic_task
            assert task is not None
            assert not task.done()
            assert task.get_name().startswith("astrbot_phone_control")
            # 假开启：设备相关后台任务仍全部未启动
            assert getattr(plugin, "_auto_config_task", None) is None
            assert getattr(plugin, "_random_task", None) is None
            assert getattr(plugin, "_continuous_task", None) is None
        finally:
            await _cleanup(plugin)
        assert plugin._semantic_task is None

    asyncio.run(scenario())


def test_maintenance_not_started_when_recall_disabled(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch, recall_enabled=False)
        try:
            await plugin.initialize()
            assert plugin._semantic_task is None
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_learning_write_requests_semantic_refresh(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch, recall_enabled=True)
        try:
            assert callable(plugin.service.semantic_refresh_hook)
            plugin.semantic_maintainer._wake.clear()
            await plugin.service.learn("测试学习内容")
            assert plugin.semantic_maintainer._wake.is_set()
            # 直接通过 service 请求也只会置位事件（不做 IO）
            plugin.semantic_maintainer._wake.clear()
            plugin.service.request_semantic_refresh()
            assert plugin.semantic_maintainer._wake.is_set()
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())
