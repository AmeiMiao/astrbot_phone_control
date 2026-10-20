"""刷手机/学习模型供应商（model.provider_id）与 schema 卡测试。"""

from __future__ import annotations

import json
from pathlib import Path

from core.config import PhoneConfig


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
