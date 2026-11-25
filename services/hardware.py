"""
Pool Marx — Hardware Abstraction Layer
Steuert GPIO, Relais und MCP4725 DAC
Läuft auf Raspberry Pi 4

DAC-Ausgabe (Pumpe 0-10V, Steuerart 3 an der Speck BADU Alpha Eco Soft):
- pump_on()/pump_off()/pump_set_speed() setzen nur einen Sollwert.
- Ein Daemon-Thread fährt den Istwert alle refresh_interval_s (0,2 s) mit
  höchstens ramp_pct_per_s (5 %/s) in Richtung Sollwert und schreibt den DAC
  bei JEDEM Durchlauf neu (zugleich Refresh gegen einen DAC-Reset auf den
  EEPROM-Startwert).
- Not-Aus/Netztrennung/cleanup() setzen Soll und Ist sofort auf 0 und
  schreiben 0 V ohne Rampe.
- Das 230V-Relais schaltet die Pumpe NICHT im Normalbetrieb — nur
  pump_mains_cutoff()/pump_mains_restore() (Not-Aus/Wartung).
"""

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from .dimmer import create_dimmer, load_dimmer_settings
from .events import log_event

logger = logging.getLogger(__name__)

# Simulationsmodus wenn keine echte GPIO Hardware vorhanden
try:
    import RPi.GPIO as GPIO
    import smbus2
    SIMULATION = False
    logger.info("RPi.GPIO gefunden — echter Hardware-Modus")
except ImportError:
    SIMULATION = True
    logger.warning("RPi.GPIO nicht gefunden — Simulationsmodus aktiv")


# ── MCP4725 DAC Konstanten ────────────────────────────────────
MCP4725_ADDR    = 0x60      # Standard I²C Adresse
MCP4725_CMD     = 0x40      # Write DAC Register (ohne EEPROM)
DAC_MAX         = 4095      # 12-Bit DAC Maximum
PUMP_SIGNAL_MAX = 10.0      # 0-10V Pumpeneingang: 10V = 100% (nominal)
# Harte Obergrenze für max_output_v: der Pumpeneingang ist 0-10V
ABS_MAX_OUTPUT_V = 10.0

# Standardwerte für den Abschnitt "dac:" in config.yaml
DAC_DEFAULTS = {
    "vdd": 3.3,                 # MCP4725 Versorgung (NICHT 5V)
    "opamp_gain": 3.2,          # LM358: 1 + 2,2k/1k
    "max_output_v": 9.5,        # Spannung bei 100 %
    "ramp_pct_per_s": 5.0,      # max. Drehzahländerung
    "refresh_interval_s": 0.2,  # Takt des DAC-Threads
}
# Gemessene Kalibrierung (pump.dac_full_scale_v) wird nur akzeptiert, wenn sie
# höchstens so weit vom Nennwert vdd × opamp_gain abweicht (Schutz vor Tippfehlern)
CALIBRATION_MAX_DEVIATION = 0.15
MAX_ABS_OFFSET_V = 0.5

# Gründe für Sollwertänderungen (endliche Menge → Metrik-Attribut "reason")
SETPOINT_REASONS = frozenset({
    "schedule", "min_speed", "manual", "frost", "backwash",
    "emergency", "mains_cutoff", "shutdown", "other",
})

DAC_ERROR_LOG_INTERVAL_S = 60.0   # I²C-Fehler höchstens 1× pro Minute loggen
DAC_DURATION_SAMPLE_S = 5.0       # Schreibdauer nur alle ~5 s als Metrik


def _cfg_number(section: dict, key: str, default: float, lo: float, hi: float,
                where: str) -> float:
    """Zahl aus Config lesen; ungültig/außerhalb (lo, hi] → Standardwert + Warnung."""
    raw = section.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float("nan")
    if math.isnan(value) or math.isinf(value) or not (lo < value <= hi):
        logger.warning(
            f"Config {where}.{key}={raw!r} ungültig (erlaubt {lo} < x ≤ {hi}) — "
            f"verwende {default}"
        )
        return float(default)
    return value


