"""
Pool Marx — Telemetrie (OpenTelemetry Logs + Metriken, Export per OTLP/HTTP)

Herstellerneutral: Standard-OpenTelemetry-SDK, Ziel z. B. Elastic Observability
(direkt oder über einen lokalen Collector, siehe scripts/otelcol-pi.yaml).

Wichtigste Regel: Telemetrie darf die Pumpensteuerung nie stören.
- Exporte laufen nur in den Hintergrund-Threads des SDK (BatchLogRecordProcessor,
  PeriodicExportingMetricReader) mit begrenzter Queue und Timeout (5 s).
- Metriken sind fast ausschließlich Observable-Instrumente: Die Werte werden im
  Metrik-Thread per Callback gelesen — DAC-Thread und Zeitplan-Schleife rufen
  hier nichts auf. Einzige Ausnahme: zwei Histogramme (DAC-Schreibdauer, nur
  alle ~5 s gesampelt; Rückspüldauer, einmal pro Rückspülung) — reine
  In-Memory-Aggregation, kein Netzwerkzugriff.
- setup_telemetry() wirft nie. Bei Fehlern/abgeschalteter Telemetrie wird ein
  No-op-Objekt zurückgegeben; Export-Fehler werden lokal nur ratenbegrenzt geloggt.
- Endpunkt/API-Key kommen ausschließlich aus den Standard-Umgebungsvariablen
  OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS / _PROTOCOL (nie aus config.yaml).
"""

import logging
import math
import os
import re
import socket
import subprocess
import threading
import time
import warnings
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SERVICE_NAME = "pool_marx"
SERVICE_VERSION = "1.0.0"
EXPORT_TIMEOUT_S = 5.0
LOG_QUEUE_SIZE = 2048
LOCAL_WARN_INTERVAL_S = 300.0    # Export-Fehler höchstens alle 5 Min lokal melden

TELEMETRY_DEFAULTS = {
    "enabled": False,
    "metrics_interval_s": 15,
    "log_level": "INFO",
    "fastapi_instrumentation": False,
    "system_metrics": True,
}

PUMP_MODES = ("schedule", "manual", "frost", "backwash", "off", "emergency")
SETPOINT_REASONS = ("schedule", "min_speed", "manual", "frost", "backwash",
                    "emergency", "mains_cutoff", "shutdown", "other")
BACKWASH_RESULTS = ("ok", "cancelled", "error")

# vcgencmd get_throttled — Bitbelegung
THROTTLE_BITS = {
    "undervoltage_now": 0,
    "freq_capped_now": 1,
    "throttled_now": 2,
    "soft_temp_limit_now": 3,
    "undervoltage_occurred": 16,
    "freq_capped_occurred": 17,
    "throttled_occurred": 18,
    "soft_temp_limit_occurred": 19,
}


# ── Konfiguration ─────────────────────────────────────────────

def load_telemetry_config(config: dict) -> dict:
    """Abschnitt telemetry: lesen; fehlt er → Standardwerte (aus)."""
    raw = (config or {}).get("telemetry") or {}
    cfg = dict(TELEMETRY_DEFAULTS)
    cfg["enabled"] = raw.get("enabled", False) is True
    try:
        interval = float(raw.get("metrics_interval_s", TELEMETRY_DEFAULTS["metrics_interval_s"]))
        if not math.isfinite(interval) or interval < 1 or interval > 3600:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning(f"telemetry.metrics_interval_s={raw.get('metrics_interval_s')!r} "
                       f"ungültig — verwende {TELEMETRY_DEFAULTS['metrics_interval_s']}")
        interval = TELEMETRY_DEFAULTS["metrics_interval_s"]
    cfg["metrics_interval_s"] = interval
    level = str(raw.get("log_level", "INFO")).upper()
    if not isinstance(logging.getLevelName(level), int):
        logger.warning(f"telemetry.log_level={raw.get('log_level')!r} ungültig — verwende INFO")
        level = "INFO"
    cfg["log_level"] = level
    cfg["fastapi_instrumentation"] = raw.get("fastapi_instrumentation", False) is True
    cfg["system_metrics"] = raw.get("system_metrics", True) is True
    return cfg


def _safe_endpoint() -> str:
    """Endpunkt für das Log — ohne evtl. Zugangsdaten in der URL."""
    ep = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318 (Standard)")
    return re.sub(r"//[^/@]*@", "//***@", ep)


