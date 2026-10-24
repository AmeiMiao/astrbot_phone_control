"""刷手机/学习模型供应商（model.provider_id）与 schema 卡测试。"""

from __future__ import annotations

import json
from pathlib import Path

from core.config import PhoneConfig


def test_models_migrate_before_framework_schema_pruning_and_preserve_backup(tmp_path):
    from core.model_config import migrate_model_file, migrate_models
    original = {"model": {"asr_provider_id": "new-stt"}, "media": {"asr_provider_id": "old-stt", "vision_provider_id": "vision"}, "perception": {"ocr_provider_id": "ocr"}, "prompt": {"app_memory_embedding_provider_id": "app-embed"}, "recall": {"embedding_provider_id": "chat-embed"}, "auto": {"share_target": "old-target"}}
    path = tmp_path / "plugin_config.json"
    content = json.dumps(original).encode()
    path.write_bytes(content)
    assert migrate_model_file(path)
    assert path.with_name(path.name + ".models-backup").read_bytes() == content
    raw = json.loads(path.read_text())
    assert raw["model"] == {"asr_provider_id": "new-stt", "vision_provider_id": "vision", "app_memory_embedding_provider_id": "app-embed"}
    assert raw["memory"]["embedding_provider_id"] == "chat-embed"
    assert not raw["media"] and not raw["perception"] and not raw["auto"]
    assert not migrate_model_file(path) and not migrate_models(raw)
    config = PhoneConfig.from_mapping(raw)
    assert config.perception.ocr_provider_id == config.media.vision_provider_id == "vision"
    assert config.model.asr_provider_id == config.media.asr_provider_id == "new-stt"
    assert config.prompt.app_memory_embedding_provider_id == "app-embed"
    assert config.recall.embedding_provider_id == "chat-embed"


def test_app_embedding_inherits_shared_selection_and_can_override():
    config = PhoneConfig.from_mapping({"model": {"embedding_provider_id": "embed", "app_memory_embedding_provider_id": ""}})
    assert config.prompt.app_memory_embedding_provider_id == "embed"
    override = PhoneConfig.from_mapping({"model": {"embedding_provider_id": "embed", "app_memory_embedding_provider_id": "app-embed"}})
    assert override.prompt.app_memory_embedding_provider_id == "app-embed"
    assert override.recall.embedding_provider_id == "embed"


def test_share_target_is_selected_from_chat_instead_of_unused_configuration():
    config = PhoneConfig.from_mapping({"auto": {"share_target": "unused:FriendMessage:1"}})
    assert not hasattr(config.auto, "share_target")
    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text())
    assert "share_target" not in schema["auto"]["items"]


def test_embedding_dropdown_uses_actual_embedding_providers():
    from types import SimpleNamespace
    from core.model_config import populate_embedding_options
    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text())
    config = SimpleNamespace(schema=schema)
    context = SimpleNamespace(get_all_embedding_providers=lambda: [SimpleNamespace(provider_config={"id": "embedding-b"}), SimpleNamespace(provider_config={"id": "embedding-a"})])
    populate_embedding_options(config, context)
    for group, key in (("memory", "embedding_provider_id"), ("model", "app_memory_embedding_provider_id")):
        assert schema[group]["items"][key]["options"] == ["", "embedding-a", "embedding-b"]
    for group, key in (("media", "asr_provider_id"), ("media", "vision_provider_id"), ("perception", "ocr_provider_id"), ("prompt", "app_memory_embedding_provider_id"), ("recall", "embedding_provider_id"), ("auto", "share_target")):
        assert key not in schema[group]["items"]


def test_model_provider_defaults_empty():
    config = PhoneConfig.from_mapping(None)
    assert config.model.provider_id == ""


def test_model_provider_parsed_and_trimmed():
    config = PhoneConfig.from_mapping(
        {"model": {"provider_id": "  deepseek-responses/deepseek-flash  "}}
    )
    assert config.model.provider_id == "deepseek-responses/deepseek-flash"


def test_conf_schema_model_card_uses_provider_selector():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    model = schema["model"]
    assert model["type"] == "object"
    provider = model["items"]["provider_id"]
    assert provider["type"] == "string"
    assert provider["default"] == ""
    assert provider["_special"] == "select_provider"


def test_config_field_paths_include_model_provider():
    from core.config_apply import LIVE_KEYS, RECONCILE_KEYS, config_field_paths

    fields = config_field_paths()
    assert "model.provider_id" in fields
    assert "model.provider_id" in LIVE_KEYS
    assert "model.provider_id" not in RECONCILE_KEYS


def test_memory_embedding_migration_preserves_explicit_selection_and_visibility():
    from core.model_config import migrate_models
    raw = {"memory": {"local_enabled": False, "embedding_provider_id": "current"},
           "model": {"embedding_provider_id": "previous"},
           "control": {"minitouch": {"binary_path": "/touch"}}}
    migrate_models(raw)
    assert raw["memory"]["embedding_provider_id"] == "current"
    assert "embedding_provider_id" not in raw["model"]
    assert raw["control"]["minitouch_enabled"] is True
    assert not migrate_models(raw)
    parsed = PhoneConfig.from_mapping(raw)
    assert parsed.recall.embedding_provider_id == "current"
    assert parsed.prompt.app_memory_embedding_provider_id == "current"
    assert parsed.control.minitouch.binary_path == "/touch"
    raw["control"]["minitouch_enabled"] = False
    assert PhoneConfig.from_mapping(raw).control.minitouch.binary_path == ""
    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text())
    assert schema["memory"]["items"]["embedding_provider_id"]["condition"] == {"local_enabled": True}
    assert schema["control"]["items"]["scrcpy"]["condition"] == {"backend": "scrcpy"}
    assert schema["control"]["items"]["minitouch"]["condition"] == {"minitouch_enabled": True}
    assert all(item["condition"] == {"enabled": True}
               for key, item in schema["traffic"]["items"].items() if key != "enabled")
