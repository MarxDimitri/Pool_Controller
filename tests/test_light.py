"""Beleuchtung: Zeitplan, Sonnenzeiten, manueller Betrieb, Dimmer (simuliert), API/MQTT/Telegram"""

import asyncio
import copy
import logging
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from services.dimmer import DIMMER_DEFAULTS, SimulatedDimmer, load_dimmer_settings
from services.light_service import (LightService, load_lighting_config, parse_time_spec,
                                    sun_times_utc)
from services.pump_service import EmergencyActive
from tests.conftest import AUTH, API_SECRET, events_of, load_real_config, run

DAY = date(2026, 7, 1)
SUNSET = datetime(2026, 7, 1, 21, 30)
SUNRISE = datetime(2026, 7, 1, 5, 0)


def fake_sun(day):
    """Feste Sonnenzeiten (unabhängig von Zeitzone und Ort)."""
    return (datetime.combine(day, SUNRISE.time()), datetime.combine(day, SUNSET.time()))


def light_cfg(config, schedule=None, **extra):
    cfg = copy.deepcopy(config)
    cfg["lighting"] = {
        "latitude": 52.52, "longitude": 13.405, "default_brightness_pct": 70,
        "dimmer": {"type": "simulation", "min_pct": 10},
        "schedule": schedule if schedule is not None else [
            {"name": "Abend", "start": "sunset", "end": "22:30", "brightness_pct": 80},
            {"name": "Spät", "start": "22:30", "end": "00:30", "brightness_pct": 30},
        ],
        **extra,
    }
    return cfg


@pytest.fixture
def rig(config, sim_factory):
    """Sim (Hardware + PumpService auf virtueller Zeit) + LightService."""
    def _make(wall=datetime(2026, 7, 1, 12, 0), **kw):
        cfg = light_cfg(config, **kw)
        sim = sim_factory(wall, cfg=cfg)
        states = []
        light = LightService(sim.hw, cfg, sim.svc, lambda dev, st: states.append((dev, st)),
                             now=sim.vt.now, sleep=sim.vt.sleep, sun_times=fake_sun)
        sim.light, sim.states = light, states
        return sim
    return _make


# ── Sonnenzeiten ──────────────────────────────────────────────

@pytest.mark.parametrize("day,rise,set_", [
    (date(2026, 6, 21), (2, 43), (19, 33)),     # Berlin Sommer: 04:43 / 21:33 MESZ
    (date(2026, 12, 21), (7, 15), (14, 54)),    # Berlin Winter: 08:15 / 15:54 MEZ
])
def test_sun_times_berlin(day, rise, set_):
    r, s = sun_times_utc(day, 52.52, 13.405)
    for got, (h, m) in ((r, rise), (s, set_)):
        expected = datetime(day.year, day.month, day.day, h, m, tzinfo=got.tzinfo)
        assert abs((got - expected).total_seconds()) <= 180


def test_sun_times_polar_day_and_night():
    assert sun_times_utc(date(2026, 6, 21), 78.2, 15.6) == (None, None)   # Spitzbergen
    assert sun_times_utc(date(2026, 12, 21), 78.2, 15.6) == (None, None)


# ── Konfiguration ─────────────────────────────────────────────

@pytest.mark.parametrize("text,kind,minutes", [
    ("07:30", "clock", 450), ("0:05", "clock", 5), ("sunset", "sunset", 0),
    ("sunset+30", "sunset", 30), ("Sunrise - 15", "sunrise", -15),
])
def test_parse_time_spec(text, kind, minutes):
    spec = parse_time_spec(text)
    assert (spec.kind, spec.minutes) == (kind, minutes)


@pytest.mark.parametrize("text", ["24:00", "7.30", "dusk", "sunset+999", ""])
def test_parse_time_spec_rejects(text):
    with pytest.raises(ValueError):
        parse_time_spec(text)