def _check_env():
    """Ungültige Umgebungsvariablen früh erkennen (→ Telemetrie aus, Dienst läuft)."""
    ep = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if ep and not re.match(r"^https?://[^\s/]+", ep):
        raise ValueError("OTEL_EXPORTER_OTLP_ENDPOINT muss mit http:// oder https:// beginnen")
    proto = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
    if proto and proto != "http/protobuf":
        logger.warning(f"OTEL_EXPORTER_OTLP_PROTOCOL={proto!r} wird nicht unterstützt — "
                       f"verwende http/protobuf")


# ── Pi-Zustand ────────────────────────────────────────────────

def parse_throttled(output: str) -> Optional[dict]:
    """'throttled=0x50005' → {'undervoltage_now': 1, ...}. Ungültig → None."""
    m = re.search(r"throttled=(0x[0-9a-fA-F]+|\d+)", output or "")
    if not m:
        return None
    value = int(m.group(1), 0)
    return {name: (value >> bit) & 1 for name, bit in THROTTLE_BITS.items()}


class PiHealth:
    """CPU-Temperatur und Unterspannung/Throttling. Fehlt etwas → None (übersprungen)."""

    def __init__(self, thermal_path: str = "/sys/class/thermal/thermal_zone0/temp",
                 vcgencmd: str = "vcgencmd", cache_s: float = 10.0,
                 runner: Callable = subprocess.run):
        self.thermal_path = thermal_path
        self.vcgencmd = vcgencmd
        self.cache_s = cache_s
        self._runner = runner
        self._vcgencmd_available = True
        self._cache: Optional[dict] = None
        self._cache_t = -math.inf
        self._lock = threading.Lock()

    def cpu_temp_c(self) -> Optional[float]:
        try:
            with open(self.thermal_path) as f:
                return int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            return None

    def throttled(self) -> Optional[dict]:
        with self._lock:
            if not self._vcgencmd_available:
                return None
            now = time.monotonic()
            if now - self._cache_t < self.cache_s:
                return self._cache
            try:
                res = self._runner([self.vcgencmd, "get_throttled"], capture_output=True,
                                   text=True, timeout=2)
                self._cache = parse_throttled(res.stdout)
            except FileNotFoundError:
                self._vcgencmd_available = False
                logger.info("vcgencmd nicht gefunden — Unterspannungs-Metriken übersprungen")
                self._cache = None
            except Exception:
                self._cache = None
            self._cache_t = now
            return self._cache


# ── Logging-Hilfen ────────────────────────────────────────────

class _RateLimitedForwarder(logging.Handler):
    """
    Meldungen der OTel-Bibliotheken (z. B. 'Failed to export') höchstens alle
    LOCAL_WARN_INTERVAL_S an den normalen Logger weiterreichen — sonst würde bei
    Netzausfall jede Export-Runde eine Zeile erzeugen.
    """

    def __init__(self, interval_s: float = LOCAL_WARN_INTERVAL_S):
        super().__init__(level=logging.WARNING)
        self.interval_s = interval_s
        self._last = -math.inf
        self._suppressed = 0
        self._guard = threading.Lock()
        self._out = logging.getLogger("pool_marx.telemetry")

    def emit(self, record: logging.LogRecord):
        now = time.monotonic()
        with self._guard:
            if now - self._last < self.interval_s:
                self._suppressed += 1
                return
            self._last = now
            suppressed, self._suppressed = self._suppressed, 0
        try:
            msg = record.getMessage().splitlines()[0][:300]
        except Exception:
            msg = "?"
        extra = f" ({suppressed} weitere unterdrückt)" if suppressed else ""
        self._out.warning(f"Telemetrie-Export gestört: {msg}{extra} — Steuerung läuft normal weiter")


_FORWARDER: Optional[_RateLimitedForwarder] = None
_ORIG_STATE: tuple = (True, logging.NOTSET)


def _install_forwarder() -> tuple:
    """Forwarder einmal pro Prozess installieren (idempotent). Gibt (Handler, alter Zustand)."""
    global _FORWARDER, _ORIG_STATE
    lib = logging.getLogger("opentelemetry")
    if _FORWARDER is None or _FORWARDER not in lib.handlers:
        _ORIG_STATE = (lib.propagate, lib.level)     # Zustand VOR der ersten Installation
        _FORWARDER = _RateLimitedForwarder()
        lib.addHandler(_FORWARDER)
    lib.propagate = False
    return _FORWARDER, _ORIG_STATE


