"""A8 — Telemetrie (OpenTelemetry) — alles lokal, In-Memory-Exporter bzw. tote Endpunkte"""

import logging
import socket
import sys
import threading
import time
from datetime import datetime

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from services import telemetry as tel
from services.hardware import HardwareController
from services.pump_service import PumpService
from tests.conftest import FlakyBus, RecordingGPIO, max_interval, run

DUMMY_KEY = "SUPERSECRET-APIKEY-123"
DUMMY_MQTT = "MQTT-PASS-456"
DUMMY_API = "DASHBOARD-PASS-789"


def tcfg(**kw):
    base = {"enabled": True, "system_metrics": False, "metrics_interval_s": 1}
    base.update(kw)
    return {"telemetry": base}


class FakePi(tel.PiHealth):
    def __init__(self, temp=48.3, bits="throttled=0x50005"):
        super().__init__(runner=lambda *a, **k: type("R", (), {"stdout": bits})())
        self._temp = temp

    def cpu_temp_c(self):
        return self._temp


@pytest.fixture
def mem():
    reader = InMemoryMetricReader()
    exporter = InMemoryLogRecordExporter()
    created = []

    def _setup(cfg=None, pi=None):
        t = tel.setup_telemetry(cfg or tcfg(), metric_readers=[reader], log_exporter=exporter,
                                pi_health=pi or FakePi())
        created.append(t)
        return t

    if tel._FORWARDER is not None:
        tel._FORWARDER._last = float("-inf")     # Ratenlimit pro Test zurücksetzen
    yield _setup, reader, exporter
    for t in created:
        t.shutdown(timeout_s=1)