def test_invalid_entries_are_skipped(caplog):
    cfg = load_lighting_config({"lighting": {"latitude": 0, "longitude": 0, "auto_sunset": True,
                                             "schedule": [
        {"name": "ok", "start": "20:00", "end": "22:00", "brightness_pct": 50},
        {"name": "sonne", "start": "sunset", "end": "23:00"},          # keine Koordinaten
        {"name": "kaputt", "start": "abends", "end": "23:00"},
        {"name": "zu hell", "start": "20:00", "end": "21:00", "brightness_pct": 150},
        {"name": "gleich", "start": "20:00", "end": "20:00"},
        {"name": "ohne end", "start": "20:00"},
    ]}})
    assert [e.name for e in cfg["schedule"]] == ["ok"]
    assert cfg["coords"] is None
    for name in ("sonne", "kaputt", "zu hell", "gleich", "ohne end"):
        assert f"'{name}' ignoriert" in caplog.text
    assert "lighting.auto_sunset wird nicht verwendet" in caplog.text


def test_missing_section_means_no_schedule_and_simulated_dimmer():
    cfg = load_lighting_config({})
    assert cfg["schedule"] == [] and cfg["default_brightness_pct"] == 80
    assert load_dimmer_settings({}).type == DIMMER_DEFAULTS["type"]
    assert load_dimmer_settings({"lighting": {"dimmer": {"type": "dali", "min_pct": 0}}}).min_pct == 10


def test_real_config_lighting_section_loads():
    cfg = load_lighting_config(load_real_config())
    assert cfg["schedule"], "config.yaml sollte einen Beispiel-Zeitplan enthalten"
    assert load_dimmer_settings(load_real_config()).type == "simulation"


# ── Zeitplan ──────────────────────────────────────────────────

def test_scheduled_windows_sunset_and_overnight(rig):
    light = rig().light
    at = lambda h, m=0, d=0: datetime(2026, 7, 1 + d, h, m)   # noqa: E731
    assert light.scheduled(at(21, 29))[1] == 0
    assert light.scheduled(at(21, 30))[0].name == "Abend"
    assert light.scheduled(at(22, 29))[1] == 80
    assert light.scheduled(at(22, 30))[1] == 30
    assert light.scheduled(at(0, 15, d=1))[1] == 30        # über Mitternacht
    assert light.scheduled(at(0, 30, d=1))[1] == 0


def test_first_matching_entry_wins(rig):
    light = rig(schedule=[
        {"name": "A", "start": "20:00", "end": "23:00", "brightness_pct": 60},
        {"name": "B", "start": "21:00", "end": "22:00", "brightness_pct": 10},
    ]).light
    assert light.scheduled(datetime(2026, 7, 1, 21, 30))[0].name == "A"


def test_next_change(rig):
    light = rig().light
    nxt = light.next_change(datetime(2026, 7, 1, 12, 0))
    assert nxt == {"time": "2026-07-01T21:30", "brightness_pct": 80, "entry": "Abend"}
    nxt = light.next_change(datetime(2026, 7, 1, 23, 0))
    assert nxt == {"time": "2026-07-02T00:30", "brightness_pct": 0, "entry": None}


def test_schedule_loop_follows_the_evening(rig, info_logs):
    sim = rig(wall=datetime(2026, 7, 1, 21, 0))

    async def scenario():
        task = asyncio.create_task(sim.light.start())
        await sim.vt.run_for(10 * 60)
        assert sim.hw.light_brightness == 0
        await sim.vt.run_until(sim.clock.t + 25 * 60)          # 21:35
        assert sim.hw.light_brightness == 80 and sim.hw.light_is_on
        await sim.vt.run_until(sim.clock.t + 60 * 60)          # 22:35
        assert sim.hw.light_brightness == 30
        await sim.vt.run_until(sim.clock.t + 2 * 3600)         # 00:35
        assert sim.hw.light_brightness == 0 and not sim.hw.light_is_on
        sim.light.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())
    changes = [(r.__dict__["light.brightness_pct"], r.__dict__.get("light.schedule_entry"))
               for r in events_of(info_logs, "light.changed")]
    assert changes == [(80, "Abend"), (30, "Spät"), (0, None)]   # keine Wiederholungen
    assert len(events_of(info_logs, "light.schedule_transition")) == 3
    assert sim.hw.dimmer.history == [80, 30, 0]


# ── Manuell / Zeitplan ────────────────────────────────────────

