"""main 级执行一致性回归：鉴权顺序、持久化 fail-closed、提示词注入隔离、设备占用。

覆盖场景：

* 鉴权先于任何抢占/中止副作用：非主人 ``/phone status`` 只收到权限拒绝，白名单用户
  的命令才登记用户任务优先；
* 验证状态持久化失败后设备操作被拒绝（fail-closed），内存中仍保持暂停，写入恢复后
  自动解除；随机玩循环在故障态停排；
* 提示词注入隔离：非授权请求零注入；普通对话只注入能力指南，不注入日记/学习/App
  经验；手机任务/手机请求才注入手机活动记忆；
* 设备占用互斥与收尾：第二个用户任务收到占用提示且不放走第一个任务的租约；任务
  结束收尾 HOME 在其他任务仍持有租约时跳过；收尾在自持设备租约内执行 HOME/清后台，
  新任务接管租约后旧收尾立即停止；
* 工具副作用分类与执行期限：会导航/点击的工具在验证进行中按写操作拒绝，纯读观察
  工具保持只读；单次尝试到期后验证批次与最终确认不再产生设备动作。

依赖 AstrBot（本地无 astrbot 时整体跳过）：在 AstrBot 容器内跑 ``pytest tests``
时用假 Context/事件直接验证 main.py 接线（不连接真实设备）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest

_astrbot = pytest.importorskip("astrbot")
if getattr(_astrbot, "_acat_local_stub", False):
    pytest.skip("本地无真实 AstrBot，跳过容器级用例", allow_module_level=True)


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
PhoneCheckInboxTool = _tools_ns["PhoneCheckInboxTool"]


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
    return types.SimpleNamespace(
        system_prompt="",
        prompt=str(prompt),
        contexts=[],
        extra_user_content_parts=[],
    )


def _injected_text(req: types.SimpleNamespace) -> str:
    """取本轮注入的临时用户内容块文本（不读 system_prompt）。"""
    return "\n".join(
        str(getattr(part, "text", ""))
        for part in getattr(req, "extra_user_content_parts", None) or []
    )


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
            prompt = _injected_text(req)
            assert req.system_prompt == ""
            assert prompt == ""
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
            prompt = _injected_text(req)
            assert req.system_prompt == ""
            # 指南对授权用户始终可用，且以临时内容块追加（不写系统提示词）
            assert "【手机操控能力】" in prompt
            assert all(
                getattr(part, "_no_save", False) is True
                for part in req.extra_user_content_parts
            )
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
            prompt = _injected_text(req)
            assert req.system_prompt == ""
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
            prompt = _injected_text(req)
            assert req.system_prompt == ""
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

        async def fake_cleanup(**_kwargs) -> None:
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

        async def fake_cleanup(**_kwargs) -> None:
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


def test_home_cleanup_holds_own_lease_and_releases_it(tmp_path, monkeypatch):
    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        gate = plugin.device_gate
        operations: list[str] = []
        busy_during: list[bool] = []

        async def fake_press_key(key: str) -> None:
            gate.checkpoint("press_key")
            operations.append(key)
            busy_during.append(gate.busy)

        async def fake_shell(command: str, **_kwargs) -> str:
            gate.checkpoint("shell")
            operations.append(command)
            busy_during.append(gate.busy)
            return ""

        monkeypatch.setattr(plugin.actions, "press_key", fake_press_key)
        monkeypatch.setattr(plugin.adb, "shell", fake_shell)
        event = FakeEvent(role="admin")
        try:
            await plugin._auto_cleanup_after_task(event)
            # HOME 与清后台都在收尾自己的设备作用域内执行（租约由收尾持有）。
            assert operations == ["home", "am kill-all"]
            assert busy_during == [True, True]
            # 收尾结束释放自己的可让路租约，不残留占用。
            assert gate.busy is False
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_home_cleanup_stops_when_new_task_takes_over_lease(tmp_path, monkeypatch):
    """A12 重放：HOME 后新任务取得租约，旧收尾必须停止，不得继续 am kill-all。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        gate = plugin.device_gate
        home_done = asyncio.Event()
        operations: list[dict] = []

        async def fake_press_key(key: str) -> None:
            gate.checkpoint("press_key")
            operations.append({"op": key, "lease": gate.busy})
            if key == "home":
                home_done.set()

        async def fake_shell(command: str, **_kwargs) -> str:
            gate.checkpoint("shell")
            operations.append({"op": command, "lease": gate.busy})
            return ""

        monkeypatch.setattr(plugin.actions, "press_key", fake_press_key)
        monkeypatch.setattr(plugin.adb, "shell", fake_shell)
        old_event = FakeEvent(umo="webchat:FriendMessage:old", role="admin")
        task = asyncio.create_task(plugin._auto_cleanup_after_task(old_event))
        new_event = None
        try:
            await asyncio.wait_for(home_done.wait(), timeout=5)
            # HOME 完成进入等待后，新任务接管可让路租约（不等待旧收尾结束）。
            new_event = FakeEvent(umo="webchat:FriendMessage:new", role="admin")
            with gate.scope(new_event, purpose="phone_tap", write=True):
                await asyncio.wait_for(task, timeout=5)
                # 旧收尾在下一次门禁检查处停止：没有执行 am kill-all。
                assert [item["op"] for item in operations] == ["home"]
                assert gate.busy is True
                assert gate.owner_umo == new_event.unified_msg_origin
        finally:
            if new_event is not None:
                gate.release_event(new_event)
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_home_cleanup_after_force_release_still_cleans(tmp_path, monkeypatch):
    """显式停止先强制释放租约：失效命令作用域内的收尾仍按后台操作执行。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        gate = plugin.device_gate
        operations: list[str] = []

        async def fake_press_key(key: str) -> None:
            operations.append(key)

        async def fake_shell(command: str, **_kwargs) -> str:
            operations.append(command)
            return ""

        monkeypatch.setattr(plugin.actions, "press_key", fake_press_key)
        monkeypatch.setattr(plugin.adb, "shell", fake_shell)
        stop_event = FakeEvent(umo="webchat:FriendMessage:1", role="admin")
        try:
            with gate.scope(stop_event, purpose="command:stop"):
                # /phone stop：先收回租约并中止在途动作，再直接收尾。
                gate.force_release("phone_stop")
                await plugin._cleanup_phone_home()
            assert operations == ["home", "am kill-all"]
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_check_inbox_tool_denied_while_verification_active(tmp_path, monkeypatch):
    """A09 重放：验证 observing 时查收件箱不得切换 App/点击，会话结束后恢复。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        called: list[dict] = []

        async def fake_check(**kwargs) -> tuple[str, None, str]:
            called.append(dict(kwargs))
            return "synthetic inbox", None, ""

        monkeypatch.setattr(plugin.service, "check_inbox", fake_check)
        event = FakeEvent(role="admin")
        inbox_tool = PhoneCheckInboxTool(service=plugin.service)
        screen_tool = PhoneScreenTool(service=plugin.service)
        try:
            plugin._verification.state = verification_mod.new_session()
            result = await inbox_tool.call(FakeToolContext(event))
            assert "error:" in str(result)
            assert called == []
            # 分类正确：可导航工具按写操作，纯读观察工具保持只读。
            assert inbox_tool._is_write_tool() is True
            assert screen_tool._is_write_tool() is False
            assert (
                plugin._verification.guard_tool(
                    "phone_screen", write=False
                )
                == ""
            )
            # 验证会话正常结束（通过）后恢复收件箱浏览能力。
            plugin._verification.state["status"] = (
                verification_mod.STATUS_PASSED
            )
            result = await inbox_tool.call(FakeToolContext(event))
            assert "error:" not in str(result)
            assert called
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())


