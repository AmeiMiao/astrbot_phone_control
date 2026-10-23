"""跨插件作用域必须经过记忆插件规则，拒绝条件下不解析或写入。"""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from core.errors import PhoneControlError
from core.livingmemory_scope import resolve_livingmemory_scope


class MemoryPlugin:
    __module__ = "scope_test.main"

    def __init__(self):
        self.config_manager = {"rules": "configured"}


def _setup(monkeypatch, scope="resolved-user-scope", allowed=True):
    rules = ModuleType("scope_test.core.memory_scope")
    rules.is_event_memory_allowed = Mock(return_value=allowed)
    rules.resolve_memory_scope = Mock(return_value=scope)
    monkeypatch.setitem(sys.modules, rules.__name__, rules)
    event = SimpleNamespace(
        unified_msg_origin="onebot:FriendMessage:123", get_sender_id=lambda: "123",
        get_platform_name=lambda: "onebot", get_sender_name=lambda: "name",
    )
    return MemoryPlugin(), event, rules


@pytest.mark.parametrize("scope", ["livingmemory:user:onebot:123", "livingmemory:global", "onebot:GroupMessage:group"])
def test_delegates_scope_to_loaded_plugin(monkeypatch, scope):
    plugin, event, rules = _setup(monkeypatch, scope)
    assert resolve_livingmemory_scope(plugin, event, {}) == scope
    rules.is_event_memory_allowed.assert_called_once_with(plugin.config_manager, event)
    rules.resolve_memory_scope.assert_called_once_with(plugin.config_manager, event)


def test_empty_scope_matches_livingmemory_recall_session_fallback(monkeypatch):
    plugin, event, _ = _setup(monkeypatch, None)
    assert resolve_livingmemory_scope(plugin, event, {}) == event.unified_msg_origin


def test_denied_source_never_resolves(monkeypatch):
    plugin, event, rules = _setup(monkeypatch, allowed=False)
    with pytest.raises(PhoneControlError, match="访问规则拒绝"):
        resolve_livingmemory_scope(plugin, event, {})
    rules.resolve_memory_scope.assert_not_called()


def test_background_task_uses_initiator_with_original_session(monkeypatch):
    plugin, event, rules = _setup(monkeypatch)
    resolve_livingmemory_scope(plugin, event, {"payload": {"requested_by": "456"}})
    proxy = rules.resolve_memory_scope.call_args.args[1]
    assert proxy.get_sender_id() == proxy.get_sender_name() == "456"
    assert proxy.get_platform_name() == "onebot"
    assert proxy.unified_msg_origin == event.unified_msg_origin
    assert rules.is_event_memory_allowed.call_args.args[1] is proxy
    assert event.get_sender_id() == "123"


def test_missing_rules_fail_explicitly(monkeypatch):
    plugin, event, _ = _setup(monkeypatch)
    monkeypatch.delitem(sys.modules, "scope_test.core.memory_scope")
    with pytest.raises(PhoneControlError, match="作用域接口"):
        resolve_livingmemory_scope(plugin, event, {})
    plugin.config_manager = None
    with pytest.raises(PhoneControlError, match="配置未就绪"):
        resolve_livingmemory_scope(plugin, event, {})
