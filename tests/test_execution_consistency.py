"""main 级执行一致性回归：鉴权顺序、持久化 fail-closed、提示词注入隔离、设备占用。

覆盖场景：

* 鉴权先于任何抢占/中止副作用：非主人 ``/phone status`` 只收到权限拒绝，白名单用户
  的命令才登记用户任务优先；
* 验证状态持久化失败后设备操作被拒绝（fail-closed），内存中仍保持暂停，写入恢复后
  自动解除；随机玩循环在故障态停排；
* 提示词注入隔离：非授权请求零注入；普通对话只注入能力指南，不注入日记/学习/App
  经验；手机任务/手机请求才注入手机活动记忆；
* 设备占用互斥与收尾：第二个用户任务收到占用提示且不放走第一个任务的租约；任务
  结束收尾 HOME 在其他任务仍持有租约时跳过。

依赖 AstrBot（本地无 astrbot 时整体跳过）：在 AstrBot 容器内跑 ``pytest tests``
时用假 Context/事件直接验证 main.py 接线（不连接真实设备）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("astrbot")


def _load_plugin_main():
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "astrbot_phone_control_under_test_consistency"
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
verification_mod = plugin_main.verification_mod
GateError = plugin_main.GateError
# main 只导入 build_phone_tools，工具类从同一模块命名空间取（避免重复导入 core.tools）
_tools_ns = plugin_main.build_phone_tools.__globals__
PhoneScreenTool = _tools_ns["PhoneScreenTool"]


# ----------------------------------------------------------------------
# 假 Context / 事件 / 插件
# ----------------------------------------------------------------------


class FakeContext:
    cron_manager = None

    def __init__(self) -> None:
        self.tools = []

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)

    def register_web_api(self, *args, **kwargs) -> None:
        pass


class FakeEvent:
    """最小事件：覆盖命令、工具门禁与提示词注入需要的事件接口。"""

    def __init__(
        self,
        *,
        umo: str = "webchat:FriendMessage:1",
        text: str = "",
        platform: str = "webchat",
        role: str = "admin",
        payload: dict | None = None,
        sender_id: str = "1",
    ) -> None:
        self.unified_msg_origin = umo
        self._text = text
        self._platform = platform
        self.role = role
        self._payload = payload or {}
        self._extras: dict = {}
        self._sender_id = sender_id
        self.sent: list[str] = []
        self.stopped = False

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
        return types.SimpleNamespace(name="FRIEND_MESSAGE")

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

    def stop_event(self) -> None:
        self.stopped = True

    async def send(self, chain) -> bool:
        for component in getattr(chain, "chain", []) or []:
            text = getattr(component, "text", None)
            if text:
                self.sent.append(str(text))
        return True

    def sent_text(self) -> str:
        return "\n".join(self.sent)


class FakeToolContext:
    """工具调用上下文（ContextWrapper 的最小替身）。"""

    def __init__(self, event: FakeEvent) -> None:
        self.context = types.SimpleNamespace(event=event)


def _raw_config(**overrides) -> dict:
    raw = dataclasses.asdict(PhoneConfig.from_mapping(None))
    raw["adb"] = {
        "host": "127.0.0.1",
        "port": 5555,
        "path": "",
        "serial": "",
        "connect_timeout": 2,
        "command_timeout": 3,
    }

    def merge(base: dict, patch: dict) -> dict:
        result = dict(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = value
        return result

    return merge(raw, overrides)


def _make_plugin(tmp_path, monkeypatch, **overrides):
    monkeypatch.setattr(plugin_main, "_resolve_data_dir", lambda: tmp_path)
    plugin = plugin_main.PhoneControlPlugin(FakeContext(), _raw_config(**overrides))
    monkeypatch.setattr(
        plugin, "_config_file_path", lambda: tmp_path / "plugin_config.json"
    )
    return plugin


async def _cleanup(plugin) -> None:
    try:
        await plugin.terminate()
    except Exception:  # noqa: BLE001 - 清理失败不影响断言
        pass


async def _quiet_injection_hooks(monkeypatch, plugin) -> dict:
    """把注入流程里的设备/计费副作用换成计数桩，聚焦提示词内容断言。"""
    calls = {"warm_scrcpy": 0, "record_tokens": 0}
    monkeypatch.setattr(plugin, "_maybe_warm_scrcpy", lambda *a, **k: None)
    monkeypatch.setattr(plugin, "_begin_llm_metric", lambda *a, **k: None)
    monkeypatch.setattr(
        plugin,
        "_record_request_tokens",
        lambda *a, **k: calls.__setitem__("record_tokens", calls["record_tokens"] + 1),
    )
    return calls


def _seed_memory_markers(monkeypatch, plugin) -> dict[str, str]:
    """给浏览日记/学习/App 经验/新鲜感塞入可辨识标记文本。"""
    markers = {
        "diary": "【浏览日记】最近看过 B 站科技区分区",
        "learning": "【学习档案】B 站入口在底部导航第二个",
        "app_memory": "【App 使用经验】B 站底部导航依次是首页/动态/我的",
        "novelty": "【新鲜感】最近同类题材看得有点多",
    }
    monkeypatch.setattr(
        plugin.diary, "render_for_prompt", lambda *a, **k: markers["diary"]
    )
    monkeypatch.setattr(
        plugin.service.learning,
        "render_for_prompt",
        lambda *a, **k: markers["learning"],
    )
    monkeypatch.setattr(
        plugin.app_memory,
        "render_for_prompt",
        lambda *a, **k: markers["app_memory"],
    )
    monkeypatch.setattr(
        plugin.service, "novelty_prompt", lambda: markers["novelty"]
    )
    return markers


def _injection_request(prompt: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(system_prompt="", prompt=str(prompt), contexts=[])


# ----------------------------------------------------------------------
# 鉴权先于任何抢占/中止副作用
# ----------------------------------------------------------------------


def test_non_owner_phone_status_denied_without_preempt(tmp_path, monkeypatch):
    async def scenario() -> None:
        # 配置主人名单后，AstrBot 管理员也不再放行。
        plugin = _make_plugin(
            tmp_path, monkeypatch, security={"owner_ids": ["999"]}
        )
        preempt_calls: list[int] = []
        gate_preempt_calls: list[str] = []

        async def fake_preempt() -> int:
            preempt_calls.append(1)
            return 0

        monkeypatch.setattr(plugin, "_preempt_autonomous_tasks", fake_preempt)
        monkeypatch.setattr(
            plugin.device_gate,
            "preempt",
            lambda reason="user_priority": gate_preempt_calls.append(reason) or [],
        )
        event = FakeEvent(text="/phone status", role="admin", sender_id="1")
        try:
            await plugin.phone_command(event, "status")
            assert preempt_calls == []
            assert gate_preempt_calls == []
            assert not plugin.service.user_priority.sessions
            assert plugin.device_gate.busy is False
            assert plugin.device_gate.epoch == 0
            assert "仅插件主人可用" in event.sent_text()
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_help_has_no_side_effects(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        preempt_calls: list[int] = []

        async def fake_preempt() -> int:
            preempt_calls.append(1)
            return 0

        monkeypatch.setattr(plugin, "_preempt_autonomous_tasks", fake_preempt)
        monkeypatch.setattr(
            plugin.device_gate, "preempt", lambda *a, **k: preempt_calls.append(1)
        )
        # 非管理员也能看帮助，且不产生抢占/占用副作用。
        event = FakeEvent(text="/phone help", role="member")
        try:
            await plugin.phone_command(event, "help")
            assert event.sent_text() != ""
            assert "权限不足" not in event.sent_text()
            assert preempt_calls == []
            assert plugin.device_gate.busy is False
            assert not plugin.service.user_priority.sessions
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_authorized_phone_command_registers_user_priority(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        preempt_calls: list[int] = []
        gate_preempt_calls: list[str] = []

        async def fake_preempt() -> int:
            preempt_calls.append(1)
            return 0

        monkeypatch.setattr(plugin, "_preempt_autonomous_tasks", fake_preempt)
        monkeypatch.setattr(
            plugin.device_gate,
            "preempt",
            lambda reason="user_priority": gate_preempt_calls.append(reason) or [],
        )
        event = FakeEvent(text="/phone quota", role="admin", sender_id="1")
        try:
            await plugin.phone_command(event, "quota")
            # 鉴权通过后才允许登记用户任务并抢占自主任务
            assert preempt_calls == [1]
            assert gate_preempt_calls == ["user_priority"]
            assert event.sent_text() != ""
            # 命令结束后释放租约，不残留占用
            assert plugin.device_gate.busy is False
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_device_authorize_rejects_non_admin_event(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            with pytest.raises(GateError) as exc:
                plugin.device_gate.enter(
                    FakeEvent(role="member"), purpose="phone_tap"
                )
            assert "权限不足" in exc.value.message
            assert plugin.device_gate.busy is False
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 持久化失败 fail-closed，恢复后解除
# ----------------------------------------------------------------------


def test_persistence_failure_blocks_ops_and_keeps_paused_state(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        controller = plugin._verification
        try:
            # 内存中构造一个仍在等待人工协助的会话
            controller.state = verification_mod.new_session(
                device="emulator-5554",
                app="B站",
                origin="user",
                initiator_umo="webchat:FriendMessage:1",
                notify_umo="webchat:FriendMessage:1",
                max_attempts=1,
            )
            verification_mod.apply_awaiting(
                controller.state, reason="test", limits=controller._limits()
            )
            real_save = controller.store.save
            fail = {"on": True}

            def flaky_save(state):
                if fail["on"]:
                    return False
                return real_save(state)

            monkeypatch.setattr(controller.store, "save", flaky_save)
            assert controller._save() is False
            assert controller.health_reason() != ""
            assert controller.paused is True
            assert controller.state["status"] == verification_mod.STATUS_AWAITING_USER

            # 门禁拒绝一切设备操作（fail-closed）
            with pytest.raises(GateError) as exc:
                plugin.device_gate.enter(
                    FakeEvent(role="admin"), purpose="command:status"
                )
            assert "验证状态无法写入" in exc.value.message
            assert plugin.device_gate.busy is False

            # 命令路径同样被拒，且提示具体原因
            cmd_event = FakeEvent(role="admin", sender_id="1")
            await plugin.phone_command(cmd_event, "status")
            assert "验证状态无法写入" in cmd_event.sent_text()
            # 失败没有清空/改写内存状态：仍保持暂停
            assert controller.state["status"] == verification_mod.STATUS_AWAITING_USER
            assert controller.paused is True

            # 写入恢复后自动解除阻断
            fail["on"] = False
            assert controller._save() is True
            assert controller.health_reason() == ""
            assert controller.persistence_ok is True
            recovered_event = FakeEvent(role="admin")
            with plugin.device_gate.scope(
                recovered_event, purpose="command:status"
            ):
                pass
            plugin.device_gate.release_event(recovered_event)
            assert plugin.device_gate.busy is False
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_reserve_attempt_rejected_after_persistence_failure(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        controller = plugin._verification
        try:
            async def fake_foreground():
                return ("", "")

            monkeypatch.setattr(plugin.service.actions, "foreground", fake_foreground)
            monkeypatch.setattr(controller.store, "save", lambda state: False)
            event = FakeEvent(role="admin")
            ok, reason = await controller.reserve_attempt(event, kind="solver")
            assert ok is False
            assert "验证状态无法写入" in reason
            assert controller.persistence_ok is False
            # 已处于故障态：后续预占直接拒绝，不再尝试开始新会话
            ok2, reason2 = await controller.reserve_attempt(event, kind="solver")
            assert ok2 is False
            assert "验证状态无法写入" in reason2
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


class _LoopDeferred(Exception):
    """循环在本次探测中按预期让路/停排：抛出以结束被测循环。"""


def test_random_loop_stops_scheduling_on_persistence_failure(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        controller = plugin._verification
        scheduled: list = []
        try:
            async def fake_schedule(auto) -> None:
                scheduled.append(auto)

            async def fake_sleep(seconds) -> bool:
                raise _LoopDeferred()

            monkeypatch.setattr(plugin, "_schedule_random_play", fake_schedule)
            monkeypatch.setattr(plugin, "_sleep_or_stop", fake_sleep)
            monkeypatch.setattr(controller.store, "save", lambda state: False)
            assert controller._save() is False
            assert controller.health_reason() != ""
            # 持久化故障：自动循环停排（探测在退避处结束）
            with pytest.raises(_LoopDeferred):
                await plugin._random_play_loop()
            assert scheduled == []
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_random_loop_waits_while_device_busy(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        scheduled: list = []
        try:
            async def fake_schedule(auto) -> None:
                scheduled.append(auto)

            async def fake_sleep(seconds) -> bool:
                raise _LoopDeferred()

            monkeypatch.setattr(plugin, "_schedule_random_play", fake_schedule)
            monkeypatch.setattr(plugin, "_sleep_or_stop", fake_sleep)
            holder = FakeEvent(umo="webchat:FriendMessage:9", role="admin")
            plugin.device_gate.enter(holder, purpose="command:open")
            # 设备仍被其他任务占用：不排新的自动任务
            with pytest.raises(_LoopDeferred):
                await plugin._random_play_loop()
            assert scheduled == []
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 手机活动记忆按权限与任务隔离
# ----------------------------------------------------------------------


def test_unauthorized_request_injects_nothing(tmp_path, monkeypatch):
    async def scenario() -> None:
        # require_admin 默认开启，普通用户的请求不应拿到任何手机提示词。
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            calls = await _quiet_injection_hooks(monkeypatch, plugin)
            markers = _seed_memory_markers(monkeypatch, plugin)
            event = FakeEvent(role="member", text="帮我打开B站看视频")
            req = _injection_request("帮我打开B站看视频")
            await plugin.inject_phone_guidance(event, req)
            prompt = req.system_prompt
            assert "【手机操控能力】" not in prompt
            for marker in markers.values():
                assert marker not in prompt
            assert calls["record_tokens"] == 0
            assert not plugin.service.user_priority.sessions
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_plain_chat_only_injects_capability_guide(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            await _quiet_injection_hooks(monkeypatch, plugin)
            markers = _seed_memory_markers(monkeypatch, plugin)
            event = FakeEvent(role="admin", text="今天天气怎么样")
            req = _injection_request("今天天气怎么样")
            await plugin.inject_phone_guidance(event, req)
            prompt = req.system_prompt
            # 指南对授权用户始终可用
            assert "【手机操控能力】" in prompt
            # 普通对话不扩大上下文：手机活动记忆一律不注入
            for marker in markers.values():
                assert marker not in prompt
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_phone_request_injects_activity_memory(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            await _quiet_injection_hooks(monkeypatch, plugin)
            markers = _seed_memory_markers(monkeypatch, plugin)
            event = FakeEvent(
                role="admin", sender_id="1", text="帮我打开B站看两条视频"
            )
            req = _injection_request("帮我打开B站看两条视频")
            await plugin.inject_phone_guidance(event, req)
            prompt = req.system_prompt
            assert "【手机操控能力】" in prompt
            for marker in markers.values():
                assert marker in prompt
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_autonomous_task_injects_activity_memory(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        try:
            calls = await _quiet_injection_hooks(monkeypatch, plugin)
            markers = _seed_memory_markers(monkeypatch, plugin)
            event = FakeEvent(
                platform="cron",
                role="member",
                payload={
                    "cron_payload": {
                        "origin": "phone_play",
                        "play_task": True,
                        "target_tokens": 0,
                    }
                },
            )
            req = _injection_request()
            await plugin.inject_phone_guidance(event, req)
            prompt = req.system_prompt
            assert "【手机操控能力】" in prompt
            for marker in markers.values():
                assert marker in prompt
            # 自主任务不登记用户任务优先
            assert not plugin.service.user_priority.sessions
            assert calls["record_tokens"] == 1
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 设备占用互斥与收尾 HOME 条件
# ----------------------------------------------------------------------


def test_second_user_task_receives_busy_hint(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        gate = plugin.device_gate
        first = FakeEvent(umo="webchat:FriendMessage:1", role="admin")
        with gate.scope(first, purpose="command:open") as first_scope:
            try:
                second = FakeEvent(
                    umo="webchat:FriendMessage:2",
                    role="admin",
                    text="/phone open B站",
                )
                await plugin.phone_command(second, "open B站")
                # 占用提示按持有者类型给出（首个任务经命令入口，类型为命令）
                assert "手机正被另一个" in second.sent_text()
                # 第一个任务的租约没有被误释放
                assert gate.busy is True
                assert gate.owner_umo == first.unified_msg_origin
                assert gate.release_event(second) is False
                # 第一个任务自己的作用域仍然有效（未暂停/未取消/未到期）
                first_scope.checkpoint("tap")
            finally:
                await _cleanup(plugin)
        assert gate.busy is False

    asyncio.run(scenario())


def test_autonomous_task_cannot_reacquire_after_user_preempt(
    tmp_path, monkeypatch
):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        gate = plugin.device_gate
        session = "webchat:FriendMessage:1"
        try:
            # 用户任务优先已登记（抢占自主任务后，自主任务不得重新取得设备租约）
            assert plugin.service.user_priority.begin(session) is True
            plugin.device_gate.preempt("user_priority")
            cron_event = FakeEvent(
                umo="cron:phone_play:1",
                platform="cron",
                role="member",
                payload={
                    "cron_payload": {
                        "origin": "phone_play",
                        "play_task": True,
                        "target_tokens": 0,
                    }
                },
            )
            with pytest.raises(GateError) as exc:
                gate.enter(cron_event, purpose="phone_screen", write=False)
            assert "用户任务优先" in exc.value.message
            # 让路拒绝不抢租约、不改变代次
            assert gate.busy is False
            assert gate.epoch == 0
            # 工具路径同样被拒（不会先拿到租约再报错）
            tool = PhoneScreenTool(service=plugin.service)
            result = await tool.call(FakeToolContext(cron_event))
            assert "用户任务优先" in result
            assert gate.busy is False
        finally:
            plugin.service.user_priority.end(session)
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_home_cleanup_skipped_while_other_lease_active(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        gate = plugin.device_gate
        cleanup_calls: list[int] = []

        async def fake_cleanup() -> None:
            cleanup_calls.append(1)

        monkeypatch.setattr(plugin, "_cleanup_phone_home", fake_cleanup)
        other = FakeEvent(umo="webchat:FriendMessage:2", role="admin")
        gate.enter(other, purpose="command:open")
        done_event = FakeEvent(umo="webchat:FriendMessage:1", role="admin")
        done_event.set_extra("phone_control_used", True)
        try:
            await plugin.on_task_agent_done(done_event)
            # 其他任务仍持有租约：HOME/清后台会打断它，必须跳过
            assert cleanup_calls == []
            assert gate.busy is True
            assert gate.owner_umo == other.unified_msg_origin
            # 最后一个任务结束时才允许收尾
            gate.release_event(other)
            await plugin.on_task_agent_done(done_event)
            assert cleanup_calls == [1]
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_home_cleanup_runs_when_no_other_lease(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        cleanup_calls: list[int] = []

        async def fake_cleanup() -> None:
            cleanup_calls.append(1)

        monkeypatch.setattr(plugin, "_cleanup_phone_home", fake_cleanup)
        event = FakeEvent(role="admin")
        event.set_extra("phone_control_used", True)
        try:
            await plugin.on_task_agent_done(event)
            assert cleanup_calls == [1]
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())