@dataclass(frozen=True)
class DacSettings:
    """Validierte DAC-Parameter + Umrechnung Prozent → Code → Spannung."""
    vdd: float
    opamp_gain: float
    max_output_v: float
    ramp_pct_per_s: float
    refresh_interval_s: float
    full_scale_v: float      # Ausgangsspannung bei Code 4095 (gemessen oder nominal)
    offset_v: float          # Offset des LM358-Ausgangs
    calibrated: bool         # True = gemessene Kalibrierung aktiv
    max_code: int            # Code für max_output_v — wird nie überschritten

    def pct_to_voltage(self, pct: float) -> float:
        """Zielspannung am Pumpeneingang: 0..100 % → 0..max_output_v"""
        pct = max(0.0, min(100.0, pct))
        return pct / 100.0 * self.max_output_v

    def pct_to_code(self, pct: float) -> int:
        """Prozent → DAC-Code (0..max_code). 0 % ist immer Code 0."""
        if not pct > 0:          # auch NaN → 0 (sicher)
            return 0
        target_v = self.pct_to_voltage(pct)
        code = round((target_v - self.offset_v) / self.full_scale_v * DAC_MAX)
        return max(0, min(self.max_code, code))

    def code_to_voltage(self, code: int) -> float:
        """Berechnete Spannung am Verstärkerausgang für einen DAC-Code"""
        if code <= 0:
            return 0.0
        return max(0.0, self.full_scale_v * code / DAC_MAX + self.offset_v)


def load_dac_settings(config: dict) -> DacSettings:
    """
    Abschnitt "dac:" lesen (fehlt er, gelten DAC_DEFAULTS) und mit der
    vorhandenen Zwei-Punkt-Kalibrierung (pump.dac_full_scale_v / dac_offset_v)
    kombinieren: Zielspannung aus max_output_v, Code aus der Kalibrierung.
    Gefährliche Werte werden geklemmt bzw. auf Standard gesetzt (mit Warnung).
    """
    dac = config.get("dac") or {}
    pump = config.get("pump") or {}

    vdd = _cfg_number(dac, "vdd", DAC_DEFAULTS["vdd"], 2.7 - 1e-9, 5.5, "dac")
    gain = _cfg_number(dac, "opamp_gain", DAC_DEFAULTS["opamp_gain"], 1.0 - 1e-9, 10.0, "dac")
    ramp = _cfg_number(dac, "ramp_pct_per_s", DAC_DEFAULTS["ramp_pct_per_s"], 0.0, 100.0, "dac")
    refresh = _cfg_number(dac, "refresh_interval_s", DAC_DEFAULTS["refresh_interval_s"],
                          0.05 - 1e-9, 2.0, "dac")

    raw_max = dac.get("max_output_v", DAC_DEFAULTS["max_output_v"])
    max_v = _cfg_number(dac, "max_output_v", DAC_DEFAULTS["max_output_v"], 0.0, 1e6, "dac")
    if max_v > ABS_MAX_OUTPUT_V:
        logger.warning(
            f"Config dac.max_output_v={raw_max!r} über {ABS_MAX_OUTPUT_V}V — "
            f"hart auf {ABS_MAX_OUTPUT_V}V geklemmt (Pumpeneingang 0-10V)"
        )
        max_v = ABS_MAX_OUTPUT_V

    nominal_fs = vdd * gain
    full_scale = nominal_fs
    calibrated = False
    if "dac_full_scale_v" in pump:
        try:
            measured = float(pump["dac_full_scale_v"])
        except (TypeError, ValueError):
            measured = float("nan")
        if abs(measured / nominal_fs - 1.0) <= CALIBRATION_MAX_DEVIATION:
            full_scale = measured
            calibrated = True
        else:
            logger.warning(
                f"pump.dac_full_scale_v={pump['dac_full_scale_v']!r} weicht mehr als "
                f"{CALIBRATION_MAX_DEVIATION:.0%} vom Nennwert {nominal_fs:.2f}V ab — "
                f"verwende Nennwert"
            )

    offset = 0.0
    if calibrated:
        try:
            offset = float(pump.get("dac_offset_v", 0.0))
        except (TypeError, ValueError):
            offset = float("nan")
        if math.isnan(offset) or abs(offset) > MAX_ABS_OFFSET_V:
            logger.warning(
                f"pump.dac_offset_v={pump.get('dac_offset_v')!r} unplausibel — verwende 0"
            )
            offset = 0.0

    # floor: der Code für 100 % ergibt nie mehr als max_output_v
    max_code = math.floor((max_v - offset) / full_scale * DAC_MAX)
    max_code = max(0, min(DAC_MAX, max_code))

    return DacSettings(
        vdd=vdd, opamp_gain=gain, max_output_v=max_v, ramp_pct_per_s=ramp,
        refresh_interval_s=refresh, full_scale_v=full_scale, offset_v=offset,
        calibrated=calibrated, max_code=max_code,
    )