def test_manual_pauses_schedule_until_resumed(rig):
    sim = rig(wall=datetime(2026, 7, 1, 21, 45))
    light = sim.light
    light.apply_schedule()
    assert sim.hw.light_brightness == 80 and light.mode == "schedule"
    light.set_brightness(45)
    assert light.mode == "manual" and sim.hw.light_brightness == 45
    light.apply_schedule()                                     # pausiert: keine Änderung
    assert sim.hw.light_brightness == 45
    light.resume_schedule()
    assert light.mode == "schedule" and sim.hw.light_brightness == 80
    light.set_manual()                                         # einfrieren
    assert light.mode == "manual" and sim.hw.light_brightness == 80
    assert sim.states[-1][0] == "light" and sim.states[-1][1]["mode"] == "manual"


def test_turn_on_uses_last_brightness_and_min_pct(rig):
    sim = rig(schedule=[])
    light = sim.light
    assert light.turn_on() == 70                               # default_brightness_pct
    light.set_brightness(55)
    light.turn_off()
    assert sim.hw.light_brightness == 0 and not sim.hw.light_is_on
    assert light.turn_on() == 55                               # zuletzt genutzt
    assert light.set_brightness(3) == 10                       # dimmer.min_pct
    with pytest.raises(ValueError):
        light.set_brightness(101)


def test_emergency_turns_light_off_and_blocks_brightening(rig):
    sim = rig(wall=datetime(2026, 7, 1, 21, 45))
    light = sim.light
    light.apply_schedule()
    assert sim.hw.light_brightness == 80
    sim.svc.emergency_stop()
    assert sim.hw.light_brightness == 0                        # Abschaltung: Licht aus
    light.apply_schedule()
    assert sim.hw.light_brightness == 0                        # Zeitplan schaltet nicht ein
    with pytest.raises(EmergencyActive):
        light.turn_on()
    light.turn_off()                                           # aus geht immer
    sim.svc.acknowledge_emergency()
    light.resume_schedule()
    assert sim.hw.light_brightness == 80


def test_light_event_only_on_change(make_hw, info_logs):
    hw = make_hw()
    hw.set_light(50); hw.set_light(50); hw.set_light(60); hw.light_off(); hw.light_off()
    evs = events_of(info_logs, "light.changed")
    assert [(r.__dict__["light.brightness_old"], r.__dict__["light.brightness_pct"])
            for r in evs] == [(0, 50), (50, 60), (60, 0)]
    assert hw.get_all_states()["light_brightness_pct"] == 0
    assert hw.get_all_states()["light_dimmer"] == "simulation"


def test_dimmer_gets_level_before_relay_turns_on(make_hw):
    order = []

    class Spy(SimulatedDimmer):
        def set_level(self, pct):
            order.append(("dimmer", pct))
            super().set_level(pct)

    hw = make_hw(dimmer=Spy())
    orig = hw._relay_on
    hw._relay_on = lambda pin: (order.append(("relay", True)), orig(pin))
    hw.set_light(40)
    assert order == [("dimmer", 40), ("relay", True)]


def test_mqtt_commands(rig):
    sim = rig(schedule=[])
    light = sim.light
    run(light.handle_mqtt_command("brightness:35"))
    assert sim.hw.light_brightness == 35 and light.mode == "manual"
    run(light.handle_mqtt_command("off"))
    assert sim.hw.light_brightness == 0
    run(light.handle_mqtt_command("schedule"))
    assert light.mode == "schedule"
    run(light.handle_mqtt_command("brightness:abc"))           # wird geloggt, wirft nicht
    run(light.handle_mqtt_command("disco"))


# ── API ───────────────────────────────────────────────────────

@pytest.fixture
def api_rig(config, make_hw):
    cfg = light_cfg(config)
    hw = make_hw(cfg, start_thread=True)
    from services.pump_service import PumpService
    svc = PumpService(hw, cfg)
    light = LightService(hw, cfg, svc, sun_times=fake_sun,
                         now=lambda: datetime(2026, 7, 1, 21, 45))
    app = api_main.create_app(svc, object(), hw, {"secret_key": API_SECRET}, light_service=light)
    with TestClient(app) as client:
        yield client, svc, hw, light
    hw.cleanup()


