"""A7 — Konfiguration: Defaults, ungültige Werte, echte config.yaml bleibt unverändert"""

import copy
import hashlib
import logging
from datetime import datetime

import pytest

import main as main_mod
from services.hardware import DAC_DEFAULTS, load_dac_settings
from services.pump_service import PumpService
from services.telemetry import TELEMETRY_DEFAULTS, load_telemetry_config
from tests.conftest import CONFIG_PATH, load_real_config


def test_real_config_has_new_sections_and_mqtt_untouched():
    cfg = load_real_config()
    assert cfg["dac"] == {"vdd": 3.3, "opamp_gain": 3.2, "max_output_v": 9.5,
                          "ramp_pct_per_s": 5, "refresh_interval_s": 0.2}
    assert cfg["pump"]["min_speed"] == {"mode": "winter", "speed": "min",
                                        "winter_months": [11, 12, 1, 2, 3]}
    assert cfg["telemetry"]["enabled"] is False
    # bestehende Werte unverändert
    assert cfg["pump"]["dac_full_scale_v"] == 10.483
    assert cfg["pump"]["speeds"]["min"] == 20
    assert set(cfg["mqtt"]) >= {"broker", "port", "username", "topic_prefix"}
    assert "password" not in cfg["mqtt"]


def test_loading_never_writes_config(config):
    before = hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
    cfg = main_mod.load_config()
    main_mod.apply_env_secrets(cfg)
    load_dac_settings(cfg)
    load_telemetry_config(cfg)
    assert hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest() == before


def test_missing_optional_sections_use_defaults(config, make_hw):
    cfg = copy.deepcopy(config)
    for key in ("dac", "telemetry"):
        cfg.pop(key)
    cfg["pump"].pop("min_speed")
    for k in ("dac_full_scale_v", "dac_offset_v", "input_full_scale_v"):
        cfg["pump"].pop(k)
    dac = load_dac_settings(cfg)
    for k, v in DAC_DEFAULTS.items():
        assert getattr(dac, k) == v
    assert load_telemetry_config(cfg) == TELEMETRY_DEFAULTS
    hw = make_hw(cfg)
    svc = PumpService(hw, cfg, now=lambda: datetime(2026, 1, 7, 2, 0))
    assert svc._min_speed_cfg["mode"] == "off"          # ohne Abschnitt: wie bisher
    assert svc._get_scheduled_speed().speed_pct == 0


@pytest.mark.parametrize("ms,expect_mode,expect_months", [
    ({"mode": "bogus"}, "off", None),
    ({"mode": "winter", "speed": "gibts-nicht"}, "off", None),
    ({"mode": "always", "speed": "off"}, "off", None),                # 0 % ist keine Mindestdrehzahl
    ({"mode": "winter", "winter_months": [0, 13, "x", 12, True]}, "winter", {12}),
    ({"mode": "winter", "winter_months": "11,12"}, "winter", set()),
    ({"mode": "WINTER"}, "winter", {11, 12, 1, 2, 3}),
])
def test_invalid_min_speed_values(config, make_hw, caplog, ms, expect_mode, expect_months):
    cfg = copy.deepcopy(config)
    cfg["pump"]["min_speed"] = ms
    with caplog.at_level(logging.WARNING):
        svc = PumpService(make_hw(cfg), cfg)
    assert svc._min_speed_cfg["mode"] == expect_mode
    if expect_months is not None:
        assert svc._min_speed_cfg["months"] == expect_months


@pytest.mark.parametrize("key,value,expect", [
    ("vdd", 5.0, 5.0), ("vdd", 12, 3.3), ("refresh_interval_s", 0, 0.2),
    ("refresh_interval_s", 10, 0.2), ("refresh_interval_s", 0.5, 0.5),
    ("opamp_gain", -1, 3.2), ("ramp_pct_per_s", 1000, 5.0), ("ramp_pct_per_s", 2, 2.0),
])
def test_dac_value_validation(config, key, value, expect):
    cfg = copy.deepcopy(config)
    cfg["dac"][key] = value
    cfg["pump"].pop("dac_full_scale_v")
    assert getattr(load_dac_settings(cfg), key) == expect


@pytest.mark.parametrize("fs,offset,calibrated,expect_offset", [
    (5.0, 0.0, False, 0.0),          # unplausibel weit weg vom Nennwert
    ("abc", 0.0, False, 0.0),
    (10.4, 2.0, True, 0.0),          # Offset unplausibel → 0
    (10.4, "x", True, 0.0),
    (10.4, -0.05, True, -0.05),
])
def test_calibration_sanity(config, caplog, fs, offset, calibrated, expect_offset):
    cfg = copy.deepcopy(config)
    cfg["pump"]["dac_full_scale_v"] = fs
    cfg["pump"]["dac_offset_v"] = offset
    dac = load_dac_settings(cfg)
    assert dac.calibrated is calibrated and dac.offset_v == expect_offset
    assert dac.code_to_voltage(dac.pct_to_code(100)) <= dac.max_output_v + 1e-9


def test_apply_env_secrets(monkeypatch, config):
    cfg = copy.deepcopy(config)
    monkeypatch.setenv("POOL_MQTT_PASSWORD", "pw")
    monkeypatch.delenv("POOL_API_SECRET", raising=False)
    cfg["api"]["secret_key"] = "legacy-in-yaml"
    legacy = main_mod.apply_env_secrets(cfg)
    assert cfg["mqtt"]["password"] == "pw" and legacy == ["api.secret_key"]