class MockGPIO:
    """Simuliert GPIO für Entwicklung ohne Raspberry Pi"""
    BCM = "BCM"
    OUT = "OUT"
    IN  = "IN"
    HIGH = True
    LOW  = False
    PUD_UP = "PUD_UP"

    _state = {}

    @classmethod
    def setmode(cls, mode): pass

    @classmethod
    def setwarnings(cls, val): pass

    @classmethod
    def setup(cls, pin, mode, initial=None, pull_up_down=None):
        cls._state[pin] = initial if initial is not None else False

    @classmethod
    def output(cls, pin, value):
        old = cls._state.get(pin, False)
        cls._state[pin] = bool(value)
        logger.debug(f"GPIO {pin}: {old} → {bool(value)}")

    @classmethod
    def input(cls, pin):
        return cls._state.get(pin, True)

    @classmethod
    def cleanup(cls):
        logger.debug("GPIO cleanup (simuliert)")


class MockDacBus:
    """Simulierter I²C-Bus für den MCP4725 (zählt Schreibvorgänge)."""

    def __init__(self):
        self.writes = 0
        self.last_code: Optional[int] = None
        self.history = deque(maxlen=2000)   # (monotonic, code)

    def write_i2c_block_data(self, addr, cmd, data):
        code = ((data[0] & 0xFF) << 4) | ((data[1] & 0xFF) >> 4)
        self.writes += 1
        self.last_code = code
        self.history.append((time.monotonic(), code))

    def close(self):
        pass


