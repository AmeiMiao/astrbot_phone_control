"""媒体模型传输：保持模型身份，为原音频选择可表达该模态的协议。"""

from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

from .errors import PhoneControlError


def audio_adapter_config(provider):
    """小米 Token Plan 原生音频使用同源 OpenAI 协议，保留认证和模型参数。"""
    config = deepcopy(provider.provider_config)
    parts = urlsplit(str(config.get("api_base") or ""))
    if (
        parts.scheme != "https"
        or not parts.hostname
        or not parts.hostname.endswith(".xiaomimimo.com")
        or parts.path.rstrip("/") != "/anthropic"
    ):
        raise PhoneControlError("小米音频接口地址未通过同源协议校验")
    config["api_base"] = urlunsplit((parts.scheme, parts.netloc, "/v1", "", ""))
    config["type"] = "openai_chat_completion"
    extra = deepcopy(config.get("custom_extra_body") or {})
    extra["thinking"] = {"type": "disabled"}
    config["custom_extra_body"] = extra
    return config


def audio_transport(provider):
    names = {base.__name__ for base in type(provider).__mro__}
    if "ProviderXiaomiTokenPlan" in names:
        return "xiaomi_openai"
    if "ProviderAnthropic" in names:
        return "transcript"
    return "native"


class MediaProviders:
    def __init__(self, context):
        self.context = context
        self.adapters = {}

    def provider(self, provider_id):
        getter = getattr(self.context, "get_provider_by_id", None)
        return getter(provider_id) if callable(getter) else None

    async def generate(self, **kwargs):
        provider_id = kwargs["chat_provider_id"]
        selected = self.provider(provider_id)
        if kwargs.get("audio_urls") and audio_transport(selected) == "xiaomi_openai":
            adapter = self.adapters.get(provider_id)
            if adapter is None:
                from astrbot.core.provider.sources.openai_source import (
                    ProviderOpenAIOfficial,
                )

                adapter = ProviderOpenAIOfficial(
                    audio_adapter_config(selected),
                    getattr(selected, "provider_settings", {}),
                )
                self.adapters[provider_id] = adapter
            payload = {k: v for k, v in kwargs.items() if k != "chat_provider_id"}
            return await adapter.text_chat(**payload)
        return await self.context.llm_generate(**kwargs)

    async def close(self):
        adapters = list(self.adapters.values())
        self.adapters.clear()
        for adapter in adapters:
            await adapter.terminate()
