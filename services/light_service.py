"""
Pool Marx — Beleuchtung: Zeitplan, manueller Betrieb, Dimmen

Modi:
- schedule: Der Zeitplan (lighting.schedule) bestimmt An/Aus und Helligkeit.
  Außerhalb aller Zeitfenster ist das Licht aus.
- manual:   Jeder manuelle Befehl (Dashboard, API, MQTT, Telegram) schaltet
  hierhin. Der Zeitplan ist pausiert, bis jemand „Zurück zum Zeitplan“ wählt.

Zeitfenster: start/end als "HH:MM" oder "sunset"/"sunrise" mit optionalem
Versatz in Minuten ("sunset+30", "sunrise-15"). Sonnenzeiten brauchen
lighting.latitude/longitude. Fenster über Mitternacht sind erlaubt
(z. B. 22:00–01:00). Überschneiden sich Fenster, gilt das erste in der Liste.

Abschaltung (Not-Aus): Licht geht aus (hardware.emergency_stop). Einschalten
und Heller-Stellen sind gesperrt, bis quittiert ist; Ausschalten geht immer.
"""

import asyncio
import logging
import math
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Callable, NamedTuple, Optional

from .events import log_event
from .pump_service import EmergencyActive

logger = logging.getLogger(__name__)

LIGHT_MODES = ("schedule", "manual")
LIGHTING_DEFAULTS = {
    "default_brightness_pct": 80,
    "check_interval_s": 20,
}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_SUN_RE = re.compile(r"^(sunrise|sunset)\s*(?:([+-])\s*(\d{1,3}))?$")
LEGACY_KEYS = ("auto_sunset", "off_time")


# ── Sonnenauf-/-untergang ─────────────────────────────────────

def sun_times_utc(day: date, lat: float, lon: float):
    """
    Sonnenaufgang und -untergang (UTC) nach der Sunrise-Equation
    (inkl. Refraktion, Genauigkeit ca. 1–2 Minuten).
    Gibt (aufgang, untergang) zurück; None bei Polartag/-nacht.
    """
    jd_noon = 2451545.0 + (day.toordinal() - date(2000, 1, 1).toordinal())
    n = jd_noon - 2451545.0 + 0.0008
    j_star = n - lon / 360.0
    m = math.radians((357.5291 + 0.98560028 * j_star) % 360)
    c = 1.9148 * math.sin(m) + 0.02 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)
    j_transit = 2451545.0 + j_star + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    sin_d = math.sin(lam) * math.sin(math.radians(23.4397))
    cos_d = math.cos(math.asin(sin_d))
    phi = math.radians(lat)
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(phi) * sin_d) / (math.cos(phi) * cos_d)
    if cos_w < -1 or cos_w > 1:
        return None, None
    w = math.degrees(math.acos(cos_w))
    epoch = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)
    to_dt = lambda j: epoch + timedelta(days=j - 2451545.0)   # noqa: E731
    return to_dt(j_transit - w / 360.0), to_dt(j_transit + w / 360.0)


def local_sun_times(day: date, lat: float, lon: float):
    """Sonnenzeiten als naive Ortszeit (Zeitzone des Systems, wie datetime.now())."""
    rise, set_ = sun_times_utc(day, lat, lon)
    conv = lambda t: t.astimezone().replace(tzinfo=None) if t else None   # noqa: E731
    return conv(rise), conv(set_)


# ── Konfiguration ─────────────────────────────────────────────

class TimeSpec(NamedTuple):
    kind: str             # "clock" | "sunrise" | "sunset"
    minutes: int          # clock: Minuten seit 0:00; sonst Versatz
    text: str


class LightEntry(NamedTuple):
    name: str
    start: TimeSpec
    end: TimeSpec
    brightness_pct: int


def parse_time_spec(value) -> TimeSpec:
    text = str(value).strip().lower()
    m = _TIME_RE.match(text)
    if m:
        return TimeSpec("clock", int(m.group(1)) * 60 + int(m.group(2)), text)
    m = _SUN_RE.match(text)
    if m:
        offset = int(m.group(3) or 0) * (-1 if m.group(2) == "-" else 1)
        if abs(offset) > 360:
            raise ValueError(f"Versatz {offset} min zu groß (max. 360)")
        return TimeSpec(m.group(1), offset, text)
    raise ValueError(f"'{value}' ist keine Zeit (HH:MM, sunset, sunrise, sunset+30 …)")


