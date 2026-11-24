"""Stufe B — Integrations- und Robustheitstests (automatisch, etwas länger)"""

import asyncio
import errno
import gc
import logging
import logging.handlers
import socket
import threading
import time
import tracemalloc
from contextlib import ExitStack
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from services.hardware import HardwareController
from services.mqtt_service import MQTTService
from services.pump_service import PumpService
from services import telemetry as tel
from tests.conftest import (
    API_SECRET, AUTH, FlakyBus, RecordingGPIO, ROOT, Sim, events_of, max_interval, run,
)

pytestmark = pytest.mark.stage_b


# ── B1 Dauerlauf 7 Tage ───────────────────────────────────────

def test_b1_seven_day_soak(config, tmp_path):
    start = datetime(2026, 1, 5, 0, 0)          # Montag, Winter → Mindestdrehzahl nachts
    # MockDacBus (begrenzte Historie) statt FlakyBus — dessen Aufzeichnungslisten
    # wachsen absichtlich unbegrenzt und würden den Speichertest verfälschen
    from services.hardware import MockDacBus
    sim = Sim(config, start, bus=MockDacBus())
    svc, vt = sim.svc, sim.vt

    async def temperature():
        now = vt.now()
        return 1.5 if (now.weekday() == 2 and 2 <= now.hour < 4) else 8.0
    svc._read_temperature = temperature

    events = {"frost.activated": 0, "frost.deactivated": 0, "backwash.finished": 0,
              "schedule.transition": 0, "pump.setpoint_changed": 0}

    class Counter(logging.Handler):
        def emit(self, record):
            name = getattr(record, "event.name", None)
            if name in events:
                events[name] += 1

    log_file = tmp_path / "soak.log"
    rot = logging.handlers.RotatingFileHandler(log_file, maxBytes=20000, backupCount=2)
    rot.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    svc_logger = logging.getLogger("services")
    svc_logger.addHandler(rot)
    svc_logger.addHandler(Counter())
    svc_logger.setLevel(logging.INFO)
    svc_logger.propagate = False
    threads_before = threading.active_count()
    mem = {}

    async def main():
        task = asyncio.create_task(svc.start())
        day = 86400
        t0 = sim.clock.t
        await vt.run_until(t0 + day)
        gc.collect()
        tracemalloc.start()
        await vt.run_until(t0 + 2 * day)
        mem["day2"] = tracemalloc.get_traced_memory()[0]
        await vt.run_until(t0 + 4 * day + 12 * 3600)              # Freitag 12:00
        await svc.manual_on(80, duration_min=30)
        await vt.run_for(29 * 60)
        assert svc.state.mode == "manual"
        await vt.run_for(2 * 60)
        assert svc.state.mode == "schedule"
        await vt.run_until(t0 + 7 * day - 60)
        gc.collect()
        mem["day7"] = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        assert not task.done(), task.exception() if task.done() else None
        svc._running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    try:
        run(main())
    finally:
        svc_logger.removeHandler(rot)
        for h in list(svc_logger.handlers):
            if isinstance(h, Counter):
                svc_logger.removeHandler(h)
        svc_logger.propagate = True
        svc_logger.setLevel(logging.NOTSET)
        rot.close()

    assert events["backwash.finished"] == 1
    assert events["frost.activated"] == 1 and events["frost.deactivated"] == 1
    assert events["schedule.transition"] == 1 + 7 * 4
    assert svc.backwash_runs == 1
    assert threading.active_count() == threads_before
    assert mem["day7"] - mem["day2"] < 1_000_000, mem
    files = sorted(tmp_path.glob("soak.log*"))
    assert 1 <= len(files) <= 3
    assert all(f.stat().st_size <= 20000 + 1000 for f in files)
    # Plausibilität des Endzustands: Sonntag 23:59 → Nacht mit Mindestdrehzahl
    assert sim.hw.pump_speed == 20 and svc.state.min_speed_active


# ── B2 Fehlerinjektion ────────────────────────────────────────

def test_b2_i2c_fault_phases_keep_cadence(config):
    bus = FlakyBus()
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=bus, start_thread=True)
    try:
        hw.pump_on(60)
        for rate in (0.01, 0.5, 1.0, 0.0):
            bus.fail_rate = rate
            n = len(bus.attempt_times)
            time.sleep(1.2)
            assert hw.dac_thread_alive
            assert max_interval(bus.attempt_times[n:], skip=0) < 3 * hw.dac.refresh_interval_s
        assert hw.get_dac_snapshot()["write_errors"] > 0
        assert hw.get_dac_snapshot()["consecutive_errors"] == 0     # nach Erholung
    finally:
        hw.cleanup()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.mark.parametrize("kind", ["closed", "blackhole"])
def test_b2_mqtt_broker_down_does_not_block(config, kind):
    hole = None
    if kind == "closed":
        port = _free_port()
    else:
        hole = socket.socket()
        hole.bind(("127.0.0.1", 0))
        hole.listen(5)
        port = hole.getsockname()[1]
    cfg = dict(config)
    cfg["mqtt"] = {**config["mqtt"], "broker": "127.0.0.1", "port": port}

    async def main():
        mqtt = MQTTService(cfg)
        task = asyncio.create_task(mqtt.start())
        lags = []
        for _ in range(30):
            t = time.monotonic()
            await asyncio.sleep(0.05)
            lags.append(time.monotonic() - t - 0.05)
        await mqtt.publish("pump/speed", 10)      # offline → kein Fehler
        await mqtt.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return max(lags)
    try:
        assert run(main()) < 0.2
    finally:
        if hole:
            hole.close()


