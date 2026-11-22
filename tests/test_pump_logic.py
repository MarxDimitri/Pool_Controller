"""A4 — Zustandslogik Pumpe / Zeitplan / Mindestdrehzahl / Frostschutz / Rückspülung"""

import asyncio
import copy
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from services.pump_service import PumpMode, EmergencyActive, BackwashBusy
from tests.conftest import run, events_of

BERLIN = ZoneInfo("Europe/Berlin")
SUMMER = datetime(2026, 7, 8, 0, 0)     # Mittwoch, kein Wintermonat
WINTER = datetime(2026, 1, 7, 0, 0)     # Mittwoch, Wintermonat


async def run_loop(sim, coro_fn, seconds):
    sim.svc._running = True
    task = asyncio.create_task(coro_fn())
    await sim.vt.run_for(seconds)
    sim.svc._running = False
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return task


# ── Zeitplan ──────────────────────────────────────────────────

@pytest.mark.parametrize("hms,name,pct", [
    ("06:59:59", "Nacht", 0),
    ("07:00:00", "Morgenfilterung", 60),
    ("08:59:59", "Morgenfilterung", 60),
    ("09:00:00", "Tagesbetrieb", 30),
    ("18:59:59", "Tagesbetrieb", 30),
    ("19:00:00", "Abendfiltreung", 60),
    ("20:59:59", "Abendfiltreung", 60),
    ("21:00:00", "Nacht", 0),
    ("23:59:59", "Nacht", 0),
    ("00:00:00", "Nacht", 0),
])
def test_schedule_boundaries(sim_factory, hms, name, pct):
    sim = sim_factory(SUMMER)
    h, m, s = map(int, hms.split(":"))
    got = sim.svc._get_scheduled_speed(SUMMER.replace(hour=h, minute=m, second=s))
    assert (got.name, got.speed_pct, got.min_speed) == (name, pct, False)


def _record_transitions(sim):
    seen = []
    orig = sim.svc._track_schedule_transition

    def spy(sched):
        before = sim.svc._last_schedule_name
        orig(sched)
        if sim.svc._last_schedule_name != before:
            seen.append((sim.vt.now(), before, sim.svc._last_schedule_name))
    sim.svc._track_schedule_transition = spy
    return seen


def test_full_day_transitions_and_setpoints(sim_factory, info_logs):
    sim = sim_factory(SUMMER)
    seen = _record_transitions(sim)

    async def main():
        await run_loop(sim, sim.svc._schedule_loop, 24 * 3600)
    run(main())
    hours = [(t.hour, t.minute) for t, _, _ in seen[1:]]
    assert hours == [(7, 0), (9, 0), (19, 0), (21, 0)]
    assert len(events_of(info_logs, "schedule.transition")) == 5   # inkl. Start
    changes = [r for r in events_of(info_logs, "pump.setpoint_changed")]
    assert [(getattr(r, "pump.setpoint.old"), getattr(r, "pump.setpoint.new")) for r in changes] == \
        [(0, 60), (60, 30), (30, 60), (60, 0)]
    assert all(getattr(r, "pump.setpoint.reason") == "schedule" for r in changes)


@pytest.mark.parametrize("day,hours_in_day,night_pct", [
    (datetime(2026, 3, 29), 23, 20),    # Sommerzeit-Beginn (März = Wintermonat → Mindestdrehzahl)
    (datetime(2026, 10, 25), 25, 0),    # Winterzeit-Beginn
])
def test_dst_change_no_double_or_missed_switching(sim_factory, day, hours_in_day, night_pct):
    start = day.replace(tzinfo=BERLIN)
    sim = sim_factory(start)
    seen = _record_transitions(sim)
    setpoints = []
    orig_set = sim.hw._set_target
    sim.hw._set_target = lambda pct, reason: (setpoints.append(pct), orig_set(pct, reason))[1]

    async def main():
        await run_loop(sim, sim.svc._schedule_loop, hours_in_day * 3600 - 30)
    run(main())
    assert sim.vt.now().day == day.day        # noch derselbe Kalendertag
    local = [(t.hour, t.minute) for t, _, _ in seen[1:]]
    assert local == [(7, 0), (9, 0), (19, 0), (21, 0)]
    expected = [60, 30, 60] + ([night_pct] if night_pct else [0])
    if night_pct:
        expected = [night_pct] + expected
    assert setpoints == expected


# ── Modi ──────────────────────────────────────────────────────

def test_manual_with_duration_returns_to_schedule(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=10))

    async def main():
        sim.svc.resume_schedule()
        assert sim.hw.pump_speed == 30
        await sim.svc.manual_on(80, duration_min=2)
        assert sim.svc.state.mode == "manual" and sim.hw.pump_speed == 80
        await sim.vt.run_for(119)
        assert sim.svc.state.mode == "manual"
        await sim.vt.run_for(2)
        assert sim.svc.state.mode == "schedule" and sim.hw.pump_speed == 30
    run(main())