def _coords(raw: dict):
    try:
        lat, lon = float(raw.get("latitude", 0) or 0), float(raw.get("longitude", 0) or 0)
    except (TypeError, ValueError):
        return None
    if lat == 0 and lon == 0:
        return None   # Platzhalter in der Vorlage
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def load_lighting_config(config: dict) -> dict:
    """Abschnitt lighting lesen. Ungültige Einträge → Warnung, Eintrag fällt weg."""
    raw = (config or {}).get("lighting") or {}
    cfg = dict(LIGHTING_DEFAULTS)
    for key in LEGACY_KEYS:
        if key in raw:
            logger.warning(f"lighting.{key} wird nicht verwendet — Zeiten stehen jetzt in "
                           f"lighting.schedule (z. B. start: \"sunset\")")
    try:
        default = int(raw.get("default_brightness_pct", LIGHTING_DEFAULTS["default_brightness_pct"]))
        if not 1 <= default <= 100:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning(f"lighting.default_brightness_pct={raw.get('default_brightness_pct')!r} "
                       f"ungültig — verwende {LIGHTING_DEFAULTS['default_brightness_pct']}")
        default = LIGHTING_DEFAULTS["default_brightness_pct"]
    cfg["default_brightness_pct"] = default
    cfg["coords"] = _coords(raw)

    entries = []
    for i, item in enumerate(raw.get("schedule") or []):
        name = str((item or {}).get("name") or f"Eintrag {i + 1}")
        try:
            start = parse_time_spec(item["start"])
            end = parse_time_spec(item["end"])
            pct = item.get("brightness_pct", default)
            if isinstance(pct, bool):
                raise ValueError("brightness_pct muss eine Zahl sein")
            pct = int(pct)
            if not 0 <= pct <= 100:
                raise ValueError(f"brightness_pct {pct} außerhalb 0–100")
            if start.text == end.text:
                raise ValueError("start und end sind gleich")
            if cfg["coords"] is None and (start.kind != "clock" or end.kind != "clock"):
                raise ValueError("sunset/sunrise braucht lighting.latitude und longitude")
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"Licht-Zeitplan '{name}' ignoriert: {e}")
            continue
        entries.append(LightEntry(name, start, end, pct))
    cfg["schedule"] = entries
    return cfg


# ── Dienst ────────────────────────────────────────────────────

