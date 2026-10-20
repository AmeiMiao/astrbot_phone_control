"""core.agent_usage_patch 单元测试：runner stats → 事件 extra 的累计 usage 转换。"""

from __future__ import annotations

from core import agent_usage_patch as patch


class _FakeUsage:
    def __init__(self, input_other=0, input_cached=0, output=0):
        self.input_other = input_other
        self.input_cached = input_cached
        self.output = output


class _FakeStats:
    def __init__(self, usage):
        self.token_usage = usage


class _FakeProvider:
    def __init__(self, model="mimo-v2.6-flash"):
        self._model = model

    def get_model(self):
        return self._model


def test_usage_payload_from_agent_stats():
    payload = patch._usage_payload(
        _FakeStats(_FakeUsage(100, 20, 5)), _FakeProvider()
    )
    assert payload == {
        "input_other": 100,
        "input_cached": 20,
        "output": 5,
        "model": "mimo-v2.6-flash",
        "source": "agent_runner_stats",
    }


def test_usage_payload_empty_returns_none():
    assert patch._usage_payload(_FakeStats(_FakeUsage())) is None
    assert patch._usage_payload(None) is None


def test_usage_payload_handles_missing_attrs():
    class _BrokenStats:
        token_usage = object()

    assert patch._usage_payload(_BrokenStats()) is None


def test_install_is_idempotent_and_uninstall_safe():
    # 本地无 astrbot 时安装降级返回 False；容器有 astrbot 时返回 True。
    # 两种情况都不能抛异常，且重复安装/卸载必须安全。
    installed = patch.install_agent_usage_capture()
    assert installed in (True, False)
    assert patch.install_agent_usage_capture() is installed
    patch.uninstall_agent_usage_capture()