def test_manual_without_duration_persists(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=10))

    async def main():
        await sim.svc.manual_on(70)
        await run_loop(sim, sim.svc._schedule_loop, 3 * 3600)
        assert sim.svc.state.mode == "manual" and sim.hw.pump_speed == 70
    run(main())


def test_manual_off_not_overridden_by_min_speed(sim_factory):
    sim = sim_factory(WINTER.replace(hour=23))

    async def main():
        sim.svc.resume_schedule()
        assert sim.hw.pump_speed == 20 and sim.svc.state.min_speed_active
        await sim.svc.manual_off()
        await run_loop(sim, sim.svc._schedule_loop, 3600)
        assert sim.hw.pump_speed == 0 and sim.svc.state.mode == "manual"
        assert not sim.svc.state.min_speed_active
    run(main())


def test_commands_rejected_during_emergency_and_backwash(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=10))

    async def main():
        sim.svc.start_backwash()
        with pytest.raises(BackwashBusy):
            await sim.svc.manual_on(50)
        sim.svc.emergency_stop()
        await sim.vt.settle()
        with pytest.raises(EmergencyActive):
            await sim.svc.manual_on(50)
        with pytest.raises(EmergencyActive):
            sim.svc.resume_schedule()
        sim.svc.acknowledge_emergency()
        assert sim.svc.state.mode == "manual" and sim.hw.pump_speed == 0
        assert sim.hw.pump_mains_connected
    run(main())


# ── Mindestdrehzahl ───────────────────────────────────────────

def _cfg_min(config, mode, months=None):
    cfg = copy.deepcopy(config)
    cfg["pump"]["min_speed"]["mode"] = mode
    if months is not None:
        cfg["pump"]["min_speed"]["winter_months"] = months
    return cfg


@pytest.mark.parametrize("mode,when,expected", [
    ("off", WINTER.replace(hour=2), (0, False)),
    ("always", SUMMER.replace(hour=2), (20, True)),
    ("winter", SUMMER.replace(hour=2), (0, False)),
    ("winter", WINTER.replace(hour=2), (20, True)),
    ("winter", WINTER.replace(hour=10), (30, False)),     # Tagbetrieb bleibt
    ("always", SUMMER.replace(hour=8), (60, False)),
])
def test_min_speed_modes(sim_factory, config, mode, when, expected):
    sim = sim_factory(when, cfg=_cfg_min(config, mode))
    got = sim.svc._get_scheduled_speed(when)
    assert (got.speed_pct, got.min_speed) == expected
    assert got.name == sim.svc._get_scheduled_speed(when).name   # Name bleibt erhalten


@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 10, 31, 23, 59), 0),
    (datetime(2026, 11, 1, 0, 0), 20),
    (datetime(2026, 11, 30, 23, 59), 20),
    (datetime(2026, 12, 31, 23, 59), 20),
    (datetime(2027, 1, 1, 0, 0), 20),
    (datetime(2027, 2, 28, 23, 59), 20),
    (datetime(2027, 3, 1, 0, 0), 20),
    (datetime(2027, 3, 31, 23, 59), 20),
    (datetime(2027, 4, 1, 0, 0), 0),
])
def test_winter_month_boundaries(sim_factory, when, expected):
    sim = sim_factory(when)
    got = sim.svc._get_scheduled_speed(when)
    assert got.name == "Nacht" and got.speed_pct == expected


def test_min_speed_logs_keep_schedule_name(sim_factory, info_logs):
    sim = sim_factory(WINTER.replace(hour=22))
    sim.svc.resume_schedule()
    assert any("'Nacht'" in r.getMessage() and "Mindestdrehzahl" in r.getMessage()
               for r in info_logs.records)
    ch = events_of(info_logs, "pump.setpoint_changed")[-1]
    assert getattr(ch, "pump.setpoint.reason") == "min_speed"


def test_frost_has_priority_over_min_speed(sim_factory, config):
    cfg = copy.deepcopy(config)
    cfg["pump"]["speeds"]["min"] = 15            # frost (min) ≠ ... unterscheidbar machen
    cfg["frost"]["pump_speed"] = "eco"           # Frost = 30 %
    sim = sim_factory(WINTER.replace(hour=2), cfg=cfg)

    async def main():
        sim.svc.resume_schedule()
        assert sim.hw.pump_speed == 15
        await sim.svc._frost_step(1.0)
        assert sim.svc.state.mode == "frost" and sim.hw.pump_speed == 30
        sim.svc._apply_schedule()
        assert sim.hw.pump_speed == 30
    run(main())