def metrics_by_name(reader):
    out = {}
    data = reader.get_metrics_data()
    for rm in (data.resource_metrics if data else []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = (m.unit, [(dict(dp.attributes), getattr(dp, "value", None),
                                         getattr(dp, "count", None)) for dp in m.data.data_points])
    return out


def exported_logs(t, exporter):
    t.force_flush()
    return [(r.log_record.body, dict(r.log_record.attributes or {}), dict(r.resource.attributes))
            for r in exporter.get_finished_logs()]


# ── No-op ─────────────────────────────────────────────────────

@pytest.mark.parametrize("cfg", [{}, {"telemetry": None}, {"telemetry": {"enabled": False}},
                                 {"telemetry": {"enabled": "yes"}}])
def test_disabled_is_noop(cfg):
    threads = threading.active_count()
    handlers = list(logging.getLogger().handlers)
    t = tel.setup_telemetry(cfg)
    assert type(t) is tel.Telemetry and not t.enabled
    assert threading.active_count() == threads
    assert logging.getLogger().handlers == handlers
    # alle Methoden sicher aufrufbar
    t.register_sources(None, None); t.record_dac_write_ms(1.0)
    t.record_backwash_duration_s(1.0); t.instrument_fastapi(None); t.force_flush(); t.shutdown()


# ── Init-Fehler ───────────────────────────────────────────────

def test_missing_packages_are_caught(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.resources", None)
    with caplog.at_level(logging.WARNING):
        t = tel.setup_telemetry(tcfg())
    assert not t.enabled
    assert len([r for r in caplog.records if "Telemetrie deaktiviert" in r.getMessage()]) == 1


@pytest.mark.parametrize("endpoint", ["ftp://x", "not a url", "localhost:4318"])
def test_invalid_env_is_caught(monkeypatch, endpoint):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    assert not tel.setup_telemetry(tcfg()).enabled


def test_partial_init_failure_cleans_up(monkeypatch):
    handlers = list(logging.getLogger().handlers)
    monkeypatch.setattr(tel.OTelTelemetry, "_create_instruments",
                        lambda self: (_ for _ in ()).throw(RuntimeError("kaputt")))
    t = tel.setup_telemetry(tcfg(), metric_readers=[InMemoryMetricReader()],
                            log_exporter=InMemoryLogRecordExporter())
    assert not t.enabled
    assert logging.getLogger().handlers == handlers
    assert logging.getLogger("opentelemetry").propagate is True


def test_grpc_protocol_warns_but_works(monkeypatch, mem, caplog):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    setup, *_ = mem
    with caplog.at_level(logging.WARNING):
        assert setup().enabled
    assert any("http/protobuf" in r.getMessage() for r in caplog.records)


def test_export_errors_are_rate_limited(mem, caplog):
    setup, *_ = mem
    setup()
    with caplog.at_level(logging.WARNING):
        lib = logging.getLogger("opentelemetry.exporter.otlp.proto.http._log_exporter")
        for _ in range(50):
            lib.error("Failed to export logs batch code: 503")
    fwd = [r for r in caplog.records if "Telemetrie-Export gestört" in r.getMessage()]
    assert len(fwd) == 1


def test_invalid_config_values_fallback(caplog):
    with caplog.at_level(logging.WARNING):
        cfg = tel.load_telemetry_config({"telemetry": {"enabled": True, "metrics_interval_s": -3,
                                                       "log_level": "LOUD"}})
    assert cfg["metrics_interval_s"] == 15 and cfg["log_level"] == "INFO"


# ── Metriken ──────────────────────────────────────────────────

def test_observable_metrics_plausible(mem, config):
    setup, reader, _ = mem
    t = setup()
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=FlakyBus(), start_thread=False)
    svc = PumpService(hw, config, now=lambda: datetime(2026, 1, 7, 2, 0))
    t.register_sources(hw, svc)
    svc.resume_schedule()                      # Winter-Nacht → Mindestdrehzahl 20 %
    for i in range(10):
        hw._dac_tick(now=1 + i * 0.2)
    hw.light_on()
    svc.record_fault_note("test")
    t.record_dac_write_ms(0.8)
    t.record_backwash_duration_s(215.0, "ok")
    t.record_backwash_duration_s(1.0, "irgendwas")   # → result=error (endliche Menge)
    m = metrics_by_name(reader)

    assert m["pool.pump.setpoint"] == ("%", [({}, 20.0, None)])
    unit, pts = m["pool.pump.actual"]
    assert unit == "%" and 0 < pts[0][1] <= 20
    unit, pts = m["pool.pump.dac_output"]
    assert unit == "V" and 0 < pts[0][1] < 2.0
    modes = {p[0]["mode"]: p[1] for p in m["pool.pump.mode"][1]}
    assert modes["schedule"] == 1 and sum(modes.values()) == 1
    assert set(modes) == set(tel.PUMP_MODES)
    assert m["pool.pump.mains_connected"][1][0][1] == 1
    assert m["pool.light.on"][1][0][1] == 1
    assert m["pool.level.ok"][1][0][1] == 1
    assert m["pool.frost.active"][1][0][1] == 0
    assert m["pool.pump.min_speed_active"][1][0][1] == 1
    reasons = {p[0]["reason"]: p[1] for p in m["pool.pump.setpoint_changes"][1]}
    assert reasons["min_speed"] == 1 and set(reasons) <= set(tel.SETPOINT_REASONS)
    assert m["pool.dac.write_errors"] == ("1", [({}, 0, None)])
    assert m["pool.pump.fault_notes"][1][0][1] == 1
    assert m["pool.backwash.runs"][1][0][1] == 0
    assert m["pool.dac.write_duration"][0] == "ms"
    bw = {p[0]["result"]: p[2] for p in m["pool.backwash.duration"][1]}
    assert m["pool.backwash.duration"][0] == "s" and bw == {"ok": 1, "error": 1}
    assert m["pi.cpu.temperature"] == ("Cel", [({}, 48.3, None)])
    assert m["pi.undervoltage_now"][1][0][1] == 1
    assert m["pi.undervoltage_occurred"][1][0][1] == 1
    assert m["pi.throttled_now"][1][0][1] == 1
    assert m["pi.throttled_occurred"][1][0][1] == 1
    assert m["pi.freq_capped_now"][1][0][1] == 0


def test_callbacks_without_sources_or_values_dont_raise(mem):
    setup, reader, _ = mem
    setup(pi=tel.PiHealth(thermal_path="/nonexistent", vcgencmd="definitely-not-installed-xyz"))
    m = metrics_by_name(reader)      # keine Quellen registriert, keine Pi-Werte
    assert "pi.cpu.temperature" not in m and "pool.pump.setpoint" not in m


def test_broken_source_does_not_break_collection(mem):
    setup, reader, _ = mem
    t = setup()

    class Broken:
        def get_dac_snapshot(self):
            raise RuntimeError("weg")
    t.register_sources(Broken(), None)
    m = metrics_by_name(reader)
    assert "pi.cpu.temperature" in m


def test_metric_attributes_low_cardinality(mem, config):
    setup, reader, _ = mem
    t = setup()
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=FlakyBus(), start_thread=False)
    svc = PumpService(hw, config)
    t.register_sources(hw, svc)
    svc.record_fault_note("freier Text " * 20)
    hw._set_target(30, "irgendein-unbekannter-grund")
    allowed = {"mode": set(tel.PUMP_MODES), "reason": set(tel.SETPOINT_REASONS),
               "result": set(tel.BACKWASH_RESULTS)}
    for name, (_, pts) in metrics_by_name(reader).items():
        if not name.startswith(("pool.", "pi.")):
            continue
        for attrs, *_ in pts:
            for k, v in attrs.items():
                assert k in allowed and v in allowed[k], (name, k, v)