class LightService:
    """
    Steuert die Beleuchtung über hardware.set_light().

    Test-Hooks: now (Wanduhr), sleep (async), sun_times(day) → (aufgang, untergang)
    als naive Ortszeit.
    """

    def __init__(self, hardware, config: dict, pump_service=None,
                 on_state_change: Optional[Callable] = None, *,
                 now: Optional[Callable[[], datetime]] = None,
                 sleep: Optional[Callable] = None,
                 sun_times: Optional[Callable] = None):
        self.hw = hardware
        self.pump = pump_service
        self.on_state_change = on_state_change
        self.cfg = load_lighting_config(config)
        self._now = now or datetime.now
        self._sleep = sleep or asyncio.sleep
        coords = self.cfg["coords"]
        self._sun = sun_times or (
            (lambda d: local_sun_times(d, *coords)) if coords else (lambda d: (None, None)))
        self.mode = "schedule"
        self.entry_name: Optional[str] = None
        self._last_on_pct = self.cfg["default_brightness_pct"]
        self._running = False
        self._background: set = set()
        self._sun_warned: set = set()

    # ── Zeitplan ──────────────────────────────────────────────

    def _resolve(self, spec: TimeSpec, day: date) -> Optional[datetime]:
        if spec.kind == "clock":
            return datetime.combine(day, dtime()) + timedelta(minutes=spec.minutes)
        rise, set_ = self._sun(day)
        base = rise if spec.kind == "sunrise" else set_
        if base is None:
            if day not in self._sun_warned:
                self._sun_warned.add(day)
                logger.warning(f"Licht-Zeitplan: kein {spec.kind} am {day} — Fenster entfällt")
            return None
        return base + timedelta(minutes=spec.minutes)

    def _windows(self, day: date):
        """(start, end, Eintrag) aller Fenster, die an `day` beginnen."""
        for entry in self.cfg["schedule"]:
            start, end = self._resolve(entry.start, day), self._resolve(entry.end, day)
            if start is None or end is None:
                continue
            if end <= start:
                end += timedelta(days=1)    # über Mitternacht
            yield start, end, entry

    def scheduled(self, now: Optional[datetime] = None):
        """(Eintrag oder None, Helligkeit) laut Zeitplan zum Zeitpunkt now."""
        now = now or self._now()
        for day in (now.date(), now.date() - timedelta(days=1)):
            for start, end, entry in self._windows(day):
                if start <= now < end:
                    return entry, entry.brightness_pct
        return None, 0

    def next_change(self, now: Optional[datetime] = None):
        """Nächster Zeitpunkt (innerhalb 48 h), an dem der Zeitplan etwas anderes will."""
        now = now or self._now()
        current = self.scheduled(now)[1]
        points = set()
        for offset in (-1, 0, 1, 2):
            for start, end, _ in self._windows(now.date() + timedelta(days=offset)):
                points.update(t for t in (start, end) if now < t <= now + timedelta(hours=48))
        for t in sorted(points):
            entry, pct = self.scheduled(t)
            if pct != current:
                return {"time": t.isoformat(timespec="minutes"), "brightness_pct": pct,
                        "entry": entry.name if entry else None}
        return None

    async def start(self):
        """Zeitplan-Schleife. Fehler werden geloggt, die Schleife läuft weiter."""
        self._running = True
        logger.info(
            f"Beleuchtung: {len(self.cfg['schedule'])} Zeitplan-Einträge, "
            f"Dimmer {self.hw.dimmer.kind} (min {self.hw.dimmer_settings.min_pct}%), "
            f"Sonnenzeiten {'aktiv' if self.cfg['coords'] else 'aus (keine Koordinaten)'}")
        while self._running:
            try:
                self.apply_schedule()
            except Exception as e:
                logger.error(f"Licht-Zeitplan Fehler: {type(e).__name__}: {e}")
            await self._sleep(self.cfg["check_interval_s"])

    def stop(self):
        self._running = False

    def apply_schedule(self):
        """Im Modus schedule den Sollzustand herstellen (idempotent)."""
        if self.mode != "schedule":
            return
        entry, pct = self.scheduled()
        name = entry.name if entry else None
        if name != self.entry_name:
            log_event(logger, logging.INFO, "light.schedule_transition",
                      f"Licht-Zeitplan: '{self.entry_name}' → '{name}' ({pct}%)",
                      **{"light.schedule.from": self.entry_name, "light.schedule.to": name,
                         "light.brightness_pct": pct})
            self.entry_name = name
        if self._emergency and pct > self.hw.light_brightness:
            return   # nach Abschaltung nicht wieder einschalten
        if pct != self.hw.light_brightness:
            self._set(pct, reason="schedule", entry=name)

    # ── Befehle ───────────────────────────────────────────────

    @property
    def _emergency(self) -> bool:
        return bool(self.pump is not None and self.pump.emergency_active)

    def _set(self, pct: int, reason: str, entry: Optional[str] = None):
        actual = self.hw.set_light(pct, reason=reason, entry=entry)
        if actual > 0:
            self._last_on_pct = actual
        self._notify()
        return actual

    def set_brightness(self, pct) -> int:
        """Manuell: Helligkeit 0–100 % (0 = aus). Schaltet auf Modus manuell."""
        try:
            pct = int(pct)
        except (TypeError, ValueError):
            raise ValueError(f"Helligkeit ungültig: {pct!r}")
        if not 0 <= pct <= 100:
            raise ValueError("Helligkeit muss 0–100 sein")
        if pct > self.hw.light_brightness and self._emergency:
            raise EmergencyActive("Not-Aus aktiv — erst quittieren")
        self.mode = "manual"
        self.entry_name = None
        return self._set(pct, reason="manual")

    def turn_on(self, pct=None) -> int:
        return self.set_brightness(pct if pct else self._last_on_pct)

    def turn_off(self) -> int:
        return self.set_brightness(0)

    def set_manual(self):
        """Zeitplan pausieren, aktuellen Zustand beibehalten."""
        self.mode = "manual"
        self.entry_name = None
        logger.info("Beleuchtung: manueller Betrieb (Zeitplan pausiert)")
        self._notify()

    def resume_schedule(self):
        """Zurück zum Zeitplan (sofort angewendet)."""
        self.mode = "schedule"
        self.entry_name = None
        logger.info("Beleuchtung: zurück zum Zeitplan")
        self.apply_schedule()
        self._notify()

    async def handle_mqtt_command(self, payload: str):
        """MQTT pool_marx/cmd/light: on, off, brightness:NN, schedule, manual."""
        cmd = str(payload).strip().lower()
        try:
            if cmd == "on":
                self.turn_on()
            elif cmd == "off":
                self.turn_off()
            elif cmd.startswith("brightness:"):
                self.set_brightness(int(float(cmd.split(":", 1)[1])))
            elif cmd == "schedule":
                self.resume_schedule()
            elif cmd == "manual":
                self.set_manual()
            else:
                logger.warning(f"Unbekannter MQTT Licht-Befehl: {cmd}")
        except (EmergencyActive, ValueError) as e:
            logger.warning(f"MQTT Licht-Befehl '{cmd}' abgelehnt: {e}")

    # ── Status ────────────────────────────────────────────────

    def get_state(self) -> dict:
        pct = self.hw.light_brightness
        return {
            "on": pct > 0,
            "brightness_pct": pct,
            "mode": self.mode,
            "schedule_entry": self.entry_name if self.mode == "schedule" else None,
            "next_change": self.next_change() if self.mode == "schedule" else None,
            "default_brightness_pct": self._last_on_pct,
            "dimmer": self.hw.dimmer.kind,
            "min_pct": self.hw.dimmer_settings.min_pct,
            "schedule_entries": len(self.cfg["schedule"]),
        }

    def _notify(self):
        if not self.on_state_change:
            return
        try:
            result = self.on_state_change("light", self.get_state())
        except Exception as e:
            logger.error(f"Licht-Statusmeldung fehlgeschlagen: {e}")
            return
        if asyncio.iscoroutine(result):
            try:
                task = asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                result.close()
                return
            self._background.add(task)
            task.add_done_callback(self._background.discard)