def test_simulated_15c_never_triggers_winter(sim_factory, monkeypatch):
    sim = sim_factory(SUMMER.replace(hour=2))

    async def main():
        temp = await sim.svc._read_temperature()
        assert temp == 15.0                      # Simulationswert ohne DS18B20
        await sim.svc._frost_step(temp)
        sim.svc.resume_schedule()
        assert sim.hw.pump_speed == 0 and not sim.svc.state.min_speed_active
        assert not sim.svc.state.frost_active
    run(main())
    assert sim.svc._min_speed_pct(SUMMER) is None


# ── Frostschutz ───────────────────────────────────────────────

def test_frost_activation_threshold(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=2))

    async def main():
        await sim.svc._frost_step(3.0)
        assert not sim.svc.state.frost_active      # genau 3,0 → nicht aktiv
        await sim.svc._frost_step(2.9)
        assert sim.svc.state.frost_active and sim.hw.pump_speed == 20
    run(main())


def test_frost_min_runtime_and_deactivation(sim_factory, info_logs):
    sim = sim_factory(SUMMER.replace(hour=2))

    async def main():
        await sim.svc._frost_step(1.0)
        sim.vt.advance(5 * 60)
        await sim.svc._frost_step(10.0)
        assert sim.svc.state.frost_active          # 5 < 10 Min Mindestlaufzeit
        sim.vt.advance(5 * 60)
        await sim.svc._frost_step(10.0)
        assert not sim.svc.state.frost_active
        assert sim.svc.state.mode == "off"         # Zeitplan Nacht → aus
    run(main())
    assert events_of(info_logs, "frost.activated") and events_of(info_logs, "frost.deactivated")


def test_frost_no_flapping_around_threshold(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=2))
    toggles = []

    async def main():
        last = False
        for i, temp in enumerate([2.9, 3.1, 2.95, 3.2, 3.05, 3.3, 2.99, 3.1, 3.2, 3.4]):
            await sim.svc._frost_step(temp)
            if sim.svc.state.frost_active != last:
                toggles.append(i)
                last = sim.svc.state.frost_active
            sim.vt.advance(5 * 60)
    run(main())
    assert toggles == [0]                          # einmal an, kein Flattern


def test_frost_sensor_failure(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=2))

    async def main():
        await sim.svc._frost_step(None)
        assert sim.svc.state.sensor_error and not sim.svc.state.frost_active
        await sim.svc._frost_step(1.0)
        assert not sim.svc.state.sensor_error and sim.svc.state.frost_active
        sim.vt.advance(60 * 60)
        await sim.svc._frost_step(None)
        assert sim.svc.state.frost_active          # ohne Messwert nie beenden
    run(main())


def test_frost_uses_monotonic_clock_across_dst(sim_factory):
    """Rückstellung der Uhr (Okt.) verlängert die Mindestlaufzeit nicht um 1 h."""
    start = datetime(2026, 10, 25, 2, 55, tzinfo=BERLIN)
    sim = sim_factory(start)

    async def main():
        await sim.svc._frost_step(1.0)
        sim.vt.advance(11 * 60)                    # Wanduhr springt zurück
        await sim.svc._frost_step(10.0)
        assert not sim.svc.state.frost_active
    run(main())


# ── Rückspülung ───────────────────────────────────────────────

def _spy_valve(sim):
    opened = []
    orig = sim.hw.backwash_valve_open
    sim.hw.backwash_valve_open = lambda: (opened.append((sim.clock.t, sim.hw.pump_actual_speed)), orig())[1]
    return opened


def test_backwash_waits_for_actual_speed(sim_factory, info_logs):
    sim = sim_factory(SUMMER.replace(hour=10))
    opened = _spy_valve(sim)

    async def main():
        sim.svc.resume_schedule()
        sim.vt.advance(10)
        assert sim.hw.pump_actual_speed == 30
        t0 = sim.clock.t
        task = sim.svc.start_backwash()
        await sim.vt.run_for(400)
        assert task.done()
        (t_open, actual), = opened
        assert actual >= 99.5
        assert 14 - 0.4 <= t_open - t0 <= 14 + 0.4     # (100-30) / 5 %/s
        assert sim.svc.state.mode == "schedule" and sim.hw.pump_speed == 30
        assert sim.hw.get_all_states()["valves"]["backwash"] is False
    run(main())
    fin = events_of(info_logs, "backwash.finished")
    assert getattr(fin[-1], "backwash.result") == "ok"
    assert sim.svc.backwash_runs == 1


