"""金融分类、识别失败和应用启动的实际共用入口。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.actions import DeviceActions
from core.app_policy import AppPolicy, AppPolicyError
from core.config import PhoneConfig


def make_policy(category="finance", confidence=0.95):
    commands, calls = [], []
    state = {
        "version": "1",
        "category": category,
        "confidence": confidence,
        "broken": False,
    }

    async def shell(command, **kwargs):
        commands.append(command)
        if command.startswith("pm list packages"):
            return "package:com.example.app"
        if command.startswith("dumpsys package"):
            return (
                "    versionCode="
                + state["version"]
                + " minSdk=23\n    versionName=1.0"
            )
        return "Events injected: 1"

    async def generate(**kwargs):
        calls.append(kwargs)
        if state["broken"]:
            raise RuntimeError("model offline")
        return SimpleNamespace(
            completion_text=json.dumps(
                dict(
                    category=state["category"],
                    confidence=state["confidence"],
                    reason="根据应用主要用途分类",
                )
            ),
            usage=None,
        )

    async def provider(_):
        return SimpleNamespace(meta=lambda: SimpleNamespace(id="test-model"))

    adb = SimpleNamespace(
        shell=shell, ensure_ready=lambda: asyncio.sleep(0), serial="test-device"
    )
    service = SimpleNamespace(
        adb=adb,
        config=PhoneConfig.from_mapping({}),
        app_context=SimpleNamespace(
            llm_generate=generate, get_using_provider_async=provider
        ),
        record_internal_usage=lambda *a, **k: None,
    )
    policy = AppPolicy(service)
    actions = DeviceActions(adb)
    actions.app_open_guard = policy.require_allowed
    return policy, actions, commands, calls, state


@pytest.mark.parametrize(
    "category,confidence",
    [("finance", 0.99), ("finance", 0.2), ("unknown", 0.99), ("tools", 0.3)],
)
def test_denied_classifications_never_launch_app(category, confidence):
    async def run():
        policy, actions, commands, calls, state = make_policy(category, confidence)
        with pytest.raises(AppPolicyError, match="拒绝打开"):
            await actions.open_app("com.example.app")
        assert not any("monkey" in c or "am start" in c for c in commands)
        assert len(calls) == 1

    asyncio.run(run())


def test_non_financial_launch_and_version_device_cache_invalidation():
    async def run():
        policy, actions, commands, calls, state = make_policy("tools")
        await actions.open_app("com.example.app")
        await actions.open_app("com.example.app")
        assert len(calls) == 1
        assert sum("monkey" in c for c in commands) == 2
        state["version"] = "2"
        state["category"] = "finance"
        with pytest.raises(AppPolicyError, match="金融类"):
            await actions.open_app("com.example.app")
        assert len(calls) == 2
        policy.service.adb.serial = "other-device"
        with pytest.raises(AppPolicyError):
            await actions.open_app("com.example.app")
        assert len(calls) == 3

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure",
    [
        "no_context",
        "no_provider",
        "exception",
        "bad_json",
        "missing_metadata",
        "invalid_confidence",
    ],
)
def test_failed_classification_is_closed_and_not_cached(failure):
    async def run():
        policy, actions, commands, calls, state = make_policy("tools")
        if failure == "no_context":
            policy.service.app_context = None
        elif failure == "no_provider":
            policy.service.app_context.get_using_provider_async = lambda _: (
                asyncio.sleep(0, result=None)
            )
        elif failure == "exception":
            state["broken"] = True
        elif failure == "bad_json":
            policy.service.app_context.llm_generate = lambda **_: asyncio.sleep(
                0, result=SimpleNamespace(completion_text="not json")
            )
        elif failure == "missing_metadata":

            async def shell(cmd, **kwargs):
                commands.append(cmd)
                return "package:com.example.app" if cmd.startswith("pm list packages") else ""

            policy.service.adb.shell = shell
        elif failure == "invalid_confidence":
            state["confidence"] = True
        with pytest.raises(AppPolicyError):
            await actions.open_app("com.example.app")
        assert not policy.cache
        assert not any("monkey" in c for c in commands)

    asyncio.run(run())
