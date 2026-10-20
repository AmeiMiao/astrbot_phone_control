"""插件生命周期回归测试：terminate 可被 AstrBot 探测、旧实例被关闭。

AstrBot 的 ``PluginManager._terminate_plugin`` 只检查插件类**自身**
``__dict__`` 里是否有 ``terminate``（定义在 mixin 里的不会被调用），旧实例的
keepalive/抓包循环因此在 reload 后继续运行。本文件覆盖：

* 本机（无真实 AstrBot）：用 AST + 与 AstrBot 探测规则等价的类行为测试，断言
  ``PhoneControlPlugin`` 类体内声明了 terminate 且委托 ``super().terminate()``；
* AstrBot 容器内：加载真实插件入口类，断言 ``"terminate" in
  PhoneControlPlugin.__dict__``；并验证存活实例登记表会把旧实例关闭、遗留后台
  循环会被取消。
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import sys
import types
from pathlib import Path

import pytest

_astrbot = pytest.importorskip("astrbot")
_IS_LOCAL_STUB = bool(getattr(_astrbot, "_acat_local_stub", False))

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = PLUGIN_ROOT / "main.py"
BOOTSTRAP_PATH = PLUGIN_ROOT / "core" / "plugin" / "bootstrap.py"
RUNTIME_PATH = PLUGIN_ROOT / "core" / "plugin" / "runtime.py"


def _class_node(path: Path, name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 缺少类 {name}")


# ---- 本机可跑：AST 与探测规则等价测试 -------------------------------------


def test_plugin_class_defines_terminate_in_own_body() -> None:
    """AstrBot 只探测类自身 __dict__：terminate 必须写在 PhoneControlPlugin 类体内。"""
    klass = _class_node(MAIN_PATH, "PhoneControlPlugin")
    methods = [
        node
        for node in klass.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    terminate = [node for node in methods if node.name == "terminate"]
    assert terminate, "PhoneControlPlugin 类体内必须定义 terminate"
    assert isinstance(terminate[0], ast.AsyncFunctionDef), "terminate 必须是 async"
    # 必须委托给 BootstrapMixin.terminate，而不是空实现。
    calls = [node for node in ast.walk(terminate[0]) if isinstance(node, ast.Call)]
    delegated = any(
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "terminate"
        and isinstance(call.func.value, ast.Call)
        and isinstance(call.func.value.func, ast.Name)
        and call.func.value.func.id == "super"
        for call in calls
    )
    assert delegated, "terminate 必须 await super().terminate() 委托真实停用逻辑"


def test_bootstrap_initialize_closes_stale_instances() -> None:
    """新实例 activate 时必须先关闭旧实例并清理遗留后台循环。"""
    klass = _class_node(BOOTSTRAP_PATH, "BootstrapMixin")
    initialize = next(
        (
            node
            for node in klass.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "initialize"
        ),
        None,
    )
    assert initialize is not None
    dumped = ast.dump(initialize)
    assert "close_stale_instances" in dumped
    assert "cancel_stale_background_tasks" in dumped


def test_bootstrap_terminate_is_idempotent() -> None:
    klass = _class_node(BOOTSTRAP_PATH, "BootstrapMixin")
    terminate = next(
        (
            node
            for node in klass.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "terminate"
        ),
        None,
    )
    assert terminate is not None
    dumped = ast.dump(terminate)
    assert "_terminated" in dumped
    assert "LIVE_PLUGIN_INSTANCES" in dumped


def test_runtime_keepalive_respects_unavailable_and_backoff() -> None:
    """保活循环不能绕过确定性不可用/退避状态反复重试。"""
    tree = ast.parse(RUNTIME_PATH.read_text(encoding="utf-8"))
    keepalive = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_traffic_keepalive_loop":
            keepalive = node
            break
    assert keepalive is not None
    dumped = ast.dump(keepalive)
    assert "unavailable" in dumped
    assert "retry_ready" in dumped


def _astrbot_would_have_terminate(star_cls_type: type) -> bool:
    """复刻 astrbot/core/star/star_manager.py::_terminate_plugin 的探测规则。"""
    if "__del__" in star_cls_type.__dict__:
        return False
    return "terminate" in star_cls_type.__dict__


def test_astrbot_detection_requires_terminate_in_own_dict() -> None:
    class Mixin:
        async def terminate(self) -> None: ...

    class OnlyMixin(Mixin):
        """修复前的结构：terminate 只在 mixin 里。"""

    class WithOwn(Mixin):
        async def terminate(self) -> None:
            await super().terminate()

    # 缺陷复现：mixin 里的 terminate 探测不到；类体内声明后探测得到。
    assert _astrbot_would_have_terminate(OnlyMixin) is False
    assert _astrbot_would_have_terminate(WithOwn) is True


def test_astrbot_detection_dispatches_to_subclass_terminate() -> None:
    called: list[str] = []

    class Mixin:
        async def terminate(self) -> None:
            called.append("mixin")

    class WithOwn(Mixin):
        async def terminate(self) -> None:
            called.append("plugin")
            await super().terminate()

    assert _astrbot_would_have_terminate(WithOwn) is True

    async def _run() -> None:
        # 复刻 AstrBot 的调用：star_metadata.star_cls.terminate()
        await WithOwn().terminate()

    asyncio.run(_run())
    assert called == ["plugin", "mixin"]


# ---- AstrBot 容器内：真实类与登记表行为 -----------------------------------


def _load_plugin_main() -> types.ModuleType:
    plugin_dir = PLUGIN_ROOT
    package_name = "astrbot_phone_control_under_test_terminate"
    module_name = f"{package_name}.main"
    if module_name in sys.modules:
        return sys.modules[module_name]
    package = types.ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(module_name, plugin_dir / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(_IS_LOCAL_STUB, reason="需要真实 AstrBot 运行环境")
def test_real_plugin_class_terminate_detected_by_astrbot() -> None:
    module = _load_plugin_main()
    # 与 AstrBot 完全相同的断言路径：类对象自身的 __dict__。
    assert "terminate" in module.PhoneControlPlugin.__dict__
    assert inspect.iscoroutinefunction(module.PhoneControlPlugin.terminate)


@pytest.mark.skipif(_IS_LOCAL_STUB, reason="需要真实 AstrBot 运行环境")
def test_close_stale_instances_terminates_old_instance() -> None:
    from core.plugin.bootstrap import LIVE_PLUGIN_INSTANCES, close_stale_instances

    closed: list[str] = []

    class _OldInstance:
        async def terminate(self) -> None:
            closed.append("closed")

    old = _OldInstance()
    current = object()
    LIVE_PLUGIN_INSTANCES.add(old)
    try:

        async def _run() -> int:
            return await close_stale_instances(current, timeout=5)

        assert asyncio.run(_run()) == 1
    finally:
        LIVE_PLUGIN_INSTANCES.discard(old)
    assert closed == ["closed"]
    assert old not in LIVE_PLUGIN_INSTANCES


@pytest.mark.skipif(_IS_LOCAL_STUB, reason="需要真实 AstrBot 运行环境")
def test_cancel_stale_background_tasks_stops_old_loops() -> None:
    from core.plugin.bootstrap import cancel_stale_background_tasks

    async def _run() -> None:
        async def _loop() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(
            _loop(), name="astrbot_phone_control_traffic_keepalive"
        )
        await asyncio.sleep(0)
        stopped = await cancel_stale_background_tasks(timeout=5)
        assert stopped >= 1
        assert task.cancelled()

    asyncio.run(_run())
