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


def test_aliyun_audio_transport_checks_host_boundary():
    class ProviderOpenAIOfficial:
        def __init__(self, host):
            self.provider_config = {
                "api_base": "https://" + host + "/compatible-mode/v1"
            }

    assert (
        audio_transport(
            ProviderOpenAIOfficial("workspace.cn-beijing.maas.aliyuncs.com")
        )
        == "aliyun_openai"
    )
    assert (
        audio_transport(ProviderOpenAIOfficial("dashscope.aliyuncs.com"))
        == "aliyun_openai"
    )
    assert (
        audio_transport(ProviderOpenAIOfficial("maas.aliyuncs.com.attacker.invalid"))
        == "native"
    )


def test_video_transport_preserves_real_video_content_and_rejects_other_adapters(tmp_path):
    import asyncio
    import base64
    from core.media_provider import MediaProviders
    async def run():
        calls = []
        class ProviderOpenAIOfficial:
            provider_config = {"api_base": "https://example.invalid/v1"}
        class Context:
            def get_provider_by_id(self, id):
                return ProviderOpenAIOfficial() if id == 'video' else SimpleNamespace()
            async def llm_generate(self, **kwargs):
                calls.append(kwargs)
                return 'answer'
        path = tmp_path/'segment.mp4'
        path.write_bytes(b'real-test-video-bytes')
        providers = MediaProviders(Context())
        assert await providers.generate(chat_provider_id='video', prompt='0–3秒', video_path=path) == 'answer'
        content = calls[0]['contexts'][0]['content']
        assert content[0]['text'] == '0–3秒'
        assert base64.b64decode(content[1]['video_url']['url'].split(',')[1]) == path.read_bytes()
        assert calls[0]['prompt'] is None and 'video_path' not in calls[0]
        with pytest.raises(PhoneControlError, match='OpenAI'):
            await providers.generate(chat_provider_id='other', prompt='clip', video_path=path)
        assert len(calls) == 1
    asyncio.run(run())