class _DropOtelRecords(logging.Filter):
    """Eigene Meldungen des SDK nicht erneut exportieren (keine Rückkopplung)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("opentelemetry")


# ── Telemetrie-Objekte ────────────────────────────────────────

class Telemetry:
    """No-op-Telemetrie (Standard). Alle Methoden sind sicher aufrufbar."""
    enabled = False

    def register_sources(self, hardware=None, pump_service=None):
        pass

    def record_dac_write_ms(self, duration_ms: float):
        pass

    def record_backwash_duration_s(self, duration_s: float, result: str = "ok"):
        pass

    def instrument_fastapi(self, app):
        pass

    def force_flush(self, timeout_s: float = 3.0):
        pass

    def shutdown(self, timeout_s: float = 3.0):
        pass


class OTelTelemetry(Telemetry):
    enabled = True

    def __init__(self, cfg: dict, *, metric_readers=None, log_exporter=None,
                 pi_health: Optional[PiHealth] = None):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        self.cfg = cfg
        self._hardware = None
        self._pump = None
        self._pi = pi_health or PiHealth()
        self._handler: Optional[logging.Handler] = None
        self._forwarder: Optional[logging.Handler] = None
        self._otel_logger_state = None
        self._shutdown_done = False
        self.logger_provider = None
        self.meter_provider = None

        try:
            hostname = socket.gethostname()
            self.resource = Resource.create({
                "service.name": SERVICE_NAME,
                "service.version": SERVICE_VERSION,
                "service.instance.id": hostname,
                "host.name": hostname,
                "deployment.environment": "home",
            })

            # OTel-Bibliotheksmeldungen: nicht an Root propagieren, nur ratenbegrenzt
            self._forwarder, self._otel_logger_state = _install_forwarder()

            # ── Logs
            if log_exporter is None:
                from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
                log_exporter = OTLPLogExporter(timeout=EXPORT_TIMEOUT_S)
            self.logger_provider = LoggerProvider(resource=self.resource)
            self.logger_provider.add_log_record_processor(BatchLogRecordProcessor(
                log_exporter,
                max_queue_size=LOG_QUEUE_SIZE,
                export_timeout_millis=int(EXPORT_TIMEOUT_S * 1000),
            ))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                handler = LoggingHandler(level=logging.getLevelName(cfg["log_level"]),
                                         logger_provider=self.logger_provider)
            handler.addFilter(_DropOtelRecords())
            # Root-Logger: nur ERGÄNZEN — bestehende Stream-/Datei-Handler bleiben
            logging.getLogger().addHandler(handler)
            self._handler = handler

            # ── Metriken
            if metric_readers is None:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
                metric_readers = [PeriodicExportingMetricReader(
                    OTLPMetricExporter(timeout=EXPORT_TIMEOUT_S),
                    export_interval_millis=int(cfg["metrics_interval_s"] * 1000),
                    export_timeout_millis=int(EXPORT_TIMEOUT_S * 1000),
                )]
            self.meter_provider = MeterProvider(resource=self.resource,
                                                metric_readers=list(metric_readers))
            self._create_instruments()

            if cfg.get("system_metrics"):
                self._enable_system_metrics()
        except Exception:
            self._teardown(timeout_s=1.0, restore_logging=True)
            raise

        logger.info(f"Telemetrie aktiv: OTLP/HTTP → {_safe_endpoint()}, "
                    f"Metriken alle {cfg['metrics_interval_s']:g}s")

    # ── Instrumente

    def _create_instruments(self):
        from opentelemetry.metrics import Observation, CallbackOptions  # noqa: F401
        m = self.meter_provider.get_meter("pool_marx", SERVICE_VERSION)
        self._Observation = Observation

        def gauge(name, unit, desc, fn):
            m.create_observable_gauge(name, callbacks=[self._safe(fn)], unit=unit, description=desc)

        def counter(name, unit, desc, fn):
            m.create_observable_counter(name, callbacks=[self._safe(fn)], unit=unit, description=desc)

        gauge("pool.pump.setpoint", "%", "Pumpen-Sollwert", self._obs_setpoint)
        gauge("pool.pump.actual", "%", "Pumpen-Istwert (Rampe)", self._obs_actual)
        gauge("pool.pump.dac_output", "V", "Berechnete Ausgangsspannung 0-10V", self._obs_dac_v)
        gauge("pool.pump.mode", "1", "Aktueller Pumpenmodus (1 = aktiv)", self._obs_mode)
        gauge("pool.pump.mains_connected", "1", "Pumpe hat Netzspannung (Relais)", self._obs_mains)
        gauge("pool.light.on", "1", "Beleuchtung an", self._obs_light)
        gauge("pool.level.ok", "1", "Wasserstand OK", self._obs_level)
        gauge("pool.frost.active", "1", "Frostschutz aktiv", self._obs_frost)
        gauge("pool.pump.min_speed_active", "1", "Mindestdrehzahl ersetzt Zeitplan-0%", self._obs_min_speed)

        counter("pool.pump.setpoint_changes", "1", "Sollwertänderungen nach Grund", self._obs_setpoint_changes)
        counter("pool.dac.write_errors", "1", "DAC-Schreibfehler (nach Retry)", self._obs_dac_errors)
        counter("pool.backwash.runs", "1", "Gestartete Rückspülungen", self._obs_backwash_runs)
        counter("pool.pump.fault_notes", "1", "Pumpenfehler-Notizen", self._obs_fault_notes)

        self._hist_dac = m.create_histogram(
            "pool.dac.write_duration", unit="ms",
            description="I²C-Schreibdauer DAC (Stichprobe alle ~5 s)")
        self._hist_backwash = m.create_histogram(
            "pool.backwash.duration", unit="s", description="Dauer der Rückspülung")

        gauge("pi.cpu.temperature", "Cel", "CPU-Temperatur", self._obs_cpu_temp)
        for name in THROTTLE_BITS:
            gauge(f"pi.{name}", "1", f"vcgencmd get_throttled: {name}", self._obs_throttle(name))

    def _safe(self, fn):
        """Callback darf nie werfen; fehlende Quelle/Wert → keine Messung."""
        def cb(options):
            try:
                return list(fn())
            except Exception:
                return []
        return cb

    def _o(self, value, attrs=None):
        return self._Observation(value, attrs or {})

    def _snap(self) -> Optional[dict]:
        return self._hardware.get_dac_snapshot() if self._hardware is not None else None

    def _obs_setpoint(self):
        s = self._snap()
        return [self._o(s["target_pct"])] if s else []

    def _obs_actual(self):
        s = self._snap()
        return [self._o(s["actual_pct"])] if s else []

    def _obs_dac_v(self):
        s = self._snap()
        return [self._o(round(s["output_v"], 3))] if s else []

    def _obs_mode(self):
        if self._pump is None:
            return []
        mode = self._pump.get_metrics_snapshot()["mode"]
        return [self._o(1 if mode == m else 0, {"mode": m}) for m in PUMP_MODES]

    def _obs_mains(self):
        hw = self._hardware
        return [self._o(int(hw.pump_mains_connected))] if hw is not None else []

    def _obs_light(self):
        hw = self._hardware
        return [self._o(int(hw.light_is_on))] if hw is not None else []

    def _obs_level(self):
        hw = self._hardware
        return [self._o(int(hw.read_level_switch()))] if hw is not None else []

    def _obs_frost(self):
        return [self._o(int(self._pump.get_metrics_snapshot()["frost_active"]))] if self._pump else []

    def _obs_min_speed(self):
        return [self._o(int(self._pump.get_metrics_snapshot()["min_speed_active"]))] if self._pump else []

    def _obs_setpoint_changes(self):
        s = self._snap()
        if not s:
            return []
        return [self._o(n, {"reason": r}) for r, n in s["setpoint_changes"].items()
                if r in SETPOINT_REASONS]

    def _obs_dac_errors(self):
        s = self._snap()
        return [self._o(s["write_errors"])] if s else []

    def _obs_backwash_runs(self):
        return [self._o(self._pump.get_metrics_snapshot()["backwash_runs"])] if self._pump else []

    def _obs_fault_notes(self):
        return [self._o(self._pump.get_metrics_snapshot()["fault_notes"])] if self._pump else []

    def _obs_cpu_temp(self):
        t = self._pi.cpu_temp_c()
        return [self._o(t)] if t is not None else []

    def _obs_throttle(self, name):
        def fn():
            bits = self._pi.throttled()
            return [self._o(bits[name])] if bits else []
        return fn

    def _enable_system_metrics(self):
        try:
            from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
        except Exception as e:
            logger.warning(f"telemetry.system_metrics: Paket nicht verfügbar ({e}) — übersprungen")
            return
        try:
            cfg = {
                "system.cpu.utilization": ["user", "system", "idle", "iowait"],
                "system.memory.usage": ["used", "free", "cached"],
                "system.memory.utilization": ["used", "free", "cached"],
                "system.disk.io": ["read", "write"],
                "system.network.io": ["transmit", "receive"],
                "process.cpu.utilization": None,
                "process.memory.usage": None,
            }
            self._sysmetrics = SystemMetricsInstrumentor(config=cfg)
            self._sysmetrics.instrument(meter_provider=self.meter_provider)
        except Exception as e:
            logger.warning(f"System-Metriken konnten nicht aktiviert werden: {e}")

    # ── öffentliche API

    def register_sources(self, hardware=None, pump_service=None):
        self._hardware = hardware
        self._pump = pump_service

    def record_dac_write_ms(self, duration_ms: float):
        try:
            self._hist_dac.record(duration_ms)
        except Exception:
            pass

    def record_backwash_duration_s(self, duration_s: float, result: str = "ok"):
        try:
            self._hist_backwash.record(
                duration_s, {"result": result if result in BACKWASH_RESULTS else "error"})
        except Exception:
            pass

    def instrument_fastapi(self, app):
        if not self.cfg.get("fastapi_instrumentation"):
            return
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app, meter_provider=self.meter_provider,
                                               excluded_urls="api/health,ws")
        except Exception as e:
            logger.warning(f"FastAPI-Instrumentierung nicht möglich: {e}")

    def force_flush(self, timeout_s: float = 3.0):
        ms = int(timeout_s * 1000)
        for p in (self.logger_provider, self.meter_provider):
            try:
                if p is not None:
                    p.force_flush(ms)
            except Exception:
                pass

    def shutdown(self, timeout_s: float = 3.0):
        """Flush + Shutdown in einem Hilfsthread mit hartem Zeitlimit."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        t = threading.Thread(target=self._teardown, args=(timeout_s,),
                             name="otel-shutdown", daemon=True)
        t.start()
        t.join(timeout_s + 1.0)
        if t.is_alive():
            logger.warning("Telemetrie-Shutdown hat das Zeitlimit überschritten — übersprungen")

    def _teardown(self, timeout_s: float, restore_logging: bool = False):
        root = logging.getLogger()
        if self._handler is not None:
            root.removeHandler(self._handler)
        half_ms = int(timeout_s * 500)
        if getattr(self, "_sysmetrics", None) is not None:
            try:
                self._sysmetrics.uninstrument()
            except Exception:
                pass
        for p in (self.logger_provider, self.meter_provider):
            if p is None:
                continue
            try:
                p.force_flush(half_ms)
            except Exception:
                pass
            try:
                if p is self.meter_provider:
                    p.shutdown(timeout_millis=half_ms)
                else:
                    p.shutdown()
            except Exception:
                pass
        # Der ratenbegrenzte Forwarder bleibt nach einem normalen Shutdown aktiv:
        # hängende Export-Threads (z. B. Blackhole-Endpunkt) können noch Meldungen
        # erzeugen. Nur bei fehlgeschlagener Initialisierung alles zurücksetzen.
        if restore_logging:
            otel_lib_logger = logging.getLogger("opentelemetry")
            if self._forwarder is not None:
                otel_lib_logger.removeHandler(self._forwarder)
            if self._otel_logger_state is not None:
                otel_lib_logger.propagate, _ = self._otel_logger_state


def setup_telemetry(config: dict, *, metric_readers=None, log_exporter=None,
                    pi_health: Optional[PiHealth] = None) -> Telemetry:
    """
    Telemetrie einrichten. Ist telemetry.enabled nicht true → No-op (keine
    Threads, Handler oder Verbindungen). Wirft nie Exceptions.
    """
    try:
        cfg = load_telemetry_config(config)
        if not cfg["enabled"]:
            return Telemetry()
        _check_env()
        return OTelTelemetry(cfg, metric_readers=metric_readers,
                             log_exporter=log_exporter, pi_health=pi_health)
    except Exception as e:
        logger.warning(f"Telemetrie deaktiviert — Initialisierung fehlgeschlagen: "
                       f"{type(e).__name__}: {e}")
        return Telemetry()
