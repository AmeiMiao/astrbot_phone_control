"""原音频协议适配保持同源、模型和凭据，且不修改宿主配置。"""

from types import SimpleNamespace
import pytest
from core.media_provider import audio_adapter_config, audio_transport
from core.errors import PhoneControlError


def test_xiaomi_audio_protocol_keeps_source_and_model():
    config = {
        "api_base": "https://token-plan-cn.xiaomimimo.com/anthropic",
        "key": ["test-only-key"],
        "model": "mimo-v2.6-pro",
        "custom_headers": {"Authorization": "test-only-header"},
    }
    result = audio_adapter_config(SimpleNamespace(provider_config=config))
    assert result["api_base"] == "https://token-plan-cn.xiaomimimo.com/v1"
    assert result["key"] == config["key"] and result["model"] == config["model"]
    result["key"].append("local-change")
    assert config["key"] == ["test-only-key"] and config["api_base"].endswith(
        "/anthropic"
    )


def test_native_audio_transport_does_not_send_credentials_to_other_host():
    with pytest.raises(PhoneControlError):
        audio_adapter_config(
            SimpleNamespace(
                provider_config={
                    "api_base": "https://xiaomimimo.com.attacker.invalid/anthropic"
                }
            )
        )


def test_placeholder_adapter_and_native_xiaomi_are_distinguished():
    class ProviderAnthropic:
        pass

    class ProviderXiaomiTokenPlan(ProviderAnthropic):
        pass

    assert audio_transport(ProviderAnthropic()) == "transcript"
    assert audio_transport(ProviderXiaomiTokenPlan()) == "xiaomi_openai"
