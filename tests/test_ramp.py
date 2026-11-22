"""A2 — Rampe (Fake-Clock, Ticks von Hand)"""

import copy
import logging

import pytest

from tests.conftest import FakeClock


@pytest.fixture
def rig(make_hw):
    clock = FakeClock()
    hw = make_hw(clock=clock)

    def tick(n=1):
        out = []
        for _ in range(n):
            clock.advance(hw.dac.refresh_interval_s)
            hw._dac_tick()
            out.append(hw._dac_actual_pct)
        return out

    return hw, clock, tick


def test_ramp_0_to_60_takes_12s(rig):
    hw, clock, tick = rig
    ramp = hw.dac.ramp_pct_per_s
    dt = hw.dac.refresh_interval_s
    start = clock.t
    hw.pump_on(60, reason="manual")
    assert hw.pump_actual_speed == 0          # Setter schreibt nicht direkt
    while hw._dac_actual_pct < 60:
        tick()
        assert clock.t - start < 30
    elapsed = clock.t - start
    expected = 60 / ramp
    assert expected - dt <= elapsed <= expected + dt
    assert hw._dac_bus.last_code == hw.dac.pct_to_code(60)


def test_no_step_larger_than_ramp_per_tick(rig):
    hw, _, tick = rig
    limit = hw.dac.ramp_pct_per_s * hw.dac.refresh_interval_s + 1e-9
    hw.pump_on(100)
    values = [0.0] + tick(200)
    hw.pump_off()
    values += tick(200)
    steps = [abs(b - a) for a, b in zip(values, values[1:])]
    assert max(steps) <= limit
    # auch in DAC-Codes: kein Sprung über ein Schritt-Äquivalent (+1 Rundung)
    codes = [hw.dac.pct_to_code(v) for v in values]
    code_limit = hw.dac.pct_to_code(limit) + 2
    assert max(abs(b - a) for a, b in zip(codes, codes[1:])) <= code_limit


def test_stall_does_not_cause_jump(rig):
    """Hängt der Thread 5 s, darf der nächste Schritt trotzdem nicht springen (dt ≤ 3 Takte)."""
    hw, clock, tick = rig
    tick()
    hw.pump_on(60)
    clock.advance(5.0)
    hw._dac_tick()
    assert hw._dac_actual_pct <= hw.dac.ramp_pct_per_s * 3 * hw.dac.refresh_interval_s + 1e-9


def test_direction_change_mid_ramp_continues_from_actual(rig):
    hw, _, tick = rig
    hw.pump_on(60)
    while hw._dac_actual_pct < 30:
        tick()
    at_change = hw._dac_actual_pct
    hw.pump_off()
    nxt = tick()[0]
    assert nxt < at_change
    assert at_change - nxt <= hw.dac.ramp_pct_per_s * hw.dac.refresh_interval_s + 1e-9
    values = tick(200)
    assert values[-1] == 0


def test_new_target_overrides_without_fallback(rig):
    hw, _, tick = rig
    hw.pump_on(80)
    tick(25)                       # ~25 %
    mid = hw._dac_actual_pct
    hw.pump_set_speed(40)
    vals = tick(100)
    assert all(v >= mid - 1e-9 for v in vals)   # kein Rückfall unter den Istwert
    assert vals[-1] == 40


@pytest.mark.parametrize("bad", [0, -5, "x", None])
def test_invalid_ramp_falls_back_to_default(config, caplog, make_hw, bad):
    cfg = copy.deepcopy(config)
    cfg["dac"]["ramp_pct_per_s"] = bad
    with caplog.at_level(logging.WARNING):
        hw = make_hw(cfg)
    assert hw.dac.ramp_pct_per_s == 5.0
    assert any("ramp_pct_per_s" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("action", ["emergency_stop", "pump_mains_cutoff", "cleanup"])
def test_stop_paths_are_immediate_without_ramp(rig, action):
    hw, _, tick = rig
    hw.pump_on(60)
    tick(200)
    assert hw._dac_actual_pct == 60
    getattr(hw, action)()
    assert hw._dac_actual_pct == 0 and hw._dac_target_pct == 0
    assert hw._dac_bus.last_code == 0             # sofort 0 V, ohne weiteren Tick