def test_b2_disk_full_logging_does_not_break_control(config, capsys):
    class DiskFull(logging.Handler):
        def emit(self, record):
            try:
                raise OSError(errno.ENOSPC, "No space left on device")
            except OSError:
                self.handleError(record)

    lg = logging.getLogger("services")
    h = DiskFull()
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    bus = FlakyBus()
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=bus, start_thread=True)
    try:
        for pct in (10, 50, 0, 30):
            hw.pump_set_speed(pct)
        time.sleep(0.6)
        assert hw.dac_thread_alive and hw.pump_speed == 30
        assert max_interval(bus.attempt_times) < 3 * hw.dac.refresh_interval_s
    finally:
        lg.removeHandler(h)
        lg.setLevel(logging.NOTSET)
        hw.cleanup()
    capsys.readouterr()


def test_b2_network_down_with_telemetry_and_faults(config, monkeypatch):
    """OTel-Endpunkt weg + I²C-Fehler gleichzeitig — DAC-Takt bleibt."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{_free_port()}")
    t = tel.setup_telemetry({"telemetry": {"enabled": True, "metrics_interval_s": 1,
                                           "system_metrics": True}})
    bus = FlakyBus(fail_rate=0.3)
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=bus, start_thread=True)
    t.register_sources(hw, None)
    try:
        hw.pump_on(100)
        time.sleep(2.5)
        assert max_interval(bus.attempt_times) < 3 * hw.dac.refresh_interval_s
    finally:
        hw.cleanup()
        t.shutdown(timeout_s=2)


# ── B3 Neustart ───────────────────────────────────────────────

def test_b3_restart_mid_ramp_starts_at_zero_and_ramps(config):
    bus = FlakyBus()
    from tests.conftest import FakeClock
    c1 = FakeClock()
    hw1 = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=bus, clock=c1, start_thread=False)
    hw1.pump_on(60)
    for _ in range(30):
        c1.advance(0.2)
        hw1._dac_tick()
    assert 25 < hw1.pump_actual_speed < 35
    hw1.cleanup()                                   # geordneter Stopp → 0 V
    assert bus.last_code == 0

    c2 = FakeClock()
    n = bus.writes
    hw2 = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=bus, clock=c2, start_thread=False)
    assert bus.writes == n + 1 and bus.last_code == 0      # erstes Schreiben nach Start: 0 V
    hw2.pump_on(60)                                         # Zeitplan übernimmt
    c2.advance(0.2)
    hw2._dac_tick()
    assert hw2.pump_actual_speed <= 1.0 + 1e-9             # kein Sprung auf den Sollwert


def test_b3_restart_mid_backwash_safe_state(config):
    sim = Sim(config, datetime(2026, 7, 8, 10, 0))

    async def main():
        sim.svc.start_backwash()
        await sim.vt.run_for(60)
        assert sim.hw.get_all_states()["valves"]["backwash"] is True
    run(main())
    # "Absturz": neuer Prozess mit frischer Hardware-Initialisierung
    gpio = RecordingGPIO()
    hw = HardwareController(config, gpio=gpio, i2c_bus=FlakyBus(), start_thread=False)
    assert gpio.state[config["gpio"]["backwash_relay"]] is True    # HIGH = Ventil zu
    assert hw.pump_speed == 0 and hw.pump_mains_connected
    svc = PumpService(hw, config, now=lambda: datetime(2026, 7, 8, 10, 1))
    assert svc.state.mode == "off" and not svc.backwash_running
    svc._apply_schedule()
    assert hw.pump_speed == 30 and hw.pump_actual_speed == 0      # Rampe ab 0


# ── B4 systemd (statisch prüfbar) ─────────────────────────────

def test_b4_unit_override_and_env_template():
    override = (ROOT / "pi" / "pool_marx.override.conf").read_text()
    assert "EnvironmentFile=-/etc/pool_marx/telemetry.env" in override
    assert "EnvironmentFile=/etc/pool_marx/secrets.env" in override
    example = (ROOT / "pi" / "telemetry.env.example").read_text()
    for line in example.splitlines():
        if line.startswith("OTEL_") and "=" in line:
            assert "<" not in line or line.startswith("#")
    assert "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf" in example
    assert "ApiKey%20<" in example                          # nur Platzhalter


def test_b4_starts_without_telemetry_env(config, monkeypatch):
    for v in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_PROTOCOL"):
        monkeypatch.delenv(v, raising=False)
    for enabled in (False, True):
        t = tel.setup_telemetry({"telemetry": {"enabled": enabled, "system_metrics": False}})
        assert t.enabled is enabled
        t.shutdown(timeout_s=1)


# ── B5 Last: 20 WebSocket-Clients + API ───────────────────────

def test_b5_many_websocket_clients_keep_dac_cadence(config):
    bus = FlakyBus()
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=bus, start_thread=True)
    svc = PumpService(hw, config)
    app = api_main.create_app(svc, object(), hw, {"secret_key": API_SECRET})
    try:
        with TestClient(app) as client, ExitStack() as stack:
            sockets = [stack.enter_context(client.websocket_connect("/ws", headers=AUTH))
                       for _ in range(20)]
            for ws in sockets:
                assert ws.receive_json()["type"] == "init"
            n = len(bus.attempt_times)
            t_end = time.monotonic() + 3.0
            i = 0
            while time.monotonic() < t_end:
                client.post(f"/api/pump/speed/{(i * 7) % 101}", headers=AUTH)
                client.post("/api/light", json={"action": "on" if i % 2 else "off"}, headers=AUTH)
                client.get("/api/status", headers=AUTH)
                i += 1
            assert i > 10
            assert max_interval(bus.attempt_times[n:], skip=0) < 3 * hw.dac.refresh_interval_s
            for ws in sockets:                       # alle Clients haben Updates bekommen
                assert ws.receive_json()["type"] == "state"
    finally:
        hw.cleanup()
