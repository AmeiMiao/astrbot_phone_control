"""工具层统一设备门禁与验证求解器取消竞速的单元测试。

这些用例只用假 service/controller/事件驱动 ``core.tools`` 与 ``core.actions``，
不连接真实设备；本地缺少 AstrBot 依赖时用最小桩模块导入工具层。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _third_party_available() -> bool:
    """AstrBot 工具层依赖是否齐全；缺任意一个就走桩导入。"""
    try:
        import astrbot.api  # noqa: F401
        import mcp.types  # noqa: F401
        import pydantic  # noqa: F401
    except ImportError:
        return False
    if getattr(astrbot.api, "_acat_local_stub", False):
        return False
    return True


def _install_import_stubs() -> list[str]:
    """为本地环境注册最小的 astrbot/mcp/pydantic 桩模块，返回新增模块名。"""
    # conftest 的 astrbot.api.logger 桩不含工具层符号，先移除再注册完整桩。
    for name in [n for n in list(sys.modules) if n == "astrbot" or n.startswith("astrbot.")]:
        if getattr(sys.modules[name], "_acat_local_stub", False):
            sys.modules.pop(name, None)
    added: list[str] = []

    def register(name: str, module: types.ModuleType) -> types.ModuleType:
        if name not in sys.modules:
            sys.modules[name] = module
            added.append(name)
        return sys.modules[name]

    class FunctionTool:
        """FunctionTool 桩：只提供工具元信息字段与泛型下标。"""

        name: str = ""
        description: str = ""
        parameters: dict = {}

        @classmethod
        def __class_getitem__(cls, _item: Any) -> type:
            return cls

    class MessageChain:
        def message(self, *_args: Any, **_kwargs: Any) -> "MessageChain":
            return self

        def file_image(self, *_args: Any, **_kwargs: Any) -> "MessageChain":
            return self

    class CallToolResult:
        def __init__(self, content: Any = None) -> None:
            self.content = list(content or [])

    class TextContent:
        def __init__(self, type: str = "text", text: str = "") -> None:  # noqa: A002
            self.type = type
            self.text = text

    class ImageContent:
        def __init__(
            self,
            type: str = "image",  # noqa: A002
            data: str = "",
            mimeType: str = "",  # noqa: N803
        ) -> None:
            self.type = type
            self.data = data
            self.mimeType = mimeType

    class NullLogger:
        """测试用静默 logger。"""

        def debug(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def info(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def warning(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def error(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def exception(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def critical(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []  # type: ignore[attr-defined]
    register("astrbot", astrbot)

    api = types.ModuleType("astrbot.api")
    api.FunctionTool = FunctionTool
    api.logger = NullLogger()
    register("astrbot.api", api)

    api_event = types.ModuleType("astrbot.api.event")
    api_event.MessageChain = MessageChain
    register("astrbot.api.event", api_event)

    astrbot_core = types.ModuleType("astrbot.core")
    astrbot_core.__path__ = []  # type: ignore[attr-defined]
    register("astrbot.core", astrbot_core)

    agent = types.ModuleType("astrbot.core.agent")
    agent.__path__ = []  # type: ignore[attr-defined]
    register("astrbot.core.agent", agent)

    run_context = types.ModuleType("astrbot.core.agent.run_context")
    run_context.ContextWrapper = type("ContextWrapper", (), {})
    register("astrbot.core.agent.run_context", run_context)

    tool_exec = types.ModuleType("astrbot.core.agent.tool")
    tool_exec.ToolExecResult = Any
    register("astrbot.core.agent.tool", tool_exec)

    agent_context = types.ModuleType("astrbot.core.astr_agent_context")
    agent_context.AstrAgentContext = type("AstrAgentContext", (), {})
    register("astrbot.core.astr_agent_context", agent_context)

    message = types.ModuleType("astrbot.core.message")
    message.__path__ = []  # type: ignore[attr-defined]
    register("astrbot.core.message", message)

    message_result = types.ModuleType("astrbot.core.message.message_event_result")
    message_result.MessageChain = MessageChain
    register("astrbot.core.message.message_event_result", message_result)

    platform = types.ModuleType("astrbot.core.platform")
    platform.__path__ = []  # type: ignore[attr-defined]
    register("astrbot.core.platform", platform)

    message_session = types.ModuleType("astrbot.core.platform.message_session")
    message_session.MessageSession = type("MessageSession", (), {})
    register("astrbot.core.platform.message_session", message_session)

    mcp = types.ModuleType("mcp")
    mcp.__path__ = []  # type: ignore[attr-defined]
    mcp_types = types.ModuleType("mcp.types")
    mcp_types.ContentBlock = object
    mcp_types.CallToolResult = CallToolResult
    mcp_types.TextContent = TextContent
    mcp_types.ImageContent = ImageContent
    mcp.types = mcp_types
    register("mcp", mcp)
    register("mcp.types", mcp_types)

    pydantic = types.ModuleType("pydantic")

    def Field(*, default_factory: Any = None, **_kwargs: Any) -> Any:
        return dataclasses.field(default_factory=default_factory)

    pydantic.Field = Field
    pydantic_dataclasses = types.ModuleType("pydantic.dataclasses")
    pydantic_dataclasses.dataclass = dataclasses.dataclass
    pydantic.dataclasses = pydantic_dataclasses
    register("pydantic", pydantic)
    register("pydantic.dataclasses", pydantic_dataclasses)
    return added


_STUB_MODULES = _install_import_stubs() if not _third_party_available() else []

from core import tools as tools_mod  # noqa: E402
from core.actions import DeviceActions  # noqa: E402
from core.config import PhoneConfig  # noqa: E402
from core.device_gate import ExecutionCancelled, GateError  # noqa: E402

# 工具子类在真实 AstrBot 下必须是 pydantic dataclass；本地桩路径已移除 pydantic 桩，
# 用标准库 dataclass 等价物，保证两套依赖下用例都能构造工具实例。
try:  # pragma: no cover - 分支取决于是否安装真实 AstrBot
    from pydantic import Field as _PydanticField
    from pydantic.dataclasses import dataclass as _pydantic_dataclass
except ModuleNotFoundError:  # pragma: no cover - 本地无 AstrBot 依赖
    import dataclasses as _stdlib_dataclasses

    def _pydantic_dataclass(cls):  # type: ignore[misc]
        return _stdlib_dataclasses.dataclass(cls)

    def _PydanticField(*, default_factory=None, **_kwargs):  # type: ignore[misc]
        return _stdlib_dataclasses.field(default_factory=default_factory)


for _stub_name in _STUB_MODULES:
    sys.modules.pop(_stub_name, None)


def run(coro: Any) -> Any:
    """在独立事件循环里执行协程（测试环境未安装 pytest-asyncio）。"""
    return asyncio.run(coro)


class FakeEvent:
    """工具上下文所需的最小消息事件。"""

    def __init__(self, text: str = "") -> None:
        self.unified_msg_origin = "webchat:FriendMessage:1"
        self.role = "admin"
        self._text = text
        self._extra: dict[str, Any] = {}

    def get_extra(self, key: str) -> Any:
        return self._extra.get(key)

    def set_extra(self, key: str, value: Any) -> None:
        self._extra[key] = value


class FakeToolContext:
    def __init__(self, event: FakeEvent) -> None:
        self.context = types.SimpleNamespace(event=event)


def _config() -> PhoneConfig:
    return PhoneConfig.from_mapping({"adb": {"host": "127.0.0.1", "port": 5555}})


class FakeGate:
    """记录进入/退出的设备门禁。"""

    def __init__(self) -> None:
        self.entered: list[dict[str, Any]] = []
        self.exited = 0

    @contextlib.contextmanager
    def scope(self, event: Any, *, purpose: str, write: bool = True) -> Any:
        self.entered.append({"event": event, "purpose": purpose, "write": write})
        try:
            yield types.SimpleNamespace(
                gate=self, purpose=purpose, write=write, checkpoint=lambda op="": None
            )
        finally:
            self.exited += 1


class RejectingGate:
    """进入作用域即抛指定门禁异常。"""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.entered = 0

    def scope(self, event: Any, *, purpose: str, write: bool = True) -> Any:
        self.entered += 1
        exc = self.exc

        class _Scope:
            def __enter__(self) -> Any:
                raise exc

            def __exit__(self, *_args: Any) -> bool:
                return False

        return _Scope()


class FakeController:
    """验证控制器桩：覆盖工具层使用的接口。"""

    _VERIFY_TOOLS = frozenset({"phone_solve_verification", "phone_bypass_verify"})
    _READ_ONLY_TOOLS = frozenset(
        {"phone_status", "phone_screen", "phone_screenshot", "phone_wait"}
    )

    def __init__(
        self,
        *,
        allow: bool = True,
        generation: int = 0,
        remaining: float = 30.0,
        state: dict[str, Any] | None = None,
        paused: bool = False,
    ) -> None:
        self.allow = allow
        self.cancel_generation = generation
        self.remaining = remaining
        self.state: dict[str, Any] = state or {}
        self.paused = paused
        self.cancelled_event = asyncio.Event()
        self.reserve_calls: list[str] = []
        self.submitted: list[Any] = []

    async def reserve_attempt(
        self, event: Any, *, kind: str = "solver"
    ) -> tuple[bool, str]:
        self.reserve_calls.append(kind)
        if not self.allow:
            return False, "error: 验证已暂停，等待人工协助"
        self.state = {
            "id": "ver-test",
            "status": "attempting",
            "attempts": 1,
            "max_attempts": 3,
            "failure_count": 0,
            "quiz_answered": 0,
            **self.state,
        }
        return True, ""

    def cancelled(self) -> asyncio.Event:
        return self.cancelled_event

    def attempt_remaining_seconds(self) -> float:
        return self.remaining

    def _limits(self) -> dict[str, int]:
        return {"max_attempts": 3}

    async def submit_outcome(self, event: Any, outcome: Any) -> str:
        self.submitted.append(outcome)
        return ""

    def status_text(self) -> str:
        return "验证会话：ver-test（等待人工协助）"


class FakeService:
    """手机能力门面桩：覆盖工具门禁与验证求解需要的接口。"""

    def __init__(
        self,
        *,
        gate: Any = None,
        verification: Any = None,
        config: PhoneConfig | None = None,
    ) -> None:
        self.gate = gate
        self.verification = verification
        self.config = config or _config()
        self.app_context: Any = object()
        self.capture_calls = 0
        self.screen_text = "验证页面：请完成滑块验证"
        self.executed_ops: list[Any] = []

    async def status_text(self) -> str:
        return "设备正常"

    def quota_error_text(self) -> str:
        return ""

    def quota_warning_text(self) -> str | None:
        return None

    def should_yield_to_user(self, event: Any) -> bool:
        return False

    def ensure_play_budget(self, event: Any) -> Any:
        return None

    async def capture(self, **_kwargs: Any) -> tuple[bytes, Path, dict[str, Any]]:
        self.capture_calls += 1
        return b"fake-image", Path("/tmp/fake-shot.png"), {"method": "png"}

    async def screen(self, max_nodes: int = 0) -> tuple[str, Any]:
        return self.screen_text, None

    async def exec_verify_ops(
        self, ops: Any, *, confirm: Any = None, wait_seconds: float = 1.0
    ) -> str:
        self.executed_ops.append((ops, confirm))
        return "已执行验证操作：tap(10,20)"


@tools_mod._wrap_call
async def _probe_call(self: Any, context: Any, **_kwargs: Any) -> Any:
    """测试用的最小工具：只记录被调用，用来验证门禁包裹。"""
    return f"ok:{self.name}"


def _status_tool(service: Any) -> Any:
    return tools_mod.PhoneStatusTool(service=service)


def _solve_tool(service: Any) -> Any:
    return tools_mod.PhoneSolveVerificationTool(service=service)


# ----------------------------------------------------------------------
# 统一门禁包裹
# ----------------------------------------------------------------------


def test_gate_absent_keeps_original_behavior() -> None:
    service = FakeService(gate=None)
    result = run(_probe_call(_status_tool(service), FakeToolContext(FakeEvent())))
    assert result == "ok:phone_status"


def test_gate_scope_wraps_call_and_uses_write_flag() -> None:
    gate = FakeGate()
    controller = FakeController()
    service = FakeService(gate=gate, verification=controller)
    context = FakeToolContext(FakeEvent())
    read_result = run(_probe_call(_status_tool(service), context))
    write_result = run(_probe_call(tools_mod.PhoneTapTool(service=service), context))
    assert read_result == "ok:phone_status"
    assert write_result == "ok:phone_tap"
    assert [item["purpose"] for item in gate.entered] == [
        "phone_status",
        "phone_tap",
    ]
    assert [item["write"] for item in gate.entered] == [False, True]
    assert gate.exited == 2


def test_gate_error_returns_error_text() -> None:
    gate = RejectingGate(GateError("手机正被另一个用户任务使用"))
    service = FakeService(gate=gate)
    result = run(_probe_call(_status_tool(service), FakeToolContext(FakeEvent())))
    assert result == "error: 手机正被另一个用户任务使用"


def test_gate_scope_refresh_reenters_scope_after_reservation() -> None:
    gate = FakeGate()
    controller = FakeController()
    service = FakeService(gate=gate, verification=controller)
    tool = _solve_tool(service)
    event = FakeEvent()
    with tool._gate_scope_after_reservation(event):
        pass
    # 同一事件可重入：刷新只是一个新的设备作用域（写标记按工具分类）。
    assert gate.entered == [
        {
            "event": event,
            "purpose": "phone_solve_verification",
            "write": False,
        }
    ]
    assert gate.exited == 1


def test_solve_rebinds_gate_scope_after_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = FakeGate()
    controller = FakeController()
    service = FakeService(gate=gate, verification=controller)
    observed: list[int] = []

    async def fake_impl(self: Any, context: Any, **kwargs: Any) -> Any:
        # 预占后的求解阶段应当已经处在刷新的设备作用域内（包装器 1 + 刷新 1）。
        observed.append(len(gate.entered))
        return tools_mod._SolveRun(result="ok")

    monkeypatch.setattr(
        tools_mod.PhoneSolveVerificationTool, "_solve_impl", fake_impl
    )
    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent())))
    assert result == "ok"
    assert [item["purpose"] for item in gate.entered] == [
        "phone_solve_verification",
        "phone_solve_verification",
    ]
    assert observed == [2]


def test_execution_cancelled_returns_abort_notice_without_error_prefix() -> None:
    gate = RejectingGate(ExecutionCancelled("手机操作已被暂停，请立即停止本轮操作"))
    service = FakeService(gate=gate)
    result = run(_probe_call(_status_tool(service), FakeToolContext(FakeEvent())))
    assert "已中止" in result
    assert "手机操作已被暂停" in result
    assert not result.startswith("error:")


def test_is_write_tool_uses_controller_registry() -> None:
    controller = FakeController()
    service = FakeService(verification=controller)
    assert _status_tool(service)._is_write_tool() is False
    assert tools_mod.PhoneTapTool(service=service)._is_write_tool() is True
    assert _solve_tool(service)._is_write_tool() is False


def test_is_write_tool_defaults_to_write_without_controller() -> None:
    service = FakeService(verification=None)
    assert _status_tool(service)._is_write_tool() is True


# ----------------------------------------------------------------------
# 验证求解：结构化判定与取消竞速
# ----------------------------------------------------------------------


@pytest.fixture
def outcome_api(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """补齐验证模块的结构化判定接口；真实实现就位后同样覆盖为可控桩。"""
    module = tools_mod.verification_mod
    constants = {
        "OUTCOME_CLEARED": "cleared",
        "OUTCOME_PROGRESS": "progress",
        "OUTCOME_FAILED": "failed",
        "OUTCOME_UNKNOWN": "unknown",
        "OUTCOME_NOT_CHALLENGE": "not_challenge",
        "SOLVE_KIND_CAPTCHA": "captcha",
        "SOLVE_KIND_QUIZ": "quiz",
    }
    for name, value in constants.items():
        monkeypatch.setattr(module, name, value, raising=False)

    recorded: list[dict[str, Any]] = []

    class FakeSolveOutcome:
        def __init__(self, **kwargs: Any) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def fake_judge(**kwargs: Any) -> FakeSolveOutcome:
        recorded.append(dict(kwargs))
        return FakeSolveOutcome(
            outcome="cleared",
            kind=kwargs.get("kind", ""),
            evidence="",
            question=0,
            generation=int(kwargs.get("generation") or 0),
        )

    monkeypatch.setattr(module, "SolveOutcome", FakeSolveOutcome, raising=False)
    monkeypatch.setattr(module, "judge_solve_outcome", fake_judge, raising=False)
    return types.SimpleNamespace(module=module, recorded=recorded, outcome=FakeSolveOutcome)


def _patch_vision(
    monkeypatch: pytest.MonkeyPatch,
    first: dict[str, Any],
    second: dict[str, Any] | None = None,
) -> None:
    """替换两阶段识图调用，避免依赖真实视觉模型。"""

    async def fake_vision(app_context: Any, provider_id: str, prompt: str, *args: Any, **kwargs: Any) -> tuple[Any, str]:
        return (first if "is_captcha" in prompt else second), "raw-text"

    # 工具实现位于 core.tools.solve 子模块：它按模块全局名调用这两个辅助函数，
    # 因此 monkeypatch 必须打到子模块命名空间（包根上的同名符号只是兼容再导出）。
    solve_module = tools_mod.solve
    monkeypatch.setattr(solve_module, "_vision_json", fake_vision)
    monkeypatch.setattr(
        solve_module,
        "_resolve_ocr_provider",
        lambda *_args, **_kwargs: ("provider-x", "ocr"),
    )


def test_solve_pause_wins_cancels_in_flight_and_skips_outcome(
    monkeypatch: pytest.MonkeyPatch, outcome_api: types.SimpleNamespace
) -> None:
    state: dict[str, Any] = {"started": False, "cancelled": False}

    async def slow_impl(self: Any, context: Any, **kwargs: Any) -> Any:
        state["started"] = True
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return tools_mod._SolveRun(result="不应返回")

    monkeypatch.setattr(
        tools_mod.PhoneSolveVerificationTool, "_solve_impl", slow_impl
    )
    controller = FakeController()
    controller.cancelled_event.set()
    service = FakeService(gate=None, verification=controller)

    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent())))

    assert "验证暂停，已中止在途求解" in result
    assert state["started"] is True
    assert state["cancelled"] is True
    assert controller.submitted == []


def test_solve_cancel_and_completion_same_tick_discards_outcome(
    monkeypatch: pytest.MonkeyPatch, outcome_api: types.SimpleNamespace
) -> None:
    """取消信号与求解完成同周期：结果必须丢弃，不能提交/污染会话。"""
    state: dict[str, Any] = {"started": False, "completed": False}

    async def instant_impl(self: Any, context: Any, **kwargs: Any) -> Any:
        state["started"] = True
        # 求解返回前触发取消：两者落在同一事件循环周期
        controller.cancelled_event.set()
        state["completed"] = True
        return tools_mod._SolveRun(
            result="本次结果不应提交",
            outcome=outcome_api.outcome(
                outcome="cleared",
                kind="captcha",
                evidence="同周期完成",
                question=0,
                generation=0,
            ),
        )

    controller = FakeController()
    monkeypatch.setattr(
        tools_mod.PhoneSolveVerificationTool, "_solve_impl", instant_impl
    )
    service = FakeService(gate=None, verification=controller)

    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent())))

    assert "验证暂停" in result
    assert state["started"] is True
    assert state["completed"] is True
    assert controller.submitted == []


def test_solve_returns_reserve_reason_when_not_allowed() -> None:
    controller = FakeController(allow=False)
    service = FakeService(gate=None, verification=controller)
    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent())))
    assert result == "error: 验证已暂停，等待人工协助"
    assert controller.submitted == []


def test_solve_submits_structured_outcome_with_generation(
    monkeypatch: pytest.MonkeyPatch, outcome_api: types.SimpleNamespace
) -> None:
    first = {
        "content": "滑块验证页面",
        "is_captcha": True,
        "is_quiz": False,
        "captcha_type": "slider",
    }
    second = {"challenge_confirmed": True, "ops": [{"action": "tap", "x": 10, "y": 20}], "confirm": None, "note": ""}
    _patch_vision(monkeypatch, first, second)
    controller = FakeController(generation=7)
    service = FakeService(gate=None, verification=controller)

    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent())))
    text = tools_mod._result_text(result)

    assert controller.reserve_calls == ["solver"]
    assert service.executed_ops, "应当执行视觉模型给出的操作"
    assert len(outcome_api.recorded) == 1
    judged = outcome_api.recorded[0]
    assert judged["generation"] == 7
    assert judged["kind"] == "captcha"
    assert judged["observation_ok"] is True
    assert "请完成滑块验证" in judged["pre_text"]
    assert len(controller.submitted) == 1
    submitted = controller.submitted[0]
    assert submitted.generation == 7
    assert submitted.outcome == "cleared"
    assert "判定：验证挑战已解除" in text
    assert "尝试 1/3" in text
    assert "失败 0 次" in text


def test_solve_non_challenge_submits_not_challenge_outcome(
    monkeypatch: pytest.MonkeyPatch, outcome_api: types.SimpleNamespace
) -> None:
    first = {
        "content": "普通聊天页面",
        "is_captcha": False,
        "is_quiz": False,
        "captcha_type": "none",
    }
    _patch_vision(monkeypatch, first)
    controller = FakeController(generation=3)
    service = FakeService(gate=None, verification=controller)

    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent())))
    text = tools_mod._result_text(result)

    assert len(controller.submitted) == 1
    submitted = controller.submitted[0]
    assert submitted.outcome == "not_challenge"
    assert submitted.generation == 3
    assert "未检测到验证码/答题页面" in text
    assert "判定：未检测到验证/答题" in text


def test_solve_without_controller_returns_impl_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def impl_stub(self: Any, context: Any, **kwargs: Any) -> Any:
        return tools_mod._SolveRun(result="控制器缺失时的求解结果")

    monkeypatch.setattr(
        tools_mod.PhoneSolveVerificationTool, "_solve_impl", impl_stub
    )
    service = FakeService(gate=None, verification=None)
    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent())))
    assert result == "控制器缺失时的求解结果"


# ----------------------------------------------------------------------
# 设备动作门禁回调
# ----------------------------------------------------------------------


class FakeAdb:
    """DeviceActions 所需的最小 ADB 客户端桩。"""

    def __init__(self, wm_output: str = "Physical size: 900x1600") -> None:
        self.wm_output = wm_output
        self.commands: list[str] = []
        self.ensure_ready_calls = 0

    async def ensure_ready(self) -> None:
        self.ensure_ready_calls += 1

    async def shell(self, command: str) -> str:
        self.commands.append(command)
        return self.wm_output if command == "wm size" else ""


def test_actions_without_guard_only_runs_device_command() -> None:
    adb = FakeAdb()
    actions = DeviceActions(adb)
    run(actions.press_key("back"))
    assert adb.commands == ["input keyevent 4"]


def test_actions_guard_blocks_before_any_device_operation() -> None:
    adb = FakeAdb()
    calls: list[str] = []

    def guard(op: str) -> None:
        calls.append(op)
        raise ExecutionCancelled("手机操作已被暂停，请立即停止本轮操作")

    actions = DeviceActions(adb, guard=guard)
    with pytest.raises(ExecutionCancelled):
        run(actions.press_key("back"))
    assert calls == ["press_key"]
    assert adb.ensure_ready_calls == 0
    assert adb.commands == []


def test_actions_guard_can_be_bound_late_and_may_be_async() -> None:
    adb = FakeAdb()
    calls: list[str] = []

    async def guard(op: str) -> None:
        calls.append(op)

    actions = DeviceActions(adb)
    actions.guard = guard
    width, height = run(actions.screen_size())
    assert (width, height) == (900, 1600)
    assert calls == ["screen_size"]


def test_actions_screenshot_entry_runs_guard(tmp_path: Path) -> None:
    adb = FakeAdb()
    calls: list[str] = []

    def guard(op: str) -> None:
        calls.append(op)
        raise ExecutionCancelled("已暂停")

    actions = DeviceActions(adb, guard=guard)
    with pytest.raises(ExecutionCancelled):
        run(actions.screenshot(tmp_path / "shot.png"))
    assert calls == ["screenshot"]
    assert adb.ensure_ready_calls == 0


class ShellAdb:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.cancelled = False
        self.block = False
        self.code = 0

    async def ensure_ready(self):
        pass

    async def shell_result(self, command, *, timeout):
        self.calls.append((command, timeout))
        self.started.set()
        try:
            if self.block:
                await asyncio.Event().wait()
            return {"exit_code": self.code, "stdout": "uid=2000(shell)", "stderr": "failure" if self.code else ""}
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def shell_service(enabled=True, gate=None):
    service = FakeService(gate=gate, config=PhoneConfig.from_mapping({
        "adb": {"host": "127.0.0.1", "port": 5555, "command_timeout": 2},
        "security": {"allow_shell_tool": enabled},
    }))
    service.adb = ShellAdb()
    return service


def test_shell_default_closed_and_permission_checked():
    service = shell_service(False)
    tool = tools_mod.PhoneShellTool(service=service)
    event = FakeEvent()
    assert "未开启设备 shell" in run(tool.call(FakeToolContext(event), command="id"))
    service.config = dataclasses.replace(service.config, security=dataclasses.replace(service.config.security, allow_shell_tool=True))
    event.role = "member"
    event.get_sender_id = lambda: "visitor"
    assert "权限不足" in run(tool.call(FakeToolContext(event), command="id"))
    assert not service.adb.calls


def test_shell_output_exit_code_and_timeout_clamped():
    service = shell_service()
    tool = tools_mod.PhoneShellTool(service=service)
    ctx = FakeToolContext(FakeEvent())
    assert '"exit_code": 0' in run(tool.call(ctx, command="id", timeout_seconds=999))
    assert service.adb.calls == [("id", 2)]
    service.adb.code = 7
    result = run(tool.call(ctx, command="exit 7"))
    assert result.startswith("error:") and '"exit_code": 7' in result and '"stderr": "failure"' in result
    assert len(service.adb.calls) == 2


@pytest.mark.parametrize("kwargs", [{"command": ""}, {"command": "x\x00y"}, {"command": "x"*8193}, {"command": "id", "timeout_seconds": -1}, {"command": "id", "timeout_seconds": float("nan")}])
def test_shell_invalid_input_has_no_device_execution(kwargs):
    service = shell_service()
    result = run(tools_mod.PhoneShellTool(service=service).call(FakeToolContext(FakeEvent()), **kwargs))
    assert result.startswith("error:") and not service.adb.calls


def test_shell_cancels_inflight_on_stop_and_authorization_change():
    from core.device_gate import DeviceExecutionGate

    async def scenario(mode):
        gate = DeviceExecutionGate()
        service = shell_service(gate=gate)
        service.adb.block = True
        tool = tools_mod.PhoneShellTool(service=service)
        task = asyncio.create_task(tool.call(FakeToolContext(FakeEvent()), command="sleep 20"))
        await service.adb.started.wait()
        if mode == "stop":
            gate.force_release("stop")
        else:
            service.config = dataclasses.replace(service.config, security=dataclasses.replace(service.config.security, allow_shell_tool=False))
        result = await asyncio.wait_for(task, 1)
        assert "已中止" in result or "授权已关闭" in result
        assert service.adb.cancelled and len(service.adb.calls) == 1
    for mode in ("stop", "revoke"):
        run(scenario(mode))


def test_shell_timeout_and_caller_cancel_reap_task():
    async def scenario():
        service = shell_service()
        service.adb.block = True
        tool = tools_mod.PhoneShellTool(service=service)
        result = await tool.call(FakeToolContext(FakeEvent()), command="sleep 20", timeout_seconds=.02)
        assert "超时" in result and service.adb.cancelled
        service.adb = ShellAdb()
        service.adb.block = True
        task = asyncio.create_task(tool.call(FakeToolContext(FakeEvent()), command="sleep 20"))
        await service.adb.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.adb.cancelled
    run(scenario())


def test_like_favorite_tools_share_run_quota_and_new_round_resets(tmp_path):
    from core.interactions import InteractionStore, interaction_run

    service = FakeService(config=PhoneConfig.from_mapping({"adb": {"host": "127.0.0.1", "port": 5555}, "security": {"allow_interactions": True, "max_likes_per_session": 1}}))
    service.interactions = InteractionStore(tmp_path)
    gate = tools_mod.PhoneInteractionGateTool(service=service)
    record = tools_mod.PhoneInteractionRecordTool(service=service)
    event = FakeEvent()
    ctx = FakeToolContext(event)
    assert "已预留" in run(gate.call(ctx, kind="like"))
    assert "名额已满" in run(gate.call(ctx, kind="favorite"))
    assert "失败并释放" in run(record.call(ctx, kind="like", success=False, summary="已激活，跳过"))
    assert "已预留" in run(gate.call(ctx, kind="favorite"))
    assert "success 必须是布尔" in run(record.call(ctx, kind="favorite", success="false", summary="invalid"))
    assert "成功" in run(record.call(ctx, kind="favorite", success=True, summary="已确认收藏"))
    assert "名额已满" in run(gate.call(ctx, kind="like"))
    interaction_run(event, restart=True)
    assert "已预留" in run(gate.call(ctx, kind="like"))
    service.interactions.finish_run(interaction_run(event))
    assert not service.interactions.reservations()


def test_light_interaction_switch_and_content_decision(tmp_path):
    from core.interactions import InteractionStore

    service = FakeService()
    service.interactions = InteractionStore(tmp_path)
    tool = tools_mod.PhoneInteractionGateTool(service=service)
    ctx = FakeToolContext(FakeEvent())
    assert "未开启点赞" in run(tool.call(ctx, kind="like"))
    service.config = dataclasses.replace(service.config, security=dataclasses.replace(service.config.security, allow_interactions=True))
    service._contents = types.SimpleNamespace(active={"o": {"decisions": []}}, owner=lambda event: "o")
    service.contents = service._contents
    assert "decide favorite" in run(tool.call(ctx, kind="favorite"))
    service._contents.active["o"]["decisions"].append({"decision": "favorite"})
    assert "已预留" in run(tool.call(ctx, kind="favorite"))


def test_original_content_comment_decision_reaches_real_quota_gate(tmp_path):
    from core.interactions import InteractionStore
    service = FakeService(config=PhoneConfig.from_mapping({"adb": {"host": "127.0.0.1", "port": 5555}, "security": {"allow_comments": True}}))
    service.interactions = InteractionStore(tmp_path)
    task = {"decisions": []}
    service._contents = types.SimpleNamespace(active={"o": task}, owner=lambda e: "o")
    service.contents = service._contents
    tool = tools_mod.PhoneInteractionGateTool(service=service)
    ctx = FakeToolContext(FakeEvent())
    assert "decide comment" in run(tool.call(ctx, kind="comment"))
    task["decisions"].append({"decision": "comment", "reason": "正文感想"})
    assert "已预留" in run(tool.call(ctx, kind="comment"))
    assert len(service.interactions.reservations()) == 1
    service.interactions.finish_run(ctx.context.event.get_extra("phone_interaction_run"))
    service.config = dataclasses.replace(service.config, security=dataclasses.replace(service.config.security, allow_comments=False))
    assert "未开启评论" in run(tool.call(ctx, kind="comment"))


def test_tool_wrapper_appends_app_experience_recall_for_open_app():
    """操作前召回接线：phone_open_app 结果后会追加当前 App 经验（真实 wrapper 路径）。"""
    from core.app_memory import AppMemoryEntry
    from core.tools.base import _PhoneToolBase, _wrap_call

    entry = AppMemoryEntry(
        app="com.example.app",
        label="示例",
        page="首页",
        note="顶部是搜索入口",
        updated_at=1_760_000_000.0,
    )

    class StubReader:
        async def select(self, **_kwargs: Any) -> list[Any]:
            return [entry]

    class StubMemory:
        def entries(self) -> list[Any]:
            return [entry]

    class StubActions:
        async def foreground(self) -> tuple[str, str]:
            return "com.example.app", "com.example.app.MainActivity"

    class StubExperiences:
        @staticmethod
        def observe(*_args: Any, **_kwargs: Any) -> None:
            pass

    service = FakeService(config=PhoneConfig.from_mapping({}))
    service.app_recall_reader = StubReader()
    service.app_memory = StubMemory()
    service.actions = StubActions()
    service.experiences = StubExperiences()
    service._cached_ui_snapshot = lambda: None

    @_pydantic_dataclass
    class OpenAppTool(_PhoneToolBase):
        name: str = "phone_open_app"
        description: str = "打开 App"
        parameters: dict = _PydanticField(
            default_factory=lambda: {"type": "object", "properties": {}}
        )

        @_wrap_call
        async def call(self, context: Any, **kwargs: Any) -> str:
            return "已打开 示例（com.example.app）"

    tool = OpenAppTool(service=service)
    result = run(tool.call(FakeToolContext(FakeEvent()), app="示例"))
    text = result if isinstance(result, str) else str(result)
    assert "已打开 示例" in text
    assert "【App 使用经验（按当前 App/页面召回）】" in text
    assert "顶部是搜索入口" in text


def test_tool_wrapper_recall_prefers_target_app_over_transient_foreground():
    """phone_open_app 刚启动时前台可能还是桌面：召回用参数解析出的目标 App 包名。"""
    from core.app_memory import AppMemoryEntry
    from core.tools.base import _PhoneToolBase, _wrap_call

    entry = AppMemoryEntry(
        app="com.example.app", label="示例", page="首页", note="底部导航五项"
    )
    seen_keys: list[str] = []

    class StubReader:
        async def select(self, **kwargs: Any) -> list[Any]:
            seen_keys.append(kwargs["app_key"])
            return [entry]

    class StubMemory:
        def entries(self) -> list[Any]:
            return [entry]

    class StubActions:
        async def foreground(self) -> tuple[str, str]:
            return "com.android.launcher", "com.android.launcher.Home"

    class StubExperiences:
        @staticmethod
        def observe(*_args: Any, **_kwargs: Any) -> None:
            pass

    service = FakeService(config=PhoneConfig.from_mapping({}))
    service.app_recall_reader = StubReader()
    service.app_memory = StubMemory()
    service.actions = StubActions()
    service.experiences = StubExperiences()
    service._cached_ui_snapshot = lambda: None
    service.resolve_app_target = lambda app: ("com.example.app", "示例")

    @_pydantic_dataclass
    class OpenAppTool(_PhoneToolBase):
        name: str = "phone_open_app"
        description: str = "打开 App"
        parameters: dict = _PydanticField(
            default_factory=lambda: {"type": "object", "properties": {}}
        )

        @_wrap_call
        async def call(self, context: Any, **kwargs: Any) -> str:
            return "已打开 示例（com.example.app）"

    tool = OpenAppTool(service=service)
    result = run(tool.call(FakeToolContext(FakeEvent()), app="示例"))
    text = result if isinstance(result, str) else str(result)
    assert seen_keys == ["com.example.app"]
    assert "底部导航五项" in text


def test_tool_wrapper_recall_falls_back_to_local_archive_on_semantic_failure():
    """语义召回异常/超时时回退本地档案，仍追加经验提示。"""
    from core.app_memory import AppMemoryEntry
    from core.tools.base import _PhoneToolBase, _wrap_call

    entry = AppMemoryEntry(
        app="com.example.app", label="示例", page="首页", note="顶部是搜索入口"
    )

    class BrokenReader:
        async def select(self, **_kwargs: Any) -> list[Any]:
            raise RuntimeError("embedding 超时")

    class StubMemory:
        def entries(self) -> list[Any]:
            return [entry]

    class StubActions:
        async def foreground(self) -> tuple[str, str]:
            return "com.example.app", "com.example.app.MainActivity"

    class StubExperiences:
        @staticmethod
        def observe(*_args: Any, **_kwargs: Any) -> None:
            pass

    service = FakeService(config=PhoneConfig.from_mapping({}))
    service.app_recall_reader = BrokenReader()
    service.app_memory = StubMemory()
    service.actions = StubActions()
    service.experiences = StubExperiences()
    service._cached_ui_snapshot = lambda: None

    @_pydantic_dataclass
    class ScreenTool(_PhoneToolBase):
        name: str = "phone_screen"
        description: str = "看屏幕"
        parameters: dict = _PydanticField(
            default_factory=lambda: {"type": "object", "properties": {}}
        )

        @_wrap_call
        async def call(self, context: Any, **kwargs: Any) -> str:
            return "屏幕：首页/推荐/搜索"

    tool = ScreenTool(service=service)
    result = run(tool.call(FakeToolContext(FakeEvent())))
    text = result if isinstance(result, str) else str(result)
    assert "【App 使用经验（按当前 App/页面召回）】" in text
    assert "顶部是搜索入口" in text


def test_break_rule_guard_stops_at_extended_budget_and_global_limit(tmp_path):
    from core.service import PhoneService

    raw = {"adb": {"host": "127.0.0.1", "port": 5555},
           "auto": {"break_rule": False}}
    service = PhoneService(raw_config=raw, adb=None, actions=None,
                           diary=None, data_dir=tmp_path)
    event = FakeEvent()
    event.set_extra("cron_payload", {"play_task": True, "target_tokens": 10000})
    context = FakeToolContext(event)
    tool = _status_tool(service)
    budget = service.ensure_play_budget(event)
    budget.spent_tokens = 10000
    with pytest.raises(tools_mod._ToolNotice, match="预算已完成"):
        run(tool._guard(context))
    raw["auto"]["break_rule"] = True
    assert run(tool._guard(context)) is event
    budget.spent_tokens = 12000
    with pytest.raises(tools_mod._ToolNotice, match="预算已完成"):
        run(tool._guard(context))
    budget.spent_tokens = 10000
    raw["quota"] = {"enabled": True, "max_tokens": 100}
    service.record_tokens(100, session=event.unified_msg_origin)
    with pytest.raises(tools_mod._ToolNotice, match="额度已用完"):
        run(tool._guard(context))


@pytest.mark.parametrize("kind,limit_key,allow_key", [
    ("comment", "max_comments_per_day", "allow_comments"),
    ("passive_reply", "max_passive_replies_per_day", "allow_passive_replies"),
    ("post", "max_posts_per_day", "allow_posts"),
])
def test_flexibility_limits_and_cooldown_reach_real_tool(tmp_path, kind, limit_key, allow_key):
    from core.interactions import InteractionStore
    raw = {"adb": {"host": "127.0.0.1", "port": 5555},
           "security": {allow_key: True, limit_key: 1, "interaction_cooldown_seconds": 999, "passive_reply_cooldown_seconds": 999},
           "flexibility": {"enabled": False, limit_key: 2}}
    service = FakeService(config=PhoneConfig.from_mapping(raw))
    service.interactions = InteractionStore(tmp_path)
    service.interactions.reserve(kind=kind, session="seed")
    service.interactions.record_result(kind=kind, success=True, summary="已发布", session="seed")
    tool = tools_mod.PhoneInteractionGateTool(service=service)
    ctx = FakeToolContext(FakeEvent())
    assert "上限" in run(tool.call(ctx, kind=kind))
    raw["flexibility"]["enabled"] = True
    raw["flexibility"]["ignore_interaction_cooldown"] = False
    service.config = PhoneConfig.from_mapping(raw)
    assert "操作太快" in run(tool.call(ctx, kind=kind))
    raw["flexibility"]["ignore_interaction_cooldown"] = True
    service.config = PhoneConfig.from_mapping(raw)
    assert "已预留" in run(tool.call(ctx, kind=kind))
    assert "名额已满" in run(tool.call(ctx, kind=kind))
    record = tools_mod.PhoneInteractionRecordTool(service=service)
    assert "成功" in run(record.call(ctx, kind=kind, success=True, summary="确认发布"))
    raw["flexibility"][limit_key] = -1
    service.config = PhoneConfig.from_mapping(raw)
    assert "已预留" in run(tool.call(ctx, kind=kind))
    raw["flexibility"][limit_key] = 0
    service.config = PhoneConfig.from_mapping(raw)
    assert "上限 0" in run(tool.call(ctx, kind=kind))
    raw["security"][allow_key] = False
    service.config = PhoneConfig.from_mapping(raw)
    assert "未开启" in run(tool.call(ctx, kind=kind))


def test_recoverable_record_summary_keeps_reservation_and_uses_elastic_limit(tmp_path):
    from core.interactions import InteractionStore, interaction_run
    raw = {"adb": {"host": "127.0.0.1", "port": 5555}, "security": {"allow_posts": True, "max_posts_per_day": 0},
           "flexibility": {"enabled": True, "max_posts_per_day": 2}}
    service = FakeService(config=PhoneConfig.from_mapping(raw))
    service.interactions = InteractionStore(tmp_path)
    ctx = FakeToolContext(FakeEvent())
    gate = tools_mod.PhoneInteractionGateTool(service=service)
    record = tools_mod.PhoneInteractionRecordTool(service=service)
    assert "已预留" in run(gate.call(ctx, kind="post"))
    assert "summary 过长" in run(record.call(ctx, kind="post", success=True, summary="长" * 145))
    assert len(service.interactions.reservations()) == 1
    assert "今日 1/2" in run(record.call(ctx, kind="post", success=True, summary="月面观察心得"))
    assert not service.interactions.reservations()
    assert service.interactions.used_today("post") == 1
    assert "已预留" in run(gate.call(ctx, kind="post"))
    service.interactions.finish_run(interaction_run(ctx.context.event))
    assert not service.interactions.reservations()
    assert "已预留" in run(gate.call(FakeToolContext(FakeEvent()), kind="post"))


@pytest.mark.parametrize('mode', ['auto', 'captcha', 'quiz'])
def test_explicit_solve_mode_cannot_turn_ordinary_page_into_challenge(monkeypatch, outcome_api, mode):
    first = {'content': '普通评论编辑页，发送按钮', 'is_captcha': False,
             'is_quiz': False, 'captcha_type': 'none'}
    _patch_vision(monkeypatch, first)
    controller = FakeController()
    service = FakeService(verification=controller)
    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent()), mode=mode))
    assert '未检测到验证码/答题页面' in tools_mod._result_text(result)
    assert not service.executed_ops
    assert len(controller.submitted) == 1
    assert controller.submitted[0].outcome == 'not_challenge'
    assert service.capture_calls == 1


def test_bypass_ordinary_page_does_not_create_session_or_touch_device(monkeypatch):
    controller = FakeController()
    service = FakeService(verification=controller)
    service.screen_text = '评论编辑页：来说点什么吧 发送'
    called = []
    async def bypass(**kwargs):
        called.append(kwargs)
        return 'unexpected', None, 'image/png'
    service.bypass_verify = bypass
    tool = tools_mod.PhoneBypassVerifyTool(service=service)
    text = run(tool.call(FakeToolContext(FakeEvent()), action='tap_points', points=[{'x': 600, 'y': 1060}]))
    assert '未检测到验证提示' in text and 'phone_tap' in text
    assert not controller.state and not controller.reserve_calls and not called
    assert not controller.paused


def test_bypass_unobservable_page_does_not_reserve_attempt():
    controller = FakeController()
    service = FakeService(verification=controller)
    async def screen(**kwargs):
        raise TimeoutError('UI unavailable')
    service.screen = screen
    tool = tools_mod.PhoneBypassVerifyTool(service=service)
    text = run(tool.call(FakeToolContext(FakeEvent()), action='drag', x1=10, y1=20, x2=40, y2=20))
    assert '观察失败' in text
    assert not controller.reserve_calls and not controller.state


def test_bypass_verified_page_still_reserves_and_executes_once():
    controller = FakeController()
    service = FakeService(verification=controller)
    calls = []
    async def bypass(**kwargs):
        calls.append(kwargs)
        return '完成一次拖动', None, 'image/png'
    async def finish(*args, **kwargs):
        calls.append('finish')
    service.bypass_verify = bypass
    controller.finish_attempt = finish
    tool = tools_mod.PhoneBypassVerifyTool(service=service)
    text = run(tool.call(FakeToolContext(FakeEvent()), action='drag', x1=10, y1=20, x2=40, y2=20, include_image=False))
    assert '完成一次拖动' in text and controller.reserve_calls == ['bypass']
    assert len(calls) == 2 and calls[-1] == 'finish'


@pytest.mark.parametrize('mode,captcha,quiz', [('quiz', True, False), ('captcha', False, True)])
def test_wrong_solve_mode_keeps_real_challenge_unresolved(monkeypatch, mode, captcha, quiz):
    _patch_vision(monkeypatch, {'content': '实际挑战', 'is_captcha': captcha,
                              'is_quiz': quiz, 'captcha_type': 'slider' if captcha else 'none'})
    controller = FakeController()
    service = FakeService(verification=controller)
    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent()), mode=mode))
    assert '类型与 mode 不匹配' in tools_mod._result_text(result)
    assert not service.executed_ops and len(controller.submitted) == 1
    assert controller.submitted[0].outcome != 'not_challenge'
    assert controller.submitted[0].outcome != 'cleared'


@pytest.mark.parametrize('mode', ['auto', 'captcha', 'quiz'])
def test_first_solve_observes_ordinary_page_without_starting_session(monkeypatch, outcome_api, mode):
    _patch_vision(monkeypatch, {'content': '普通评论页', 'is_captcha': False,
                              'is_quiz': False, 'captcha_type': 'none'})
    controller = FakeController()
    controller.active = False
    service = FakeService(verification=controller)
    text = tools_mod._result_text(run(_solve_tool(service).call(FakeToolContext(FakeEvent()), mode=mode)))
    assert '未检测到验证码/答题页面' in text
    assert not controller.state and not controller.reserve_calls and not controller.submitted
    assert not service.executed_ops


def test_first_solve_identification_timeout_does_not_pause_phone(monkeypatch):
    async def timeout(*a, **kw):
        raise TimeoutError('vision timed out')
    monkeypatch.setattr(tools_mod.solve, '_vision_json', timeout)
    monkeypatch.setattr(tools_mod.solve, '_resolve_ocr_provider', lambda *a: ('eyes', ''))
    controller = FakeController()
    controller.active = False
    service = FakeService(verification=controller)
    text = tools_mod._result_text(run(_solve_tool(service).call(FakeToolContext(FakeEvent()), mode='captcha')))
    assert '识图超时' in text
    assert not controller.state and not controller.reserve_calls and not controller.paused
    assert not service.executed_ops


def test_first_real_challenge_reuses_identification_before_bounded_attempt(monkeypatch, outcome_api):
    _patch_vision(monkeypatch, {'content': '滑块', 'is_captcha': True, 'is_quiz': False,
                              'captcha_type': 'slider'}, {'challenge_confirmed': True, 'ops': [{'action': 'tap', 'x': 10, 'y': 20}]})
    controller = FakeController()
    controller.active = False
    service = FakeService(verification=controller)
    run(_solve_tool(service).call(FakeToolContext(FakeEvent())))
    assert controller.reserve_calls == ['solver'] and len(controller.submitted) == 1
    assert len(service.executed_ops) == 1
    assert service.capture_calls == 3  # 首次识别、求解前新截图、操作后截图。


def test_stop_during_first_identification_prevents_new_attempt(monkeypatch, outcome_api):
    _patch_vision(monkeypatch, {'content': '滑块', 'is_captcha': True, 'is_quiz': False, 'captcha_type': 'slider'}, {'challenge_confirmed': True, 'ops': [{'action': 'tap', 'x': 10, 'y': 20}]})
    controller = FakeController()
    controller.active = False
    service = FakeService(verification=controller)
    def stopped(operation):
        raise ExecutionCancelled('用户已停止')
    service.guard_device = stopped
    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent())))
    assert '已中止' in tools_mod._result_text(result)
    assert not controller.reserve_calls and not controller.state and not service.executed_ops


@pytest.mark.parametrize('second', [
    {'challenge_confirmed': False, 'ops': [{'action': 'tap', 'x': 600, 'y': 1060}]},
    {'ops': [{'action': 'tap', 'x': 600, 'y': 1060}]},
    {'challenge_confirmed': 'true', 'ops': [{'action': 'tap', 'x': 600, 'y': 1060}]},
])
def test_first_false_positive_needs_independent_boolean_confirmation(monkeypatch, second):
    _patch_vision(monkeypatch, {'content': '普通文章和SAFE TAP按钮', 'is_captcha': True,
                              'is_quiz': False, 'captcha_type': 'slider'}, second)
    controller = FakeController()
    controller.active = False
    service = FakeService(verification=controller)
    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent()), mode='captcha'))
    text = tools_mod._result_text(result)
    assert '未执行操作' in text
    assert not service.executed_ops and not controller.reserve_calls and not controller.state
    assert not controller.submitted and not controller.paused


def test_second_identification_timeout_does_not_start_attempt(monkeypatch):
    calls = []
    async def vision(context, provider, prompt, *args, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            return {'content': '疑似滑块', 'is_captcha': True, 'is_quiz': False, 'captcha_type': 'slider'}, ''
        raise TimeoutError('second classification timeout')
    monkeypatch.setattr(tools_mod.solve, '_vision_json', vision)
    monkeypatch.setattr(tools_mod.solve, '_resolve_ocr_provider', lambda *a: ('eyes', ''))
    controller = FakeController()
    controller.active = False
    service = FakeService(verification=controller)
    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent())))
    assert '求解识图超时' in tools_mod._result_text(result)
    assert len(calls) == 2 and not controller.reserve_calls and not controller.state
    assert not service.executed_ops


@pytest.mark.parametrize('confirmed', [False, True])
def test_uncertain_first_type_is_rechecked_without_guessing_actions(monkeypatch, confirmed):
    _patch_vision(monkeypatch, {'content': 'SAFE TAP普通按钮', 'is_captcha': True,
                              'is_quiz': False, 'captcha_type': 'none'},
                  {'challenge_confirmed': confirmed, 'ops': [{'action': 'tap', 'x': 600, 'y': 1060}]})
    controller = FakeController()
    controller.active = False
    service = FakeService(verification=controller)
    result = run(_solve_tool(service).call(FakeToolContext(FakeEvent()), mode='captcha'))
    text = tools_mod._result_text(result)
    assert ('未检测到验证码/答题页面' if not confirmed else '未识别出明确的验证码类型') in text
    assert service.capture_calls == 2
    assert not controller.reserve_calls and not controller.state and not service.executed_ops
