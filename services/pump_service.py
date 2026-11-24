"""
Pool Marx — Pumpensteuerung Service
Verwaltet Zeitplan, Frostschutz, Rückspülung, manuelle Steuerung und Not-Aus
"""

import asyncio
import glob
import logging
import threading
import time
from datetime import datetime, time as dtime
from enum import Enum
from typing import NamedTuple, Optional, Callable
from dataclasses import dataclass, asdict

from .events import log_event, sanitize_text
from .hardware import HardwareController, SIMULATION

logger = logging.getLogger(__name__)


class PumpMode(Enum):
    OFF       = "off"
    MANUAL    = "manual"
    SCHEDULE  = "schedule"
    FROST     = "frost"
    BACKWASH  = "backwash"
    EMERGENCY = "emergency"


PUMP_MODES = tuple(m.value for m in PumpMode)
MIN_SPEED_MODES = ("off", "always", "winter")
DEFAULT_WINTER_MONTHS = (11, 12, 1, 2, 3)
FROST_HYSTERESIS_DEFAULT_C = 0.5


class EmergencyActive(Exception):
    """Befehl abgelehnt — Not-Aus ist aktiv und noch nicht quittiert"""


class BackwashBusy(Exception):
    """Befehl abgelehnt — Rückspülung läuft"""


class ScheduledSpeed(NamedTuple):
    name: str            # Name des Zeitplaneintrags (bleibt auch bei Mindestdrehzahl)
    speed_pct: int       # tatsächlich zu fahrende Drehzahl
    min_speed: bool      # True = geplante 0 % durch Mindestdrehzahl ersetzt


@dataclass
class PumpState:
    running: bool        = False
    speed_pct: int       = 0
    mode: str            = PumpMode.OFF.value
    frost_active: bool   = False
    sensor_error: bool   = False   # Temperatursensor fehlt/liefert keine Werte
    min_speed_active: bool = False # Zeitplan-0% durch Mindestdrehzahl ersetzt
    override_until: Optional[str] = None
    last_changed: str    = ""

    def to_dict(self) -> dict:
        return asdict(self)