def test_confirm_skipped_after_attempt_deadline_expired(tmp_path, monkeypatch):
    """A15 重放：首步后单次尝试到期，最终确认点击不得再执行。"""

    async def scenario() -> None:
        plugin = _make_plugin(tmp_path, monkeypatch)
        verification = plugin._verification
        verification.state = verification_mod.new_session()
        taps: list[tuple[int, int]] = []

        async def fake_screen_size() -> tuple[int, int]:
            return (1080, 1920)

        async def fake_tap(x: int, y: int) -> None:
            plugin.service.guard_device("tap")
            taps.append((x, y))
            if len(taps) == 1:
                # 模拟首步执行后单次尝试期限已到（默认 30s）。
                verification.state["attempt_started_at"] = time.time() - 31

        monkeypatch.setattr(plugin.actions, "screen_size", fake_screen_size)
        monkeypatch.setattr(plugin.service, "tap", fake_tap)
        event = FakeEvent(role="admin")
        try:
            with plugin.device_gate.scope(
                event, purpose="phone_solve_verification", write=False
            ):
                allowed, _reason = await verification.reserve_attempt(event)
                assert allowed is True
                with pytest.raises(plugin_main.PhoneControlError):
                    await plugin.service.exec_verify_ops(
                        [{"action": "tap", "x": 10, "y": 20}],
                        confirm={"x": 30, "y": 40},
                        wait_seconds=0.2,
                    )
            # 首步点击已执行（不可回滚），最终确认点击必须为 0。
            assert taps == [(10, 20)]
        finally:
            await _cleanup(plugin)

    asyncio.run(scenario())
