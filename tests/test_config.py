"""Configuration persistence for clause 10 (settings) and 11 (bad input)."""
import json

import pytest

from voicetray import config as cfg
from voicetray import manifest
from conftest import MODEL_DIR


def test_defaults_are_valid_and_model_dir_is_local():
    c = cfg.Config()
    assert c.validate() == []
    assert c.hotkey == "rmenu"
    assert c.sample_rate == 16000
    assert c.language == "auto"
    assert c.use_uia is True
    assert c.model_dir.endswith(manifest.MODEL_ID)
    assert (MODEL_DIR / "model.int8.onnx").is_file()


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "cfg" / "config.json"
    c = cfg.Config(device=16, language="zh", num_threads=4,
                   max_record_seconds=30.0, allow_injected=False)
    assert c.save(p) == p
    loaded, warnings = cfg.Config.load(p)
    assert warnings == []
    assert loaded.device == 16
    assert loaded.language == "zh"
    assert loaded.num_threads == 4
    assert loaded.max_record_seconds == 30.0
    assert loaded.allow_injected is False


def test_missing_file_returns_defaults_without_warnings(tmp_path):
    c, warnings = cfg.Config.load(tmp_path / "nope.json")
    assert warnings == []
    assert c == cfg.Config()


def test_corrupt_json_falls_back_to_defaults(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ not json", encoding="utf-8")
    c, warnings = cfg.Config.load(p)
    assert c == cfg.Config()
    assert any("读取失败" in w for w in warnings)


def test_non_object_json_falls_back(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    c, warnings = cfg.Config.load(p)
    assert c == cfg.Config()
    assert any("不是对象" in w for w in warnings)


def test_unknown_keys_are_ignored_with_warning(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"hotkey": "rmenu", "future_option": 42}), "utf-8")
    c, warnings = cfg.Config.load(p)
    assert c.hotkey == "rmenu"
    assert any("future_option" in w for w in warnings)


def test_wrong_type_keeps_the_default_value(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"num_threads": "four", "use_uia": 1}), "utf-8")
    c, warnings = cfg.Config.load(p)
    assert c.num_threads == 2
    assert c.use_uia is True
    assert len(warnings) >= 2


def test_out_of_range_value_triggers_full_fallback(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"sample_rate": 999999}), "utf-8")
    c, warnings = cfg.Config.load(p)
    assert c.sample_rate == 16000
    assert any("整体回退" in w for w in warnings)


def test_save_refuses_invalid_config(tmp_path):
    with pytest.raises(cfg.ConfigError):
        cfg.Config(language="xx").save(tmp_path / "config.json")


def test_device_null_is_accepted(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"device": None}), "utf-8")
    c, warnings = cfg.Config.load(p)
    assert c.device is None
    assert warnings == []
