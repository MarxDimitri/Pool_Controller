"""A1 — DAC-Mapping Prozent → Code → Spannung"""

import copy
import logging
import math

import pytest

from services.hardware import (
    DAC_MAX, HardwareController, load_dac_settings,
)


@pytest.fixture
def dac(config):
    return load_dac_settings(config)


def lsb(dac):
    return dac.full_scale_v / DAC_MAX


def test_real_config_uses_measured_calibration(dac):
    assert dac.calibrated is True
    assert dac.vdd == 3.3 and dac.opamp_gain == 3.2
    assert dac.max_output_v == 9.5


def test_zero_percent_is_code_zero(dac):
    assert dac.pct_to_code(0) == 0
    assert dac.code_to_voltage(0) == 0.0


def test_hundred_percent_matches_max_output_never_more(dac):
    code = dac.pct_to_code(100)
    assert code == dac.max_code
    v = dac.code_to_voltage(code)
    assert v <= dac.max_output_v + 1e-9
    assert dac.max_output_v - v <= lsb(dac)        # aber höchstens 1 LSB darunter
    assert dac.code_to_voltage(code + 1) > dac.max_output_v


@pytest.mark.parametrize("pct", [20, 40, 60, 80])
def test_intermediate_voltages_within_one_lsb(dac, pct):
    expected = pct / 100 * dac.max_output_v
    got = dac.code_to_voltage(dac.pct_to_code(pct))
    assert abs(got - expected) <= lsb(dac)


def test_nominal_formula_without_calibration(config):
    cfg = copy.deepcopy(config)
    for k in ("dac_full_scale_v", "dac_offset_v"):
        cfg["pump"].pop(k, None)
    dac = load_dac_settings(cfg)
    assert dac.calibrated is False
    # Vorgabe: max_code = max_output_v / opamp_gain / dac_vdd * 4095
    assert dac.max_code == math.floor(9.5 / 3.2 / 3.3 * 4095)
    assert dac.pct_to_code(100) == dac.max_code
    for pct in range(0, 101):
        assert abs(dac.pct_to_code(pct) - round(pct / 100 * dac.max_code)) <= 1


def test_monotonic_over_full_range(dac):
    codes = [dac.pct_to_code(p) for p in range(0, 101)]
    assert all(b > a for a, b in zip(codes, codes[1:]))
    assert codes[0] == 0 and codes[-1] == dac.max_code


@pytest.mark.parametrize("pct", [-5, -0.01, 100.01, 150, 1e9])
def test_out_of_range_is_clamped(dac, pct):
    code = dac.pct_to_code(pct)
    assert 0 <= code <= dac.max_code
    assert code == (0 if pct < 0 else dac.max_code)


def test_nan_maps_to_zero_in_mapping(dac):
    assert dac.pct_to_code(float("nan")) == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "abc", None, True])
def test_setpoint_rejects_nan_and_garbage(make_hw, bad):
    hw = make_hw()
    with pytest.raises(ValueError):
        hw.pump_set_speed(bad)
    assert hw.pump_speed == 0


@pytest.mark.parametrize("pct,expected", [(-10, 0), (250, 100), (55.5, 55.5)])
def test_setpoint_clamped(make_hw, pct, expected):
    hw = make_hw()
    hw.pump_set_speed(pct)
    assert hw._dac_target_pct == expected


@pytest.mark.parametrize("max_v", [12, 10.5, 1e6])
def test_too_high_max_output_is_clamped_with_warning(config, caplog, max_v):
    cfg = copy.deepcopy(config)
    cfg["dac"]["max_output_v"] = max_v
    with caplog.at_level(logging.WARNING):
        dac = load_dac_settings(cfg)
    assert dac.max_output_v == 10.0
    assert dac.code_to_voltage(dac.pct_to_code(100)) <= 10.0
    assert any("geklemmt" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("override", [
    {"vdd": 0}, {"vdd": -3.3}, {"vdd": "x"}, {"opamp_gain": 0}, {"opamp_gain": float("nan")},
    {"max_output_v": -1}, {"max_output_v": 0}, {"vdd": 1.0, "opamp_gain": 1.0},
])
def test_code_never_exceeds_4095_or_negative(config, override):
    cfg = copy.deepcopy(config)
    cfg["dac"].update(override)
    cfg["pump"].pop("dac_full_scale_v", None)
    dac = load_dac_settings(cfg)
    for pct in [-100, 0, 0.1, 50, 99.9, 100, 1000]:
        c = dac.pct_to_code(pct)
        assert 0 <= c <= DAC_MAX
        assert dac.code_to_voltage(c) <= 10.0 + 1e-9


def test_written_bytes_encode_code(make_hw):
    hw = make_hw()
    hw.pump_set_speed(100)
    hw._dac_target_pct = hw._dac_actual_pct = 100.0   # ohne Rampe, nur Kodierung prüfen
    hw._dac_tick(now=1.0)
    assert hw._dac_bus.last_code == hw.dac.max_code
