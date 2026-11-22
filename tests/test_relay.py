"""A5 — Relais / Netztrennung"""

import pytest

from tests.conftest import FlakyBus, RecordingGPIO


@pytest.fixture
def rig(make_hw, config):
    events = []
    gpio = RecordingGPIO(events)
    bus = FlakyBus(events)
    hw = make_hw(gpio=gpio, i2c_bus=bus)
    return hw, gpio, bus, events, config["gpio"]


def test_pump_commands_never_touch_mains_relay(rig):
    hw, gpio, _, _, pins = rig
    n = len(gpio.outputs_for(pins["pump_relay"]))
    for i in range(20):
        hw.pump_on(i * 5)
        hw.pump_set_speed(100 - i * 5)
        hw.pump_off()
        hw._dac_tick(now=1.0 + i)
    assert len(gpio.outputs_for(pins["pump_relay"])) == n


def test_mains_cutoff_dac_zero_before_relay_open(rig):
    hw, gpio, bus, events, pins = rig
    hw.pump_on(60)
    hw._dac_target_pct = hw._dac_actual_pct = 60.0
    hw._dac_tick(now=1.0)
    events.clear()
    hw.pump_mains_cutoff()
    dac_idx = next(i for i, e in enumerate(events) if e == ("dac", 0))
    relay_idx = next(i for i, e in enumerate(events) if e == ("gpio", pins["pump_relay"], True))
    assert dac_idx < relay_idx                 # erst 0 V, dann Relais (HIGH = aus)
    assert hw.pump_mains_connected is False


def test_restore_closes_relay(rig):
    hw, gpio, _, _, pins = rig
    hw.pump_mains_cutoff()
    hw.pump_mains_restore()
    assert gpio.outputs_for(pins["pump_relay"])[-1] == ("output", pins["pump_relay"], False)
    assert hw.pump_mains_connected is True
    assert hw.pump_speed == 0                  # läuft nach Restore NICHT an


def test_emergency_stop_calls_cutoff(rig, monkeypatch):
    hw, *_ = rig
    called = []
    orig = hw.pump_mains_cutoff
    monkeypatch.setattr(hw, "pump_mains_cutoff", lambda **kw: (called.append(kw), orig(**kw)))
    hw.emergency_stop()
    assert called and called[0].get("reason") == "emergency"
    assert hw.pump_mains_connected is False


def test_startup_state(rig):
    hw, gpio, bus, events, pins = rig
    # Relais geschlossen (active-low: LOW), DAC 0 V, Soll 0
    assert ("output", pins["pump_relay"], False) in gpio.calls
    assert hw.pump_mains_connected is True
    assert bus.history[0][1] == 0
    assert hw.pump_speed == 0 and hw.pump_actual_speed == 0 and not hw.pump_running
    # alle anderen Relais aus (HIGH)
    for key in ("backwash_relay", "fill_relay", "ph_relay", "chlor_relay", "light_relay"):
        assert gpio.state[pins[key]] is True


def test_light_relay_active_low_and_idempotent(rig):
    hw, gpio, _, _, pins = rig
    pin = pins["light_relay"]
    assert pin == 25
    hw.light_on(); hw.light_on()
    assert gpio.state[pin] is False and hw.light_is_on is True
    assert hw.get_all_states()["light"] is True
    hw.light_off(); hw.light_off()
    assert gpio.state[pin] is True and hw.light_is_on is False
    assert hw.get_all_states()["light"] is False


def test_mains_connected_matches_relay_in_every_state(rig):
    hw, gpio, _, _, pins = rig
    pin = pins["pump_relay"]
    for action in (hw.emergency_stop, hw.pump_mains_restore, hw.pump_mains_cutoff,
                   hw.pump_mains_restore):
        action()
        assert hw.pump_mains_connected == (gpio.state[pin] is False)


def test_get_all_states_contains_dac_fields(rig):
    hw, *_ = rig
    st = hw.get_all_states()
    assert st["pump"]["actual_speed_pct"] == 0
    assert st["dac"]["write_errors"] == 0
    assert st["simulation"] is True