# ── Pi ────────────────────────────────────────────────────────

@pytest.mark.parametrize("out,expect", [
    ("throttled=0x0", {"undervoltage_now": 0, "undervoltage_occurred": 0, "throttled_now": 0}),
    ("throttled=0x50005", {"undervoltage_now": 1, "throttled_now": 1, "undervoltage_occurred": 1,
                           "throttled_occurred": 1, "freq_capped_now": 0}),
    ("throttled=0x50000", {"undervoltage_now": 0, "throttled_now": 0, "undervoltage_occurred": 1,
                           "throttled_occurred": 1}),
    ("throttled=0x1", {"undervoltage_now": 1, "undervoltage_occurred": 0}),
])
def test_parse_throttled(out, expect):
    bits = tel.parse_throttled(out)
    for k, v in expect.items():
        assert bits[k] == v


def test_parse_throttled_garbage():
    assert tel.parse_throttled("") is None and tel.parse_throttled("error") is None


def test_pi_health_missing_tools_and_files(tmp_path):
    pi = tel.PiHealth(thermal_path=str(tmp_path / "nope"), vcgencmd="definitely-not-installed-xyz")
    assert pi.cpu_temp_c() is None
    assert pi.throttled() is None and pi.throttled() is None   # danach dauerhaft übersprungen
    f = tmp_path / "temp"
    f.write_text("48312\n")
    assert tel.PiHealth(thermal_path=str(f)).cpu_temp_c() == pytest.approx(48.312)


def test_pi_health_caches_and_survives_errors():
    calls = []

    def runner(*a, **k):
        calls.append(1)
        raise TimeoutError
    pi = tel.PiHealth(runner=runner, cache_s=100)
    assert pi.throttled() is None and pi.throttled() is None
    assert len(calls) == 1


# ── Logs / Events / Secrets ───────────────────────────────────

def test_log_events_carry_event_name_and_attributes(mem, config):
    setup, _, exporter = mem
    t = setup()
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=FlakyBus(), start_thread=False)
    svc = PumpService(hw, config, now=lambda: datetime(2026, 7, 8, 10, 0))
    logging.getLogger("services").setLevel(logging.INFO)
    try:
        svc.resume_schedule()
        svc.record_fault_note("E-04 nach Umschaltung")
        hw.emergency_stop()
        hw.pump_mains_restore()
        logs = exported_logs(t, exporter)
    finally:
        logging.getLogger("services").setLevel(logging.NOTSET)
    by_event = {}
    for body, attrs, _ in logs:
        if "event.name" in attrs:
            by_event.setdefault(attrs["event.name"], attrs)
    for name in ("schedule.transition", "pump.setpoint_changed", "pump.fault_note",
                 "emergency_stop", "pump.mains_cutoff", "pump.mains_restore"):
        assert name in by_event, name
    sc = by_event["pump.setpoint_changed"]
    assert sc["pump.setpoint.old"] == 0 and sc["pump.setpoint.new"] == 30
    assert sc["pump.setpoint.reason"] == "schedule"
    fn = by_event["pump.fault_note"]
    assert fn["fault.note"] == "E-04 nach Umschaltung" and fn["pump.mode"] == "schedule"


