"""LivingMemory 作用域适配：调用已加载插件的同一套读写与访问规则。"""

from importlib import import_module

from .errors import PhoneControlError


class _InitiatorEvent:
    """后台用户任务保留来源会话与平台，以发起人身份解析用户作用域。"""

    def __init__(self, event, user_id):
        self._event = event
        self._user_id = user_id

    def get_sender_id(self):
        return self._user_id

    def get_sender_name(self):
        return self._user_id

    def __getattr__(self, key):
        return getattr(self._event, key)


def resolve_livingmemory_scope(plugin, event, payload):
    """复用 LivingMemory 的白名单、身份别名、隔离会话与作用域解析。"""
    config = getattr(plugin, "config_manager", None)
    if config is None:
        raise PhoneControlError("LivingMemory 作用域配置未就绪")
    package = type(plugin).__module__.rsplit(".", 1)[0]
    try:
        rules = import_module(f"{package}.core.memory_scope")
        allowed = rules.is_event_memory_allowed
        resolve = rules.resolve_memory_scope
    except (ImportError, AttributeError) as exc:
        raise PhoneControlError("LivingMemory 未提供作用域接口，请更新该插件") from exc
    meta = payload.get("payload") or {}
    initiator = str(meta.get("initiator_user_id") or meta.get("requested_by") or "").strip()
    if initiator and initiator != str(event.get_sender_id() or "").strip():
        event = _InitiatorEvent(event, initiator)
    if not allowed(config, event):
        raise PhoneControlError("LivingMemory 访问规则拒绝该来源记忆")
    # 与 LivingMemory 的召回入口一致，空作用域使用来源会话。
    return resolve(config, event) or str(getattr(event, "unified_msg_origin", "") or "").strip() or None