class HardwareController:
    """
    Zentrale Hardware-Steuerung für Pool Marx.
    Verwaltet alle GPIO-Pins und den MCP4725 DAC.

    Test-Hooks (alle optional): gpio, i2c_bus, clock (monotone Uhr für die
    Rampe), start_thread=False (dann _dac_tick() von Hand aufrufen).
    """

    def __init__(self, config: dict, *, gpio=None, i2c_bus=None,
                 clock: Optional[Callable[[], float]] = None,
                 start_thread: bool = True, dimmer=None):
        self.config = config
        self.gpio_config = config["gpio"]
        self._relay_states = {}
        self._clock = clock or time.monotonic

        self.dac = load_dac_settings(config)
        pump_cfg = config.get("pump") or {}
        input_fs = pump_cfg.get("input_full_scale_v")
        pump_hint = ""
        if input_fs:
            try:
                pump_hint = (f", Pumpe zeigt bei 100 % ca. "
                             f"{self.dac.max_output_v / float(input_fs) * 100:.0f}%")
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        logger.info(
            f"DAC: 100 % = {self.dac.max_output_v:.2f}V (Code {self.dac.max_code}), "
            f"4095 → {self.dac.full_scale_v:.3f}V "
            f"({'gemessen' if self.dac.calibrated else 'nominal'} "
            f"{self.dac.vdd}V × {self.dac.opamp_gain}), Offset {self.dac.offset_v:+.3f}V, "
            f"Rampe {self.dac.ramp_pct_per_s:g}%/s, Takt {self.dac.refresh_interval_s:g}s"
            f"{pump_hint}"
        )

        # DAC-Zustand. _lock schützt Soll/Ist/Zähler (kurz gehalten, nie über
        # I²C-Zugriffe). _bus_lock serialisiert I²C-Schreibvorgänge.
        # Sperr-Reihenfolge immer: _bus_lock → _lock.
        self._lock = threading.Lock()
        self._bus_lock = threading.Lock()
        self._dac_target_pct = 0.0
        self._dac_actual_pct = 0.0
        self._dac_last_code = 0
        self._last_tick: Optional[float] = None
        self._ramp_started_at: Optional[float] = None
        self._dac_write_count = 0
        self._dac_write_errors = 0
        self._dac_consecutive_errors = 0
        self._dac_last_error_log = -math.inf
        self._dac_errors_since_log = 0
        self._setpoint_changes = {r: 0 for r in SETPOINT_REASONS}
        self._last_setpoint_change: Optional[dict] = None
        self._duration_hook: Optional[Callable[[float], None]] = None
        self._duration_sample_every = max(1, round(DAC_DURATION_SAMPLE_S / self.dac.refresh_interval_s))
        self.tick_count = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # GPIO initialisieren
        if gpio is not None:
            self.GPIO = gpio
        elif SIMULATION:
            self.GPIO = MockGPIO()
        else:
            self.GPIO = GPIO

        # Beleuchtung: Relais = Hauptschalter, Dimmer = Helligkeit
        self.dimmer_settings = load_dimmer_settings(config)
        self.dimmer = dimmer or create_dimmer(self.dimmer_settings)
        self._light_pct = 0           # 0 = aus
        self._light_last_pct = 100    # für light_on() ohne Wert

        self._setup_gpio()
        self._setup_dac(i2c_bus)
        if start_thread:
            self.start_dac_thread()
        logger.info("HardwareController initialisiert")

    def _setup_gpio(self):
        """Alle GPIO Pins konfigurieren"""
        gpio = self.GPIO
        gpio.setmode(gpio.BCM)
        gpio.setwarnings(False)

        # Alle Relais-Pins als Output (LOW = Relais aus, da active-low Board)
        relay_pins = [
            self.gpio_config["pump_relay"],
            self.gpio_config["backwash_relay"],
            self.gpio_config["fill_relay"],
            self.gpio_config["ph_relay"],
            self.gpio_config["chlor_relay"],
            self.gpio_config["light_relay"],
        ]
        for pin in relay_pins:
            gpio.setup(pin, gpio.OUT, initial=gpio.HIGH)  # HIGH = Relais aus
            self._relay_states[pin] = False

        # Pumpen-Relais ist KEIN Ein/Aus-Schalter für den Normalbetrieb —
        # das Ein/Aus/Drehzahl macht ausschließlich das 0-10V Signal.
        # Das Relais dient nur als Not-Aus/Wartungsschalter für die 230V
        # Versorgung der Pumpe und bleibt daher im Normalbetrieb dauerhaft
        # geschlossen (Netzspannung liegt an).
        self._relay_on(self.gpio_config["pump_relay"])

        # Schwimmerschalter als Input mit Pull-Up
        gpio.setup(
            self.gpio_config["level_switch"],
            gpio.IN,
            pull_up_down=gpio.PUD_UP
        )
        logger.info(f"GPIO Pins konfiguriert: Relais={relay_pins}")

    def _setup_dac(self, i2c_bus=None):
        """MCP4725 DAC für 0-10V Pumpensteuerung initialisieren — sofort 0V"""
        if i2c_bus is not None:
            self._dac_bus = i2c_bus
        elif SIMULATION:
            self._dac_bus = MockDacBus()
            logger.info("DAC Simulation aktiv")
        else:
            try:
                self._dac_bus = smbus2.SMBus(1)
                logger.info("MCP4725 DAC initialisiert")
            except Exception as e:
                logger.error(f"DAC Initialisierung fehlgeschlagen: {e}")
                self._dac_bus = None
        with self._bus_lock:
            self._write_code(0)  # Sicherheit: 0V beim Start

    # ── DAC: Schreiben ────────────────────────────────────────

    def set_write_duration_hook(self, hook: Optional[Callable[[float], None]]):
        """Telemetrie-Hook für die I²C-Schreibdauer (ms). Wird nur alle ~5 s aufgerufen."""
        self._duration_hook = hook

    def _write_code(self, code: int) -> bool:
        """
        DAC-Code schreiben (Aufrufer hält _bus_lock). Bei Fehler genau ein
        Retry, danach Fehlerzähler + rate-limitiertes Log. Wirft nie.
        """
        code = max(0, min(DAC_MAX, int(code)))
        bus = self._dac_bus
        last_exc: Optional[BaseException] = None
        if bus is not None:
            data = [(code >> 4) & 0xFF, (code << 4) & 0xFF]
            for _attempt in range(2):
                t0 = time.perf_counter()
                try:
                    bus.write_i2c_block_data(MCP4725_ADDR, MCP4725_CMD, data)
                except Exception as e:
                    last_exc = e
                    continue
                self._on_write_ok(code, (time.perf_counter() - t0) * 1000.0)
                return True
        else:
            last_exc = OSError("DAC-Bus nicht verfügbar")
        self._on_write_error(last_exc)
        return False

    def _on_write_ok(self, code: int, duration_ms: float):
        with self._lock:
            self._dac_last_code = code
            self._dac_write_count += 1
            recovered = self._dac_consecutive_errors
            self._dac_consecutive_errors = 0
            sample = self._dac_write_count % self._duration_sample_every == 0
        if recovered:
            logger.info(f"DAC schreibt wieder normal (nach {recovered} Fehlversuchen)")
        hook = self._duration_hook
        if sample and hook is not None:
            try:
                hook(duration_ms)
            except Exception:
                pass

    def _on_write_error(self, exc: Optional[BaseException]):
        now = time.monotonic()
        with self._lock:
            self._dac_write_errors += 1
            self._dac_consecutive_errors += 1
            self._dac_errors_since_log += 1
            if now - self._dac_last_error_log < DAC_ERROR_LOG_INTERVAL_S:
                return
            self._dac_last_error_log = now
            since = self._dac_errors_since_log
            self._dac_errors_since_log = 0
            total = self._dac_write_errors
            consecutive = self._dac_consecutive_errors
        log_event(
            logger, logging.ERROR, "dac.write_error",
            f"DAC Schreibfehler (nach Retry): {exc} — {since} Fehler seit letzter "
            f"Meldung, gesamt {total}, in Folge {consecutive}",
            **{"dac.error": repr(exc), "dac.errors_total": total,
               "dac.errors_since_last_log": since, "dac.errors_consecutive": consecutive},
        )

    # ── DAC: Rampe / Hintergrundthread ────────────────────────

    def start_dac_thread(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._dac_loop, name="dac-ramp", daemon=True)
        self._thread.start()

    def stop_dac_thread(self, timeout: float = 2.0) -> bool:
        """Thread beenden (join mit Timeout). True = beendet."""
        self._stop_event.set()
        t = self._thread
        if t is not None:
            t.join(timeout)
            return not t.is_alive()
        return True

    @property
    def dac_thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _dac_loop(self):
        """Fester Takt (kein Aufsummieren von Verzögerungen). Stirbt nie."""
        interval = self.dac.refresh_interval_s
        next_t = time.monotonic()
        while not self._stop_event.is_set():
            try:
                self._dac_tick()
            except Exception as e:   # pragma: no cover — reine Absicherung
                self._on_write_error(e)
            next_t += interval
            delay = next_t - time.monotonic()
            if delay <= 0:
                next_t = time.monotonic()   # Überlauf → neu synchronisieren
                delay = 0
            self._stop_event.wait(delay)

    def _dac_tick(self, now: Optional[float] = None):
        """
        Ein Durchlauf: Istwert max. ramp_pct_per_s × dt Richtung Sollwert,
        dann DAC schreiben (auch ohne Änderung = Refresh). dt ist auf 3 Takte
        begrenzt — nach einem Hänger gibt es keinen Sprung.
        """
        if now is None:
            now = self._clock()
        reached_msg = None
        with self._bus_lock:
            with self._lock:
                interval = self.dac.refresh_interval_s
                if self._last_tick is None:
                    dt = interval
                else:
                    dt = min(max(now - self._last_tick, 0.0), 3 * interval)
                self._last_tick = now
                target = self._dac_target_pct
                actual = self._dac_actual_pct
                step = self.dac.ramp_pct_per_s * dt
                if actual < target:
                    actual = min(target, actual + step)
                elif actual > target:
                    actual = max(target, actual - step)
                if actual == target and self._dac_actual_pct != target:
                    started = self._ramp_started_at
                    dur = (now - started) if started is not None else 0.0
                    reached_msg = (target, dur)
                self._dac_actual_pct = actual
                code = self.dac.pct_to_code(actual)
                self.tick_count += 1
            self._write_code(code)
        if reached_msg:
            logger.info(f"Pumpe Istwert hat Sollwert {reached_msg[0]:g}% erreicht "
                        f"(Rampe {reached_msg[1]:.1f}s)")

    # ── DAC: Sollwert ─────────────────────────────────────────

    @staticmethod
    def _validate_pct(pct) -> float:
        """Zahl prüfen: NaN/Inf/kein Zahlenwert → ValueError, sonst auf 0..100 klemmen"""
        if isinstance(pct, bool):
            raise ValueError(f"Ungültige Drehzahl: {pct!r}")
        try:
            value = float(pct)
        except (TypeError, ValueError):
            raise ValueError(f"Ungültige Drehzahl: {pct!r}") from None
        if not math.isfinite(value):
            raise ValueError(f"Ungültige Drehzahl: {pct!r}")
        return max(0.0, min(100.0, value))

    def _record_change(self, old: float, new: float, reason: str):
        """Zähler + letzte Änderung (Aufrufer hält _lock)"""
        self._setpoint_changes[reason] += 1
        self._last_setpoint_change = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "old": old, "new": new, "reason": reason,
        }
        self._ramp_started_at = self._clock()

    def _log_change(self, old: float, new: float, reason: str, actual: float, ramp: bool = True):
        how = f"Rampe {self.dac.ramp_pct_per_s:g}%/s" if ramp else "sofort, ohne Rampe"
        log_event(
            logger, logging.INFO, "pump.setpoint_changed",
            f"Pumpe Sollwert {old:g}% → {new:g}% (Grund: {reason}, Ist {actual:.1f}%, {how})",
            **{"pump.setpoint.old": old, "pump.setpoint.new": new,
               "pump.setpoint.reason": reason, "pump.actual": actual},
        )

    def _set_target(self, pct, reason: str) -> bool:
        """Nur Sollwert setzen (der DAC-Thread fährt die Rampe). True = geändert."""
        pct = self._validate_pct(pct)
        reason = reason if reason in SETPOINT_REASONS else "other"
        with self._lock:
            old = self._dac_target_pct
            if pct == old:
                return False
            self._dac_target_pct = pct
            self._record_change(old, pct, reason)
            actual = self._dac_actual_pct
        self._log_change(old, pct, reason, actual)
        return True

    def _force_zero(self, reason: str) -> bool:
        """Soll UND Ist sofort 0, 0V schreiben — ohne Rampe."""
        with self._bus_lock:
            with self._lock:
                old = self._dac_target_pct
                actual = self._dac_actual_pct
                self._dac_target_pct = 0.0
                self._dac_actual_pct = 0.0
                if old != 0:
                    self._record_change(old, 0.0, reason)
            ok = self._write_code(0)
        if old != 0 or actual != 0:
            self._log_change(old, 0.0, reason, actual, ramp=False)
        return ok

    # ── Pumpe ─────────────────────────────────────────────────

    def pump_on(self, speed_pct: float = 60, reason: str = "manual"):
        """
        Pumpe einschalten mit Drehzahl 0-100% (Sollwert, Rampe übernimmt der Thread).
        Steuert NUR das 0-10V Signal — das Relais (230V) bleibt unberührt,
        außer bei einem expliziten Not-Aus (siehe pump_mains_cutoff()).
        """
        self._set_target(speed_pct, reason)

    def pump_off(self, reason: str = "manual"):
        """Pumpe ausschalten — Sollwert 0 (sanft per Rampe). Netzspannung bleibt anliegen."""
        self._set_target(0, reason)

    def pump_set_speed(self, speed_pct: float, reason: str = "manual"):
        """Pumpen-Drehzahl ändern — reines 0-10V Signal, kein Relais-Zugriff"""
        self._set_target(speed_pct, reason)

    def pump_mains_cutoff(self, reason: str = "mains_cutoff"):
        """
        NOT-AUS / Wartung: Pumpe zusätzlich vom Netz trennen (Relais öffnen).
        Reihenfolge: erst DAC sofort 0V, dann Relais öffnen.
        Nur für Notfälle/Wartung — NICHT für den Routinebetrieb verwenden,
        da häufiges Schalten der 230V-Versorgung die Pumpenelektronik/
        Kondensatoren laut Hersteller belastet.
        """
        self._force_zero(reason)
        self._relay_off(self.gpio_config["pump_relay"])
        log_event(logger, logging.WARNING, "pump.mains_cutoff",
                  "Pumpe: Netzspannung getrennt (Not-Aus/Wartung)",
                  **{"pump.mains_cutoff.reason": reason})

    def pump_mains_restore(self):
        """Pumpe nach Not-Aus/Wartung wieder ans Netz nehmen (Relais schließen)."""
        self._relay_on(self.gpio_config["pump_relay"])
        log_event(logger, logging.INFO, "pump.mains_restore",
                  "Pumpe: Netzspannung wiederhergestellt")

    @property
    def pump_mains_connected(self) -> bool:
        """True = Relais geschlossen, Pumpe hat Netzspannung"""
        return self._relay_states.get(self.gpio_config["pump_relay"], False)

    @property
    def pump_running(self) -> bool:
        """Sollwert > 0"""
        return self._dac_target_pct > 0

    @property
    def pump_speed(self) -> int:
        """Sollwert in %"""
        return int(round(self._dac_target_pct))

    @property
    def pump_actual_speed(self) -> float:
        """Istwert in % (Rampe)"""
        return round(self._dac_actual_pct, 2)

    def get_dac_snapshot(self) -> dict:
        """Konsistente Momentaufnahme (für Telemetrie/Diagnose), thread-sicher"""
        with self._lock:
            code = self._dac_last_code
            return {
                "target_pct": self._dac_target_pct,
                "actual_pct": self._dac_actual_pct,
                "code": code,
                "output_v": self.dac.code_to_voltage(code),
                "write_count": self._dac_write_count,
                "write_errors": self._dac_write_errors,
                "consecutive_errors": self._dac_consecutive_errors,
                "setpoint_changes": dict(self._setpoint_changes),
                "last_change": dict(self._last_setpoint_change) if self._last_setpoint_change else None,
                "thread_alive": self.dac_thread_alive,
            }

    # ── BESGO Rückspülung ──────────────────────────────────────

    def backwash_valve_open(self):
        """BESGO Magnetventil öffnen → Wasserdruck betätigt Stangenventil"""
        self._relay_on(self.gpio_config["backwash_relay"])
        logger.info("BESGO Magnetventil: OFFEN (Rückspülung)")

    def backwash_valve_close(self):
        self._relay_off(self.gpio_config["backwash_relay"])
        logger.info("BESGO Magnetventil: GESCHLOSSEN (Filterbetrieb)")

    # ── Frischwasser Nachspeisung ──────────────────────────────

    def fill_valve_open(self):
        self._relay_on(self.gpio_config["fill_relay"])
        logger.info("Nachspeisung Magnetventil: OFFEN")

    def fill_valve_close(self):
        self._relay_off(self.gpio_config["fill_relay"])
        logger.info("Nachspeisung Magnetventil: GESCHLOSSEN")

    # ── Dosierpumpen ──────────────────────────────────────────

    def ph_pump_on(self):
        self._relay_on(self.gpio_config["ph_relay"])
        logger.info("pH-Minus Dosierpumpe: EIN")

    def ph_pump_off(self):
        self._relay_off(self.gpio_config["ph_relay"])
        logger.info("pH-Minus Dosierpumpe: AUS")

    def chlor_pump_on(self):
        self._relay_on(self.gpio_config["chlor_relay"])
        logger.info("Chlor Dosierpumpe: EIN")

    def chlor_pump_off(self):
        self._relay_off(self.gpio_config["chlor_relay"])
        logger.info("Chlor Dosierpumpe: AUS")

    # ── Beleuchtung ───────────────────────────────────────────

    def set_light(self, pct, reason: str = "manual", entry: Optional[str] = None) -> int:
        """
        Helligkeit 0–100 % setzen (0 = aus). Werte unter dimmer.min_pct werden
        auf min_pct angehoben. Reihenfolge: einschalten = erst Dimmer, dann
        Relais; ausschalten = erst Relais, dann Dimmer (kein Aufblitzen).
        Gibt die tatsächlich gesetzte Helligkeit zurück.
        """
        try:
            pct = int(round(float(pct)))
        except (TypeError, ValueError):
            raise ValueError(f"Helligkeit ungültig: {pct!r}")
        pct = max(0, min(100, pct))
        if 0 < pct < self.dimmer_settings.min_pct:
            pct = self.dimmer_settings.min_pct
        old_pct, was_on = self._light_pct, self.light_is_on
        pin = self.gpio_config["light_relay"]
        if pct > 0:
            self.dimmer.set_level(pct)
            self._relay_on(pin)
            self._light_last_pct = pct
        else:
            self._relay_off(pin)
            self.dimmer.set_level(0)
        self._light_pct = pct
        self._log_light(was_on, old_pct, pct, reason, entry)
        return pct

    def light_on(self, pct=None, reason: str = "manual"):
        """Licht an — mit pct oder der zuletzt genutzten Helligkeit."""
        return self.set_light(pct if pct else self._light_last_pct, reason=reason)

    def light_off(self, reason: str = "manual"):
        return self.set_light(0, reason=reason)

    @property
    def light_brightness(self) -> int:
        """Aktuelle Helligkeit in % (0 = aus)."""
        return self._light_pct if self.light_is_on else 0

    def _log_light(self, was_on: bool, old_pct: int, new_pct: int, reason: str,
                   entry: Optional[str]):
        """Ereignis nur bei echter Änderung (light_off() beim Beenden/Abschalten ist oft ein No-op)."""
        now_on = new_pct > 0
        old_pct = old_pct if was_on else 0
        text = f"EVA Beleuchtung: {f'EIN {new_pct}%' if now_on else 'AUS'}"
        if was_on == now_on and old_pct == new_pct:
            logger.debug(f"{text} (unverändert)")
            return
        where = f", Zeitplan '{entry}'" if entry else ""
        log_event(logger, logging.INFO, "light.changed",
                  f"{text} (vorher {old_pct}%, Grund: {reason}{where}, "
                  f"Dimmer: {self.dimmer.kind})",
                  **{"light.on": now_on, "light.brightness_pct": new_pct,
                     "light.brightness_old": old_pct, "light.reason": reason,
                     "light.schedule_entry": entry, "light.dimmer": self.dimmer.kind})

    @property
    def light_is_on(self) -> bool:
        return self._relay_states.get(self.gpio_config["light_relay"], False)

    # ── Relais (intern) ───────────────────────────────────────

    def _relay_on(self, pin: int):
        """Relais einschalten (active-low: LOW = ein)"""
        self.GPIO.output(pin, self.GPIO.LOW)
        self._relay_states[pin] = True
        logger.debug(f"Relais Pin {pin}: EIN")

    def _relay_off(self, pin: int):
        """Relais ausschalten (active-low: HIGH = aus)"""
        self.GPIO.output(pin, self.GPIO.HIGH)
        self._relay_states[pin] = False
        logger.debug(f"Relais Pin {pin}: AUS")

    # ── Sensoren ──────────────────────────────────────────────

    def read_level_switch(self) -> bool:
        """
        Schwimmerschalter lesen.
        True = Pegel OK, False = Pegel zu niedrig → Nachspeisung nötig
        """
        raw = self.GPIO.input(self.gpio_config["level_switch"])
        # Active-Low: LOW-Signal = Schwimmer unten = Pegel zu niedrig
        level_ok = bool(raw)
        return level_ok

    # ── Status ────────────────────────────────────────────────

    def get_all_states(self) -> dict:
        """Alle Hardware-Zustände als Dictionary"""
        snap = self.get_dac_snapshot()
        return {
            "pump": {
                "running": snap["target_pct"] > 0,
                "speed_pct": int(round(snap["target_pct"])),
                "actual_speed_pct": round(snap["actual_pct"], 1),
            },
            "dac": {
                "output_v": round(snap["output_v"], 2),
                "write_errors": snap["write_errors"],
            },
            "valves": {
                "backwash": self._relay_states.get(
                    self.gpio_config["backwash_relay"], False),
                "fill": self._relay_states.get(
                    self.gpio_config["fill_relay"], False),
            },
            "dosing": {
                "ph_pump": self._relay_states.get(
                    self.gpio_config["ph_relay"], False),
                "chlor_pump": self._relay_states.get(
                    self.gpio_config["chlor_relay"], False),
            },
            "light": self._relay_states.get(
                self.gpio_config["light_relay"], False),
            "light_brightness_pct": self.light_brightness,
            "light_dimmer": self.dimmer.kind,
            "level_ok": self.read_level_switch(),
            "simulation": SIMULATION,
        }

    def emergency_stop(self):
        """Notaus — alle Verbraucher sofort abschalten, Pumpe zusätzlich vom Netz getrennt"""
        log_event(logger, logging.WARNING, "emergency_stop",
                  "NOTAUS ausgelöst — alle Verbraucher werden abgeschaltet")
        self.pump_mains_cutoff(reason="emergency")
        self.backwash_valve_close()
        self.fill_valve_close()
        self.ph_pump_off()
        self.chlor_pump_off()
        self.light_off(reason="emergency")

    def cleanup(self):
        """Beim Beenden: 0V sofort, DAC-Thread stoppen, 0V final, GPIO freigeben"""
        self.emergency_stop()
        if not self.stop_dac_thread(timeout=2.0):
            logger.error("DAC-Thread hat sich nicht rechtzeitig beendet")
        with self._bus_lock:
            self._write_code(0)
        if not SIMULATION and self.GPIO is not MockGPIO:
            try:
                self.GPIO.cleanup()
            except Exception as e:
                logger.error(f"GPIO cleanup fehlgeschlagen: {e}")
        logger.info("GPIO freigegeben")