def test_api_light_actions(api_rig):
    client, svc, hw, light = api_rig
    r = client.post("/api/light", json={"action": "on", "brightness_pct": 40}, headers=AUTH)
    assert r.json()["light"] is True and r.json()["state"]["brightness_pct"] == 40
    assert r.json()["state"]["mode"] == "manual"
    r = client.post("/api/light/brightness/65", headers=AUTH)
    assert r.json()["state"]["brightness_pct"] == 65
    assert client.post("/api/light/brightness/120", headers=AUTH).status_code == 422
    assert client.post("/api/light", json={"action": "brightness"}, headers=AUTH).status_code == 400
    r = client.post("/api/light/schedule", headers=AUTH)
    st = r.json()["state"]
    assert st["mode"] == "schedule" and st["brightness_pct"] == 80 and st["schedule_entry"] == "Abend"
    assert st["next_change"]["time"] == "2026-07-01T22:30"
    assert client.get("/api/light", headers=AUTH).json()["dimmer"] == "simulation"
    assert client.get("/api/status", headers=AUTH).json()["light"]["on"] is True
    r = client.post("/api/light", json={"action": "manual"}, headers=AUTH)
    assert r.json()["state"]["mode"] == "manual" and r.json()["state"]["brightness_pct"] == 80


def test_api_light_emergency(api_rig):
    client, svc, hw, light = api_rig
    client.post("/api/light/brightness/50", headers=AUTH)
    client.post("/api/emergency_stop", headers=AUTH)
    assert hw.light_brightness == 0
    assert client.post("/api/light/brightness/50", headers=AUTH).status_code == 423
    assert client.post("/api/light", json={"action": "off"}, headers=AUTH).status_code == 200


def test_api_without_light_service_still_switches(config, make_hw):
    from services.pump_service import PumpService
    hw = make_hw(start_thread=True)
    app = api_main.create_app(PumpService(hw, config), object(), hw, {"secret_key": API_SECRET})
    with TestClient(app) as client:
        assert client.post("/api/light", json={"action": "on"}, headers=AUTH).json()["light"] is True
    hw.cleanup()


# ── Telegram ──────────────────────────────────────────────────

def test_telegram_light_commands(rig):
    from services.telegram_service import TelegramService
    from tests.test_telegram import FakeApi, NOW, USER, msg
    sim = rig(wall=datetime(2026, 7, 1, 21, 45))
    cfg = copy.deepcopy(sim.light.cfg)
    tcfg = {"telegram": {"enabled": True, "allowed_user_ids": [USER], "token": "1:x"}}
    api = FakeApi()
    bot = TelegramService(tcfg, sim.svc, sim.hw, light=sim.light, api=api, clock=lambda: NOW)

    run(bot.handle_update(msg("/licht 40")))
    assert sim.hw.light_brightness == 40 and sim.light.mode == "manual"
    assert "Licht: an, 40 %" in api.last_text() and "manuell" in api.last_text()
    run(bot.handle_update(msg("/licht zeitplan")))
    assert sim.hw.light_brightness == 80
    assert "Zeitplan „Abend“, 22:30 → 30 %" in api.last_text()
    run(bot.handle_update(msg("/licht an 25%")))
    assert sim.hw.light_brightness == 25
    run(bot.handle_update(msg("/licht 150")))
    assert "Aufruf: /licht" in api.last_text()
    run(bot.handle_update(msg("/status")))
    assert "Licht: an, 25 % (Dimmer simuliert) — manuell" in api.last_text()
    assert cfg["schedule"]


def test_telegram_light_event_texts(config, sim_factory):
    from services.telegram_service import TelegramService
    sim = sim_factory(datetime(2026, 7, 1, 12, 0))
    bot = TelegramService({}, sim.svc, sim.hw)

    def rec(**attrs):
        r = logging.LogRecord("services.hardware", logging.INFO, __file__, 1, "x", None, None)
        setattr(r, "event.name", "light.changed")
        for k, v in attrs.items():
            setattr(r, "light." + k, v)
        return r

    assert bot.format_event(rec(on=True, brightness_pct=80, brightness_old=0, reason="schedule",
                                schedule_entry="Abend")) == "Licht an, 80 % (Zeitplan „Abend“)"
    assert bot.format_event(rec(on=True, brightness_pct=30, brightness_old=80,
                                reason="manual")) == "Licht 80 → 30 % (manuell)"
    assert bot.format_event(rec(on=False, brightness_pct=0, brightness_old=30,
                                reason="emergency")) == "Licht aus (Abschaltung)"