def test_no_secrets_anywhere(monkeypatch, config):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", f"Authorization=ApiKey%20{DUMMY_KEY}")
    monkeypatch.setenv("POOL_MQTT_PASSWORD", DUMMY_MQTT)
    monkeypatch.setenv("POOL_API_SECRET", DUMMY_API)
    import main as main_mod
    cfg = dict(config)
    main_mod.apply_env_secrets(cfg)
    assert cfg["mqtt"]["password"] == DUMMY_MQTT
    reader, exporter = InMemoryMetricReader(), InMemoryLogRecordExporter()
    t = tel.setup_telemetry({**cfg, "telemetry": {"enabled": True, "system_metrics": False}},
                            metric_readers=[reader], log_exporter=exporter, pi_health=FakePi())
    try:
        hw = HardwareController(cfg, gpio=RecordingGPIO(), i2c_bus=FlakyBus(), start_thread=False)
        svc = PumpService(hw, cfg)
        t.register_sources(hw, svc)
        logging.getLogger("services").setLevel(logging.INFO)
        hw.pump_on(40)
        svc.record_fault_note("Notiz")
        # selbst wenn jemand versehentlich ein Secret als Attribut übergibt:
        from services.events import log_event
        log_event(logging.getLogger("services.x"), logging.WARNING, "test", "x",
                  password=DUMMY_MQTT, api_key=DUMMY_KEY, authorization=DUMMY_KEY)
        blob = repr(exported_logs(t, exporter)) + repr(metrics_by_name(reader)) + repr(t.resource.attributes)
    finally:
        logging.getLogger("services").setLevel(logging.NOTSET)
        t.shutdown(timeout_s=1)
    for secret in (DUMMY_KEY, DUMMY_MQTT, DUMMY_API):
        assert secret not in blob


def test_otel_library_logs_not_reexported(mem):
    setup, _, exporter = mem
    t = setup()
    logging.getLogger("opentelemetry.sdk.test").warning("intern")
    bodies = [str(b) for b, *_ in exported_logs(t, exporter)]
    assert "intern" not in bodies                       # roher SDK-Eintrag nicht exportiert
    assert sum("Telemetrie-Export gestört" in b for b in bodies) <= 1   # nur die Sammelwarnung


def test_dac_writes_produce_no_logs_or_points(mem, config):
    setup, reader, exporter = mem
    t = setup()
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=FlakyBus(), start_thread=False)
    hw.set_write_duration_hook(t.record_dac_write_ms)
    t.register_sources(hw, None)
    exported_logs(t, exporter)
    exporter.clear()
    for i in range(250):
        hw._dac_tick(now=1 + i * 0.2)
    assert exported_logs(t, exporter) == []
    count = metrics_by_name(reader)["pool.dac.write_duration"][1][0][2]
    assert count <= 250 // hw._duration_sample_every + 1     # Stichprobe, nicht pro Write


def test_fastapi_instrumentation_switch(mem, config):
    from fastapi import FastAPI
    setup, *_ = mem
    app = FastAPI()
    setup(tcfg(fastapi_instrumentation=True)).instrument_fastapi(app)
    setup(tcfg()).instrument_fastapi(FastAPI())   # aus → nichts


def test_system_metrics_switch(config):
    reader = InMemoryMetricReader()
    t = tel.setup_telemetry(tcfg(system_metrics=True), metric_readers=[reader],
                            log_exporter=InMemoryLogRecordExporter(), pi_health=FakePi())
    try:
        names = set(metrics_by_name(reader))
        assert any(n.startswith("system.") or n.startswith("process.") for n in names)
    finally:
        t.shutdown(timeout_s=1)


# ── Unerreichbare Endpunkte ───────────────────────────────────

def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def blackhole():
    """Nimmt Verbindungen an (Kernel-Backlog), antwortet aber nie → Export-Timeout."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(50)
    yield s.getsockname()[1]
    s.close()


@pytest.mark.parametrize("kind", ["closed", "blackhole"])
def test_unreachable_endpoint_keeps_dac_cadence(monkeypatch, config, kind, request):
    port = _closed_port() if kind == "closed" else request.getfixturevalue("blackhole")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://127.0.0.1:{port}")
    t0 = time.monotonic()
    t = tel.setup_telemetry(tcfg(metrics_interval_s=1))
    assert t.enabled and time.monotonic() - t0 < 1.0     # Start blockiert nicht
    bus = FlakyBus()
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=bus, start_thread=True)
    t.register_sources(hw, None)
    hw.set_write_duration_hook(t.record_dac_write_ms)
    log = logging.getLogger("services.lasttest")
    log.setLevel(logging.INFO)
    try:
        hw.pump_on(60)
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            log.info("Lastzeile", extra={"event.name": "test.load"})
            time.sleep(0.01)
        assert max_interval(bus.attempt_times) < 3 * hw.dac.refresh_interval_s
        assert hw.dac_thread_alive
    finally:
        log.setLevel(logging.NOTSET)
        hw.cleanup()
        s = time.monotonic()
        t.shutdown(timeout_s=3.0)
        assert time.monotonic() - s < 5.0                  # Shutdown in wenigen Sekunden