def test_backwash_from_standstill_takes_full_ramp(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=2))     # Nacht, Pumpe aus
    opened = _spy_valve(sim)

    async def main():
        t0 = sim.clock.t
        sim.svc.start_backwash()
        await sim.vt.run_for(400)
        assert 20 - 0.4 <= opened[0][0] - t0 <= 20 + 0.4
    run(main())


@pytest.mark.parametrize("abort_after", [5, 30])
def test_emergency_during_backwash(sim_factory, info_logs, abort_after):
    sim = sim_factory(SUMMER.replace(hour=10))

    async def main():
        task = sim.svc.start_backwash()
        await sim.vt.run_for(abort_after)
        sim.svc.emergency_stop()
        await sim.vt.settle()
        assert task.done()
        assert sim.svc.state.mode == "emergency"
        assert sim.hw.pump_actual_speed == 0 and sim.bus.last_code == 0
        assert sim.hw.get_all_states()["valves"]["backwash"] is False
        assert not sim.hw.pump_mains_connected
    run(main())
    assert getattr(events_of(info_logs, "backwash.finished")[-1], "backwash.result") == "cancelled"


def test_backwash_timeout_when_ramp_stalls(sim_factory, info_logs):
    sim = sim_factory(SUMMER.replace(hour=10))
    opened = _spy_valve(sim)

    async def main():
        sim.vt.tick_dac = False                     # DAC-Thread "tot"
        task = sim.svc.start_backwash()
        await sim.vt.run_for(120)
        assert task.done() and not opened
        assert sim.svc.state.mode == "schedule"
    run(main())
    assert any("nicht innerhalb" in r.getMessage() for r in info_logs.records)
    assert getattr(events_of(info_logs, "backwash.finished")[-1], "backwash.result") == "error"


def test_backwash_restores_previous_manual_state(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=10))

    async def main():
        await sim.svc.manual_on(45)
        sim.svc.start_backwash()
        await sim.vt.run_for(400)
        assert sim.svc.state.mode == "manual" and sim.hw.pump_speed == 45
    run(main())


def test_backwash_cancel_resume_schedule(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=10))

    async def main():
        await sim.svc.manual_on(45)
        sim.svc.start_backwash()
        await sim.vt.run_for(30)
        await sim.svc.cancel_backwash(resume_schedule=True)
        assert sim.svc.state.mode == "schedule" and sim.hw.pump_speed == 30
    run(main())


def test_backwash_blocked_on_low_level(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=10))
    sim.gpio.level = False

    async def main():
        with pytest.raises(ValueError):
            sim.svc.start_backwash()
    run(main())


def test_frost_during_backwash_takes_over_afterwards(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=2))

    async def main():
        sim.svc.start_backwash()
        await sim.vt.run_for(30)
        await sim.svc._frost_step(1.0)
        assert sim.svc.state.mode == "backwash" and sim.svc.state.frost_active
        await sim.vt.run_for(400)
        assert sim.svc.state.mode == "frost" and sim.hw.pump_speed == 20
    run(main())


def test_backwash_scheduler_triggers_monday_10(sim_factory, config):
    monday = datetime(2026, 7, 6, 9, 59)
    sim = sim_factory(monday)

    async def main():
        await run_loop(sim, sim.svc._backwash_scheduler, 10 * 60)
    run(main())
    assert sim.svc.backwash_runs == 1


# ── MQTT-Befehle ──────────────────────────────────────────────

@pytest.mark.parametrize("cmd,mode,speed", [
    ("on", "manual", 60), ("off", "manual", 0), ("speed:35", "manual", 35),
    ("speed:abc", "schedule", 30), ("schedule", "schedule", 30), ("bogus", "schedule", 30),
])
def test_mqtt_commands(sim_factory, cmd, mode, speed):
    sim = sim_factory(SUMMER.replace(hour=10))

    async def main():
        sim.svc.resume_schedule()
        await sim.svc.handle_mqtt_command(cmd)
        assert sim.svc.state.mode == mode and sim.hw.pump_speed == speed
    run(main())


def test_mqtt_backwash_and_rejection(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=10))

    async def main():
        await sim.svc.handle_mqtt_command("backwash")
        assert sim.svc.backwash_running
        await sim.svc.handle_mqtt_command("on")          # abgelehnt, keine Exception
        assert sim.svc.state.mode == "backwash"
        sim.svc.emergency_stop()
        await sim.vt.settle()
    run(main())


def test_state_change_callback_async(sim_factory):
    sim = sim_factory(SUMMER.replace(hour=10))
    got = []

    async def cb(device, state):
        got.append((device, state["mode"]))
    sim.svc.on_state_change = cb

    async def main():
        await sim.svc.manual_on(40)
        await sim.vt.settle()
    run(main())
    assert ("pump", "manual") in got
    sim.svc._update_state(mode="manual")   # ohne Loop: Coroutine wird sauber verworfen