class PumpService:
    """
    Steuert die Speck BADU Alpha ECO Soft.
    Funktionen:
    - Zeitgesteuerte Filterprogramme (mit optionaler Mindestdrehzahl statt 0 %)
    - Frostschutz (automatisch bei <3°C, Vorrang vor manuellen Befehlen)
    - Rückspül-Sequenz (gegen Doppelstart gesperrt)
    - Manuelle Übersteuerung mit Zeitlimit
    - Verriegelter Not-Aus: blockiert alle Automatiken bis zur Quittierung
    - MQTT Publish bei Statusänderung

    Test-Hooks: now (Wanduhr), monotonic (Laufzeiten), sleep (async).
    """

    def __init__(self,
                 hardware: HardwareController,
                 config: dict,
                 on_state_change: Optional[Callable] = None,
                 *,
                 now: Optional[Callable[[], datetime]] = None,
                 monotonic: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable] = None):
        self.hw = hardware
        self.config = config
        self.pump_config = config["pump"]
        self.frost_config = config["frost"]
        self.backwash_config = config["backwash"]
        self.on_state_change = on_state_change
        self._now = now or datetime.now
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep

        self.state = PumpState()
        self._state_lock = threading.Lock()
        self._override_task: Optional[asyncio.Task] = None
        self._backwash_task: Optional[asyncio.Task] = None
        self._background: set[asyncio.Task] = set()
        self._running = False
        self._emergency = False
        self._frost_activated_at: Optional[float] = None   # monotonic
        self._last_schedule_name: Optional[str] = None
        # Zähler für Telemetrie (Observable Counter)
        self.backwash_runs = 0
        self.fault_notes_count = 0
        # Optionales Telemetrie-Objekt (services/telemetry.py), nur für Histogramme
        self.telemetry = None
        # Lebenszeichen der Zeitplan-Schleife (für den systemd-Watchdog)
        self.last_heartbeat = time.monotonic()

        # Drehzahl-Map aus Config
        self.speed_map = self.pump_config["speeds"]
        self._min_speed_cfg = self._load_min_speed_config()

    def _speed_to_pct(self, speed_name: str) -> int:
        """Geschwindigkeitsname → Prozent"""
        return self.speed_map.get(speed_name, 0)

    @property
    def emergency_active(self) -> bool:
        return self._emergency

    @property
    def backwash_running(self) -> bool:
        return self._backwash_task is not None and not self._backwash_task.done()

    def _check_commands_allowed(self):
        if self._emergency:
            raise EmergencyActive("Not-Aus aktiv — erst quittieren")
        if self.backwash_running:
            raise BackwashBusy("Rückspülung läuft")

    async def start(self):
        """
        Startet alle Hintergrundaufgaben. Stirbt eine davon, wird die Exception
        weitergereicht — main.py beendet dann den Prozess und systemd startet neu.
        """
        self._running = True
        logger.info("PumpService gestartet")
        await asyncio.gather(
            self._schedule_loop(),
            self._frost_loop(),
            self._backwash_scheduler(),
        )

    async def stop(self):
        self._running = False
        self.hw.pump_off(reason="shutdown")
        logger.info("PumpService gestoppt")

    # ── Mindestdrehzahl ───────────────────────────────────────

    def _load_min_speed_config(self) -> dict:
        """pump.min_speed lesen und prüfen. Fehlt der Abschnitt → aus."""
        raw = self.pump_config.get("min_speed") or {}
        mode = str(raw.get("mode", "off")).lower()
        if mode not in MIN_SPEED_MODES:
            logger.warning(f"pump.min_speed.mode={raw.get('mode')!r} ungültig — Mindestdrehzahl aus")
            mode = "off"
        speed_name = raw.get("speed", "min")
        pct = self.speed_map.get(speed_name) if isinstance(speed_name, str) else None
        if mode != "off" and (not isinstance(pct, (int, float)) or not 0 < pct <= 100):
            logger.warning(
                f"pump.min_speed.speed={speed_name!r} ist kein gültiger Eintrag in "
                f"pump.speeds — Mindestdrehzahl aus"
            )
            mode = "off"
        months_raw = raw.get("winter_months", list(DEFAULT_WINTER_MONTHS))
        months = set()
        for m in (months_raw if isinstance(months_raw, (list, tuple)) else []):
            if isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 12:
                months.add(m)
            else:
                logger.warning(f"pump.min_speed.winter_months: {m!r} ignoriert")
        if mode != "off":
            logger.info(
                f"Mindestdrehzahl: Modus '{mode}', {speed_name} = {pct}%"
                + (f", Monate {sorted(months)}" if mode == "winter" else "")
            )
        return {"mode": mode, "speed": speed_name, "pct": int(pct or 0), "months": months}

    def _min_speed_pct(self, now: datetime) -> Optional[int]:
        """Mindestdrehzahl in %, falls jetzt aktiv — sonst None."""
        cfg = self._min_speed_cfg
        if cfg["mode"] == "always":
            return cfg["pct"]
        if cfg["mode"] == "winter":
            if now.month in cfg["months"]:
                return cfg["pct"]
            # TODO(DS18B20): Sobald der Außentemperatursensor verbaut ist, "winter"
            # zusätzlich über die Außentemperatur aktivieren (z. B. < 5 °C,
            # Schwelle als pump.min_speed.outdoor_below_c). Nur echte Messwerte
            # verwenden — NIE den Simulationswert 15.0 aus _read_temperature()
            # und nie None. Bis dahin entscheidet allein der Monat.
        return None

    # ── Zeitplan ──────────────────────────────────────────────

    async def _schedule_loop(self):
        """Prüft alle 60s ob Zeitplan aktiv ist"""
        while self._running:
            self.last_heartbeat = time.monotonic()
            self._apply_schedule()
            await self._sleep(60)

    def _apply_schedule(self):
        """Zeitplan-Drehzahl anwenden, sofern kein anderer Modus Vorrang hat"""
        scheduled = self._get_scheduled_speed()
        self._track_schedule_transition(scheduled)
        if self._emergency or self.state.mode in [
            PumpMode.MANUAL.value,
            PumpMode.FROST.value,
            PumpMode.BACKWASH.value,
            PumpMode.EMERGENCY.value,
        ]:
            return
        if scheduled is None:
            return
        name, speed_pct, via_min = scheduled
        if speed_pct > 0:
            if not self.hw.pump_running or self.hw.pump_speed != speed_pct:
                self.hw.pump_on(speed_pct, reason="min_speed" if via_min else "schedule")
                if via_min:
                    logger.info(f"Zeitplan '{name}': 0% → Mindestdrehzahl {speed_pct}%")
                else:
                    logger.info(f"Zeitplan '{name}': Pumpe {speed_pct}%")
            target = dict(running=True, speed_pct=speed_pct, mode=PumpMode.SCHEDULE.value,
                          min_speed_active=via_min)
        else:
            if self.hw.pump_running:
                self.hw.pump_off(reason="schedule")
                logger.info(f"Zeitplan '{name}': Pumpe AUS")
            target = dict(running=False, speed_pct=0, mode=PumpMode.OFF.value,
                          min_speed_active=False)
        # Nur bei Änderung melden (sonst MQTT/WebSocket-Push jede Minute)
        if any(getattr(self.state, k) != v for k, v in target.items()):
            self._update_state(**target)

    def _track_schedule_transition(self, scheduled: Optional[ScheduledSpeed]):
        """Jeden Wechsel des Zeitplaneintrags loggen — auch wenn ein anderer Modus Vorrang hat"""
        name = scheduled.name if scheduled else None
        if name == self._last_schedule_name:
            return
        old = self._last_schedule_name
        self._last_schedule_name = name
        log_event(
            logger, logging.INFO, "schedule.transition",
            f"Zeitplanwechsel: '{old}' → '{name}' "
            f"({scheduled.speed_pct if scheduled else '-'}%"
            f"{', Mindestdrehzahl' if scheduled and scheduled.min_speed else ''}, "
            f"Modus {self.state.mode})",
            **{"schedule.from": old, "schedule.to": name,
               "schedule.speed_pct": scheduled.speed_pct if scheduled else None,
               "schedule.min_speed": scheduled.min_speed if scheduled else None,
               "pump.mode": self.state.mode},
        )

    def _get_scheduled_speed(self, now: Optional[datetime] = None) -> Optional[ScheduledSpeed]:
        """
        Gibt (Name, speed_pct, min_speed) für die aktuelle Uhrzeit zurück.
        Ergibt der Zeitplan 0 % und ist die Mindestdrehzahl aktiv, wird
        stattdessen deren Drehzahl geliefert (Name des Eintrags bleibt).
        """
        now = now or self._now()
        now_t = now.time().replace(tzinfo=None)
        schedule = self.pump_config.get("schedule", [])

        for entry in schedule:
            start = dtime.fromisoformat(entry["start"])
            end   = dtime.fromisoformat(entry["end"])

            # Behandelt Mitternacht-Überschneidung (z.B. 21:00-07:00)
            if start <= end:
                in_range = start <= now_t < end
            else:
                in_range = now_t >= start or now_t < end

            if in_range:
                speed_name = entry.get("speed", "off")
                pct = self._speed_to_pct(speed_name)
                if pct <= 0:
                    min_pct = self._min_speed_pct(now)
                    if min_pct:
                        return ScheduledSpeed(entry["name"], min_pct, True)
                return ScheduledSpeed(entry["name"], pct, False)
        return None

    # ── Frostschutz ───────────────────────────────────────────

    def _frost_pct(self) -> int:
        return self._speed_to_pct(self.frost_config["pump_speed"])

    async def _frost_loop(self):
        """Überwacht Temperatur und aktiviert Frostschutz"""
        interval = self.frost_config.get("check_interval_sec", 300)

        if not self.frost_config.get("enabled", True):
            logger.warning("Software-Frostschutz ist in der Config deaktiviert")
            return

        while self._running:
            temp = await self._read_temperature()
            await self._frost_step(temp)
            await self._sleep(interval)

    async def _frost_step(self, temp: Optional[float]):
        """
        Eine Frostschutz-Prüfung. Aktivierung bei temp < Schwelle; Deaktivierung
        erst nach Mindestlaufzeit UND temp ≥ Schwelle + Hysterese (kein Flattern).
        """
        run_minutes = self.frost_config.get("run_minutes", 0)
        threshold = self.frost_config["temp_threshold_c"]
        hysteresis = float(self.frost_config.get("hysteresis_c", FROST_HYSTERESIS_DEFAULT_C))

        if temp is None:
            # Fail-safe: ohne Messwert einen aktiven Frostschutz nie beenden
            if not self.state.sensor_error:
                logger.error(
                    "Temperatursensor liefert keinen Wert — "
                    "Software-Frostschutz kann nicht auslösen"
                )
                self._update_state(sensor_error=True)
            return

        if self.state.sensor_error:
            logger.info(f"Temperatursensor wieder verfügbar: {temp:.1f}°C")
            self._update_state(sensor_error=False)

        if temp < threshold:
            if not self.state.frost_active and not self._emergency:
                logger.warning(
                    f"FROSTSCHUTZ aktiv: {temp:.1f}°C < {threshold}°C"
                )
                await self._activate_frost_protection(temp)
            # bereits aktiv → einfach weiterlaufen lassen
        elif self.state.frost_active:
            elapsed_min = self._frost_elapsed_minutes()
            if elapsed_min >= run_minutes and temp >= threshold + hysteresis:
                logger.info(
                    f"Frostschutz beendet: {temp:.1f}°C ≥ {threshold + hysteresis}°C "
                    f"(Mindestlaufzeit {run_minutes} Min erreicht)"
                )
                await self._deactivate_frost_protection(temp)
            else:
                logger.debug(
                    f"Frostschutz bleibt aktiv — Laufzeit {elapsed_min:.1f}/{run_minutes} Min, "
                    f"{temp:.1f}°C (Ende ab {threshold + hysteresis}°C)"
                )

    def _frost_elapsed_minutes(self) -> float:
        """Minuten seit Aktivierung des Frostschutzes (monoton, unabhängig von Zeitumstellung)"""
        if self._frost_activated_at is None:
            return float("inf")
        return (self._monotonic() - self._frost_activated_at) / 60.0

    async def _activate_frost_protection(self, temp: Optional[float] = None):
        """Frostschutz einschalten — überschreibt Zeitplan und manuelle Modi"""
        if self._override_task:
            self._override_task.cancel()
        self._frost_activated_at = self._monotonic()
        log_event(logger, logging.WARNING, "frost.activated",
                  f"Frostschutz aktiviert ({temp}°C)",
                  **{"frost.temp_c": temp, "frost.threshold_c": self.frost_config["temp_threshold_c"],
                     "pump.mode_before": self.state.mode})
        if self.backwash_running:
            # Rückspülung läuft mit Vollgas weiter; danach greift der Frostschutz
            self._update_state(frost_active=True)
            return
        speed_pct = self._frost_pct()
        self.hw.pump_on(speed_pct, reason="frost")
        self._update_state(
            running=True,
            speed_pct=speed_pct,
            mode=PumpMode.FROST.value,
            frost_active=True,
            min_speed_active=False,
        )

    async def _deactivate_frost_protection(self, temp: Optional[float] = None):
        """Frostschutz beenden — zurück zu Zeitplan"""
        self._frost_activated_at = None
        self._update_state(frost_active=False)
        log_event(logger, logging.INFO, "frost.deactivated",
                  f"Frostschutz deaktiviert ({temp}°C)", **{"frost.temp_c": temp})
        # Modus zurücksetzen damit Zeitplan wieder übernimmt
        if self.state.mode == PumpMode.FROST.value:
            self._update_state(mode=PumpMode.SCHEDULE.value)
            self._apply_schedule()

    async def _read_temperature(self) -> Optional[float]:
        """DS18B20 Temperatur lesen. None = kein gültiger Messwert."""
        try:
            # 1-Wire Sensor Pfad auf Raspberry Pi
            device_file = glob.glob("/sys/bus/w1/devices/28-*/w1_slave")
            if not device_file:
                # Testwert nur im Simulationsmodus — auf dem Pi ist ein
                # fehlender Sensor ein Fehler, kein "warmer" Messwert
                return 15.0 if SIMULATION else None

            with open(device_file[0], "r") as f:
                lines = f.readlines()
                if lines[0].strip().endswith("YES"):
                    temp_str = lines[1].split("t=")[1]
                    return float(temp_str) / 1000.0
        except Exception as e:
            logger.error(f"Temperatursensor Fehler: {e}")
        return None

    # ── Rückspülung ───────────────────────────────────────────

    def _backwash_due(self, now: datetime) -> bool:
        schedule = self.backwash_config.get("schedule", {})
        return (now.strftime("%A").lower() == schedule.get("day", "monday") and
                now.strftime("%H:%M") == schedule.get("time", "10:00"))

    async def _backwash_scheduler(self):
        """Automatischer Rückspül-Zeitplan"""
        if not self.backwash_config.get("auto_enabled", True):
            logger.info("Automatische Rückspülung ist in der Config deaktiviert")
            return

        while self._running:
            if self._backwash_due(self._now()):
                try:
                    self.start_backwash()
                    logger.info("Automatische Rückspülung gestartet")
                except (EmergencyActive, BackwashBusy, ValueError) as e:
                    logger.warning(f"Automatische Rückspülung übersprungen: {e}")
                await self._sleep(60)  # Verhindert Doppelauslösung

            await self._sleep(30)

    def start_backwash(self) -> asyncio.Task:
        """
        Rückspülung als Hintergrund-Task starten.
        Wirft EmergencyActive / BackwashBusy / ValueError, wenn nicht erlaubt.
        """
        self._check_commands_allowed()
        if not self.hw.read_level_switch():
            raise ValueError("Wasserstand zu niedrig — Rückspülung gesperrt")

        if self._override_task:
            self._override_task.cancel()
        prev_mode = self.state.mode
        prev_speed = self.state.speed_pct
        backwash_speed = self._speed_to_pct("backwash")
        self._update_state(
            running=True,
            speed_pct=backwash_speed,
            mode=PumpMode.BACKWASH.value
        )
        self._backwash_task = asyncio.create_task(
            self._run_backwash(prev_mode, prev_speed, backwash_speed)
        )
        return self._backwash_task

    def _ramp_timeout_s(self, target_pct: float) -> float:
        """Timeout für das Hochfahren: Rampendauer ab aktuellem Istwert + Reserve"""
        cfg = self.backwash_config.get("ramp_timeout_sec")
        if cfg:
            return float(cfg)
        ramp = self.hw.dac.ramp_pct_per_s
        return abs(target_pct - self.hw.pump_actual_speed) / ramp + 10.0

    async def _wait_for_actual_speed(self, target_pct: float, timeout_s: float,
                                     poll_s: float = 0.2) -> bool:
        """Warten, bis der Istwert (Rampe) den Sollwert erreicht hat. False = Timeout."""
        deadline = self._monotonic() + timeout_s
        while True:
            if self.hw.pump_actual_speed >= target_pct - 0.5:
                return True
            if self._monotonic() >= deadline:
                return False
            await self._sleep(poll_s)

    async def _run_backwash(self, prev_mode: str, prev_speed: int, backwash_speed: int):
        """
        Vollständige Rückspül-Sequenz:
        1. Pumpe auf Vollgas, warten bis der Istwert (Rampe) erreicht ist
        2. BESGO Magnetventil öffnen → Rückspülung
        3. Warten (3 Min)
        4. Magnetventil schließen → Nachspülen
        5. Warten (30 Sek)
        6. Pumpe zurück auf vorherigen Betrieb
        Bei Not-Aus wird der Task abgebrochen.
        """
        started = self._monotonic()
        self.backwash_runs += 1
        result = "error"
        log_event(logger, logging.INFO, "backwash.started", "Rückspül-Sequenz startet",
                  **{"backwash.speed_pct": backwash_speed, "pump.mode_before": prev_mode,
                     "pump.speed_before": prev_speed})
        try:
            # Schritt 1: Pumpe auf Vollgas — Rampe abwarten (statt fester 3 s)
            self.hw.pump_on(backwash_speed, reason="backwash")
            timeout = self._ramp_timeout_s(backwash_speed)
            if not await self._wait_for_actual_speed(backwash_speed, timeout):
                raise RuntimeError(
                    f"Pumpe erreicht {backwash_speed}% nicht innerhalb {timeout:.0f}s "
                    f"(Ist {self.hw.pump_actual_speed}%) — Rückspülung abgebrochen"
                )

            # Schritt 2: BESGO auf Rückspülung
            self.hw.backwash_valve_open()
            logger.info("Rückspülung läuft...")
            await self._sleep(self.backwash_config["backwash_duration_sec"])

            # Schritt 3: BESGO auf Nachspülen (Ventil schließen = Filter)
            self.hw.backwash_valve_close()
            logger.info("Nachspülen...")
            await self._sleep(self.backwash_config["rinse_duration_sec"])

            logger.info("Rückspül-Sequenz abgeschlossen")
            result = "ok"

        except asyncio.CancelledError:
            result = "cancelled"
            logger.warning("Rückspül-Sequenz abgebrochen")
            raise
        except Exception as e:
            logger.error(f"Fehler bei Rückspülung: {e}")
        finally:
            self.hw.backwash_valve_close()
            if not self._emergency:
                self._restore_after_backwash(prev_mode, prev_speed)
            duration = self._monotonic() - started
            log_event(logger, logging.INFO, "backwash.finished",
                      f"Rückspülung beendet: {result} nach {duration:.0f}s",
                      **{"backwash.result": result, "backwash.duration_s": duration,
                         "emergency": self._emergency})
            if self.telemetry is not None:
                try:
                    self.telemetry.record_backwash_duration_s(duration, result)
                except Exception:
                    pass

    async def cancel_backwash(self, resume_schedule: bool = False):
        """
        Laufende Rückspülung abbrechen: Ventil zu, Pumpe zurück auf den
        vorherigen Betrieb — oder direkt zurück zum Zeitplan.
        """
        if self._emergency:
            raise EmergencyActive("Not-Aus aktiv — erst quittieren")
        task = self._backwash_task
        if task is not None and not task.done():
            logger.info("Rückspülung wird abgebrochen")
            task.cancel()
            await asyncio.wait({task})   # finally-Block (Ventil zu, Restore) abwarten
        if resume_schedule:
            self.resume_schedule()

    @staticmethod
    def _reason_for_mode(mode: str) -> str:
        return {"frost": "frost", "manual": "manual"}.get(mode, "schedule")

    def _restore_after_backwash(self, prev_mode: str, prev_speed: int):
        """Pumpe und Zustand nach der Rückspülung zurücksetzen"""
        if self.state.frost_active:
            speed = max(prev_speed, self._frost_pct())
            mode = PumpMode.FROST.value if prev_mode != PumpMode.MANUAL.value else prev_mode
        else:
            speed = prev_speed
            mode = prev_mode
        if mode in (PumpMode.BACKWASH.value, PumpMode.EMERGENCY.value):
            mode = PumpMode.SCHEDULE.value

        reason = self._reason_for_mode(mode)
        if speed > 0:
            self.hw.pump_on(speed, reason=reason)
        else:
            self.hw.pump_off(reason=reason)
        self._update_state(running=speed > 0, speed_pct=speed, mode=mode)
        self._apply_schedule()

    # ── Manuelle Steuerung ────────────────────────────────────

    async def manual_on(self, speed_pct: int = 60, duration_min: Optional[int] = None):
        """Pumpe manuell einschalten, optional mit Zeitlimit"""
        self._check_commands_allowed()
        speed_pct = max(0, min(100, int(speed_pct)))

        if self.state.frost_active and speed_pct < self._frost_pct():
            logger.warning(
                f"Frostschutz aktiv — {speed_pct}% abgelehnt, "
                f"Pumpe bleibt auf {self._frost_pct()}%"
            )
            self.hw.pump_on(self._frost_pct(), reason="frost")
            self._update_state(
                running=True,
                speed_pct=self._frost_pct(),
                mode=PumpMode.FROST.value
            )
            return

        if self._override_task:
            self._override_task.cancel()

        if speed_pct > 0:
            self.hw.pump_on(speed_pct, reason="manual")
        else:
            self.hw.pump_off(reason="manual")
        self._update_state(
            running=speed_pct > 0,
            speed_pct=speed_pct,
            mode=PumpMode.MANUAL.value,
            min_speed_active=False,
        )

        if duration_min and duration_min > 0:
            self._override_task = asyncio.create_task(
                self._auto_return_to_schedule(duration_min * 60)
            )
            logger.info(
                f"Pumpe manuell: {speed_pct}% für {duration_min} Min"
            )
        else:
            logger.info(f"Pumpe manuell: {speed_pct}%")

    async def manual_off(self):
        """
        Pumpe manuell ausschalten. Bleibt aus (Modus manuell), bis der
        Zeitplan wieder aktiviert wird — sonst würde der Zeitplan sie
        nach spätestens 60s wieder einschalten. Die Mindestdrehzahl
        überschreibt ein manuelles Aus NICHT (nur der Frostschutz).
        """
        await self.manual_on(0)

    def resume_schedule(self):
        """Zurück zum automatischen Zeitplan (sofort angewendet)"""
        self._check_commands_allowed()
        if self._override_task:
            self._override_task.cancel()
        if self.state.frost_active:
            self._update_state(mode=PumpMode.FROST.value)
            return
        self._update_state(mode=PumpMode.SCHEDULE.value)
        self._apply_schedule()
        logger.info("Zurück zum Zeitplan")

    async def _auto_return_to_schedule(self, seconds: int):
        """Nach Ablauf der Übersteuerungszeit zurück zum Zeitplan"""
        await self._sleep(seconds)
        if self.state.mode == PumpMode.MANUAL.value and not self._emergency:
            self._override_task = None
            self.resume_schedule()
            logger.info("Manuelle Übersteuerung beendet — zurück zum Zeitplan")

    # ── Not-Aus ───────────────────────────────────────────────

    def emergency_stop(self):
        """
        Verriegelter Not-Aus (Software): alle Verbraucher aus, Pumpe vom Netz,
        Zeitplan/Frostschutz/Rückspülung/manuelle Befehle gesperrt, bis
        acknowledge_emergency() aufgerufen wird.
        Kein Ersatz für einen Reparaturschalter bei Wartungsarbeiten!
        """
        self._emergency = True
        if self._override_task:
            self._override_task.cancel()
        if self.backwash_running:
            self._backwash_task.cancel()
        self.hw.emergency_stop()
        self._update_state(
            running=False,
            speed_pct=0,
            mode=PumpMode.EMERGENCY.value,
            min_speed_active=False,
        )
        logger.warning("NOT-AUS verriegelt — Quittierung erforderlich")

    def acknowledge_emergency(self):
        """
        Not-Aus quittieren: Netzspannung der Pumpe wieder zuschalten.
        Die Pumpe bleibt AUS (Modus manuell) — Zeitplan muss bewusst
        wieder aktiviert werden.
        """
        # DAC steht seit dem Not-Aus auf 0V → Pumpe läuft nach dem Zuschalten nicht an
        self.hw.pump_mains_restore()
        if not self._emergency:
            return
        self._emergency = False
        self._update_state(
            running=False,
            speed_pct=0,
            mode=PumpMode.MANUAL.value
        )
        logger.warning("Not-Aus quittiert — Pumpe bleibt aus bis zur nächsten Aktion")

    # ── Diagnose ──────────────────────────────────────────────

    def pump_status_summary(self) -> dict:
        """Pumpenstatus für Diagnose (Fehlernotiz, Telemetrie)"""
        snap = self.hw.get_dac_snapshot()
        last = snap["last_change"] or {}
        with self._state_lock:
            mode = self.state.mode
            frost = self.state.frost_active
            min_speed = self.state.min_speed_active
        return {
            "mode": mode,
            "setpoint_pct": round(snap["target_pct"], 1),
            "actual_pct": round(snap["actual_pct"], 1),
            "dac_output_v": round(snap["output_v"], 2),
            "dac_write_errors": snap["write_errors"],
            "mains_connected": self.hw.pump_mains_connected,
            "frost_active": frost,
            "min_speed_active": min_speed,
            "schedule_entry": self._last_schedule_name,
            "last_change_time": last.get("time"),
            "last_change_old": last.get("old"),
            "last_change_new": last.get("new"),
            "last_change_reason": last.get("reason"),
        }

    def record_fault_note(self, text: Optional[str] = None) -> dict:
        """
        Pumpenfehler (z. B. E-04) mit aktuellem Status als WARNING loggen —
        zur späteren Korrelation mit Zeitplanwechseln/Sollwertänderungen.
        """
        note = sanitize_text(text)
        status = self.pump_status_summary()
        self.fault_notes_count += 1
        last = (f"{status['last_change_time']} {status['last_change_old']:g}%→"
                f"{status['last_change_new']:g}% ({status['last_change_reason']})"
                if status["last_change_time"] else "keine")
        log_event(
            logger, logging.WARNING, "pump.fault_note",
            f"PUMPENFEHLER-NOTIZ: Modus={status['mode']}, Soll={status['setpoint_pct']:g}%, "
            f"Ist={status['actual_pct']:g}%, DAC={status['dac_output_v']}V, "
            f"Netz={'ja' if status['mains_connected'] else 'nein'}, "
            f"Zeitplan='{status['schedule_entry']}', letzte Änderung: {last}"
            + (f" — Notiz: {note}" if note else ""),
            **{f"pump.{k}": v for k, v in status.items()},
            **{"fault.note": note or None},
        )
        return {**status, "note": note}

    def protection_status(self) -> dict:
        """
        Frostschutz und Winterbetrieb (Mindestdrehzahl) für das Dashboard.
        frost.state:     active | ready | sensor_missing | disabled
        min_speed.state: active | standby (Saison, greift bei Zeitplan-0 %) |
                         off_season | off
        """
        with self._state_lock:
            frost_active = self.state.frost_active
            sensor_error = self.state.sensor_error
            min_active = self.state.min_speed_active
        frost_enabled = bool(self.frost_config.get("enabled", True))
        if not frost_enabled:
            frost_state = "disabled"
        elif frost_active:
            frost_state = "active"
        elif sensor_error:
            frost_state = "sensor_missing"
        else:
            frost_state = "ready"

        cfg = self._min_speed_cfg
        in_season = self._min_speed_pct(self._now()) is not None
        if cfg["mode"] == "off":
            min_state = "off"
        elif min_active:
            min_state = "active"
        elif in_season:
            min_state = "standby"
        else:
            min_state = "off_season"
        return {
            "frost": {
                "state": frost_state,
                "threshold_c": self.frost_config.get("temp_threshold_c"),
                "speed_pct": self._frost_pct(),
            },
            "min_speed": {
                "state": min_state,
                "mode": cfg["mode"],
                "speed_pct": cfg["pct"],
                "months": sorted(cfg["months"]),
            },
        }

    def get_metrics_snapshot(self) -> dict:
        """Thread-sichere Werte für Observable-Metriken (wird aus dem Metrik-Thread gelesen)"""
        with self._state_lock:
            return {
                "mode": self.state.mode,
                "frost_active": self.state.frost_active,
                "min_speed_active": self.state.min_speed_active,
                "backwash_runs": self.backwash_runs,
                "fault_notes": self.fault_notes_count,
            }

    # ── Status ────────────────────────────────────────────────

    def _update_state(self, **kwargs):
        """State aktualisieren und Callback aufrufen"""
        with self._state_lock:
            for key, value in kwargs.items():
                if hasattr(self.state, key):
                    setattr(self.state, key, value)
            self.state.last_changed = self._now().isoformat()
            snapshot = self.state.to_dict()

        if self.on_state_change:
            result = self.on_state_change("pump", snapshot)
            if asyncio.iscoroutine(result):
                # Callback ist async (MQTT publish) → im laufenden Loop einplanen
                try:
                    task = asyncio.get_running_loop().create_task(result)
                except RuntimeError:
                    result.close()
                    return
                self._background.add(task)
                task.add_done_callback(self._background.discard)

    def get_state(self) -> dict:
        with self._state_lock:
            return self.state.to_dict()

    async def handle_mqtt_command(self, command: str):
        """
        MQTT Befehle verarbeiten.
        Format: "on", "off", "speed:60", "backwash", "schedule"
        """
        command = command.strip().lower()
        logger.info(f"MQTT Befehl empfangen: {command}")

        try:
            if command == "on":
                await self.manual_on(60)
            elif command == "off":
                await self.manual_off()
            elif command.startswith("speed:"):
                try:
                    speed = int(command.split(":")[1])
                except ValueError:
                    logger.error(f"Ungültige Drehzahl: {command}")
                    return
                await self.manual_on(speed)
            elif command == "backwash":
                self.start_backwash()   # läuft als Task, blockiert MQTT nicht
            elif command == "schedule":
                self.resume_schedule()
            else:
                logger.warning(f"Unbekannter MQTT Befehl: {command}")
        except (EmergencyActive, BackwashBusy, ValueError) as e:
            logger.warning(f"MQTT Befehl '{command}' abgelehnt: {e}")
