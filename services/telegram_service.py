"""
Pool Marx — Telegram-Bot (optional)

Zwei Aufgaben:
1. Benachrichtigungen: ein logging.Handler leitet ausgewählte Log-Meldungen
   an die freigegebenen Telegram-Nutzer weiter. Meldungen kurz hintereinander
   werden zu einer Nachricht gebündelt, die Anzahl pro Minute ist begrenzt.
   - laut (mit Ton): Warnungen und Fehler (ab notify_level, mindestens WARNING)
   - leise (ohne Ton): notify_events und — mit notify_state_changes —
     Zustandsänderungen (Pumpe %, Licht, Rückspülung). Per /stumm abschaltbar.
2. Befehle: Long Polling (getUpdates). Der Pi baut die Verbindung zu
   api.telegram.org selbst auf — keine Portfreigabe, kein Webhook.

Sicherheit:
- Nur Nutzer-IDs aus telegram.allowed_user_ids, nur private Chats.
- Rückspülen und Abschalten nur nach Bestätigung per Knopf (Zeitlimit).
- Not-Aus quittieren / Netzspannung zurückschalten geht bewusst NICHT per
  Telegram (nur am Dashboard im LAN, siehe ROADMAP Punkt 1).
- Alte Befehle (z. B. während der Pi offline war) werden verworfen.
- Der Bot-Token steht nur in der Umgebung (POOL_TELEGRAM_TOKEN) und taucht
  in keinem Log auf (httpx-Request-Logs werden gedämpft, Fehlertexte bereinigt).

Die Steuerung hängt nie vom Bot ab: Handler blockiert nicht, Fehler im Bot
werden geloggt und beenden höchstens den Bot, nicht den Dienst.
"""

import asyncio
import logging
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

from .events import log_event, sanitize_text
from .pump_service import BackwashBusy, EmergencyActive
from .version import __version__

# Transport-Logger: wird NICHT an Telegram weitergeleitet (sonst Schleife,
# wenn Telegram nicht erreichbar ist)
logger = logging.getLogger(__name__)
# Befehle / abgewiesene Nutzer: wird weitergeleitet (ab notify_level)
audit = logging.getLogger("pool_marx.telegram")

TELEGRAM_DEFAULTS = {
    "enabled": False,
    "allowed_user_ids": [],
    "notify_level": "WARNING",
    "notify_events": ["frost.deactivated", "backwash.finished", "pump.mains_restore"],
    "notify_startup": True,
    "notify_state_changes": False,
    "commands_enabled": True,
    "manual_duration_min": 120,
    "max_command_age_s": 120,
    "confirm_timeout_s": 60,
    "max_messages_per_min": 6,
    "repeat_suppress_min": 15,
    "bundle_interval_s": 2.0,
    "poll_timeout_s": 30,
}

# Zustandsänderungen (leise, mit notify_state_changes)
STATE_EVENTS = frozenset({"pump.setpoint_changed", "light.changed",
                          "backwash.started", "backwash.finished"})
EVENT_ICONS = {"pump.setpoint_changed": "🌀", "light.changed": "💡",
               "backwash.started": "🔄", "backwash.finished": "🔄",
               "frost.deactivated": "❄️", "pump.mains_restore": "🔌"}
REASON_NAMES = {"schedule": "Zeitplan", "min_speed": "Winterbetrieb", "manual": "manuell",
                "frost": "Frostschutz", "backwash": "Rückspülung", "emergency": "Abschaltung",
                "mains_cutoff": "Netz getrennt", "shutdown": "Dienst beendet",
                "other": "sonstiges"}
BACKWASH_RESULT_NAMES = {"ok": "fertig", "cancelled": "abgebrochen", "error": "mit Fehler beendet"}
MAX_MUTE_HOURS = 168

# Nie weiterleiten: eigener Transport + HTTP-Bibliotheken
EXCLUDED_LOGGERS = (__name__, "httpx", "httpcore")
MAX_MESSAGE_LEN = 4000          # Telegram-Grenze 4096
OUTBOX_SIZE = 200
BACKOFF_MAX_S = 60.0
UNAUTHORIZED_LOG_INTERVAL_S = 3600.0
STARTUP_DELAY_S = 5.0           # Startmeldung erst, wenn der Zeitplan angewendet ist
_DIGITS_RE = re.compile(r"\d+(?:[.,]\d+)?")

MODE_NAMES = {
    "schedule": "Zeitplan", "manual": "manuell", "frost": "Frostschutz",
    "backwash": "Rückspülung", "off": "aus", "emergency": "NOT-AUS",
}
FROST_NAMES = {"active": "aktiv", "ready": "bereit", "sensor_missing": "Sensor fehlt",
               "disabled": "deaktiviert"}
MIN_SPEED_NAMES = {"active": "aktiv", "standby": "Bereitschaft",
                   "off_season": "außerhalb der Saison", "off": "aus"}

HELP_TEXT = (
    "Pool Marx — Befehle\n"
    "/status — aktueller Zustand\n"
    "/pumpe 60 — Drehzahl in % (zurück zum Zeitplan nach {dur})\n"
    "/pumpe 60 30 — Drehzahl für 30 Minuten (0 = ohne Zeitlimit)\n"
    "/aus — Pumpe aus (zurück zum Zeitplan nach {dur})\n"
    "/zeitplan — zurück zum Zeitplan\n"
    "/licht an | /licht aus\n"
    "/rueckspuelen — Rückspülung starten (mit Bestätigung)\n"
    "/abschalten — alles abschalten und verriegeln (mit Bestätigung)\n"
    "/notiz <Text> — Pumpenfehler notieren (z. B. E-04)\n"
    "/stumm — Statusmeldungen aus (Warnungen kommen weiter)\n"
    "/stumm 3 — Statusmeldungen für 3 Stunden aus\n"
    "/laut — Statusmeldungen wieder an\n"
    "\n"
    "Not-Aus quittieren geht nur am Dashboard im LAN."
)


class TelegramApiError(Exception):
    """Fehler der Bot-API oder der Verbindung. Text enthält nie den Token."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


ApiCall = Callable[[str, dict, Optional[float]], Awaitable]


# ── Konfiguration ─────────────────────────────────────────────

def _int_in(raw: dict, key: str, lo: int, hi: int) -> int:
    default = TELEGRAM_DEFAULTS[key]
    value = raw.get(key, default)
    try:
        if isinstance(value, bool):
            raise TypeError
        value = int(value)
        if not lo <= value <= hi:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning(f"telegram.{key}={raw.get(key)!r} ungültig — verwende {default}")
        return default
    return value


def load_telegram_config(config: dict) -> dict:
    """Abschnitt telegram: lesen; fehlt er → Standardwerte (aus)."""
    raw = (config or {}).get("telegram") or {}
    cfg = dict(TELEGRAM_DEFAULTS)
    cfg["enabled"] = raw.get("enabled", False) is True
    cfg["notify_startup"] = raw.get("notify_startup", True) is True
    cfg["notify_state_changes"] = raw.get("notify_state_changes", False) is True
    cfg["commands_enabled"] = raw.get("commands_enabled", True) is True

    ids = raw.get("allowed_user_ids") or []
    if not isinstance(ids, list):
        ids = [ids]
    valid = []
    for uid in ids:
        if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0:
            valid.append(uid)
        elif isinstance(uid, str) and uid.strip().isdigit():
            valid.append(int(uid.strip()))
        else:
            hint = (" — das sieht nach einem Namen aus (z. B. dem Bot-Namen). Gemeint ist "
                    "deine eigene Telegram-Nutzer-ID, eine Zahl wie 518273645 "
                    "(ermitteln: docs/telegram.md, Abschnitt 3)"
                    if isinstance(uid, str) and not uid.strip().lstrip("-").isdigit() else "")
            logger.warning(f"telegram.allowed_user_ids: {uid!r} ist keine Nutzer-ID — "
                           f"ignoriert{hint}")
    cfg["allowed_user_ids"] = valid

    level = str(raw.get("notify_level", "WARNING")).upper()
    if not isinstance(logging.getLevelName(level), int):
        logger.warning(f"telegram.notify_level={raw.get('notify_level')!r} ungültig — verwende WARNING")
        level = "WARNING"
    cfg["notify_level"] = level

    events = raw.get("notify_events", TELEGRAM_DEFAULTS["notify_events"])
    cfg["notify_events"] = [str(e) for e in events] if isinstance(events, list) else []

    cfg["manual_duration_min"] = _int_in(raw, "manual_duration_min", 0, 1440)
    cfg["max_command_age_s"] = _int_in(raw, "max_command_age_s", 10, 3600)
    cfg["confirm_timeout_s"] = _int_in(raw, "confirm_timeout_s", 10, 600)
    cfg["max_messages_per_min"] = _int_in(raw, "max_messages_per_min", 1, 20)
    cfg["repeat_suppress_min"] = _int_in(raw, "repeat_suppress_min", 0, 1440)
    cfg["poll_timeout_s"] = _int_in(raw, "poll_timeout_s", 1, 50)
    try:
        bundle = float(raw.get("bundle_interval_s", TELEGRAM_DEFAULTS["bundle_interval_s"]))
        if not 0.5 <= bundle <= 60:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning(f"telegram.bundle_interval_s={raw.get('bundle_interval_s')!r} ungültig — "
                       f"verwende {TELEGRAM_DEFAULTS['bundle_interval_s']}")
        bundle = TELEGRAM_DEFAULTS["bundle_interval_s"]
    cfg["bundle_interval_s"] = bundle
    return cfg


# ── HTTP (httpx) ──────────────────────────────────────────────

class HttpxApi:
    """Bot-API über httpx. Der Token steht nur in der Basis-URL."""

    def __init__(self, token: str, connect_timeout_s: float = 10.0):
        import httpx   # erst hier: ohne Telegram keine Abhängigkeit nötig
        self._httpx = httpx
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/",
            timeout=httpx.Timeout(connect_timeout_s),
        )

    def _redact(self, text: str) -> str:
        return str(text).replace(self._token, "***") if self._token else str(text)

    async def __call__(self, method: str, payload: dict, read_timeout_s: Optional[float] = None):
        httpx = self._httpx
        timeout = httpx.Timeout(10.0, read=read_timeout_s) if read_timeout_s else None
        try:
            kwargs = {"json": payload}
            if timeout is not None:
                kwargs["timeout"] = timeout
            resp = await self._client.post(method, **kwargs)
        except httpx.HTTPError as e:
            # Nur Typ + bereinigter Text; Exceptions können die URL enthalten
            raise TelegramApiError(self._redact(f"{type(e).__name__}: {e}")) from None
        try:
            data = resp.json()
        except ValueError:
            raise TelegramApiError(f"HTTP {resp.status_code}, keine JSON-Antwort",
                                   status=resp.status_code) from None
        if not data.get("ok"):
            params = data.get("parameters") or {}
            raise TelegramApiError(
                self._redact(f"HTTP {resp.status_code}: {data.get('description', 'unbekannt')}"),
                status=resp.status_code, retry_after=params.get("retry_after"))
        return data.get("result")

    async def close(self):
        try:
            await self._client.aclose()
        except Exception:
            pass


# ── Log-Handler ───────────────────────────────────────────────

class TelegramNotifyHandler(logging.Handler):
    """
    Wählt Log-Meldungen aus und legt sie in den Postausgang. Blockiert nie
    (wird auch aus dem DAC-Thread aufgerufen), sendet selbst nichts.

    Laut (mit Ton): ab WARNING; gleiche Meldungen werden nur alle
    repeat_suppress_s weitergegeben. Leise (ohne Ton): alles darunter, also
    notify_events und Zustandsänderungen; fällt weg, solange muted() True ist.
    """

    ICONS = {logging.CRITICAL: "🛑", logging.ERROR: "🔴", logging.WARNING: "⚠️"}

    def __init__(self, notify_level: int, notify_events, submit: Callable[[str, bool], None],
                 redact: Optional[Callable[[str], str]] = None,
                 repeat_suppress_s: float = 0.0,
                 monotonic: Callable[[], float] = time.monotonic,
                 *,
                 state_changes: bool = False,
                 formatter: Optional[Callable[[logging.LogRecord], Optional[str]]] = None,
                 muted: Optional[Callable[[], bool]] = None):
        super().__init__(level=logging.DEBUG)   # Auswahl in accept()
        self.notify_level = notify_level
        self.notify_events = frozenset(notify_events)
        self.state_changes = state_changes
        self._submit = submit
        self._redact = redact or (lambda s: s)
        self._repeat_s = repeat_suppress_s
        self._monotonic = monotonic
        self._formatter = formatter
        self._muted = muted
        self._recent: dict = {}   # Schlüssel → [zuletzt gesendet, unterdrückt seitdem]

    def accept(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        for excluded in EXCLUDED_LOGGERS:
            if name == excluded or name.startswith(excluded + "."):
                return False
        if record.levelno >= self.notify_level:
            return True
        event = getattr(record, "event.name", None)
        return event in self.notify_events or (self.state_changes and event in STATE_EVENTS)

    @staticmethod
    def is_silent(record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING

    def format_line(self, record: logging.LogRecord, text: Optional[str] = None) -> str:
        icon = self.ICONS.get(record.levelno) or EVENT_ICONS.get(
            getattr(record, "event.name", None), "ℹ️")
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        text = sanitize_text(text if text is not None else record.getMessage(), max_len=600)
        return self._redact(f"{icon} {stamp} {text}")

    def _repeat_key(self, record: logging.LogRecord) -> tuple:
        # Zahlen ausblenden: "reconnect in 10s", "gesamt 17 Fehler" zählen als gleich
        return (record.name, record.levelno, _DIGITS_RE.sub("#", record.getMessage()))

    def _suppressed(self, record: logging.LogRecord) -> Optional[int]:
        """None = unterdrücken, sonst Anzahl der seit dem letzten Senden unterdrückten."""
        if self._repeat_s <= 0:
            return 0
        now = self._monotonic()
        if len(self._recent) > 500:   # Speicher begrenzen
            self._recent = {k: v for k, v in self._recent.items()
                            if now - v[0] < self._repeat_s}
        key = self._repeat_key(record)
        entry = self._recent.get(key)
        if entry is not None and now - entry[0] < self._repeat_s:
            entry[1] += 1
            return None
        skipped = entry[1] if entry is not None else 0
        self._recent[key] = [now, 0]
        return skipped

    def emit(self, record: logging.LogRecord):
        try:
            if not self.accept(record):
                return
            silent = self.is_silent(record)
            skipped = 0
            if silent:
                # Zustandsänderungen nie als Wiederholung verwerfen (30→60→30 % sind
                # drei echte Änderungen) — aber per /stumm abschaltbar
                if self._muted is not None and self._muted():
                    return
            else:
                skipped = self._suppressed(record)
                if skipped is None:
                    return
            text = self._formatter(record) if self._formatter is not None else None
            line = self.format_line(record, text)
            if skipped:
                line += f" (seit der letzten Meldung {skipped}× wiederholt)"
            self._submit(line, silent)
        except Exception:   # Logging darf die Steuerung nie stören
            pass


# ── Dienst ────────────────────────────────────────────────────

class TelegramService:
    """
    Telegram-Bot für Benachrichtigungen und (optional) Befehle.

    Test-Hooks: api (async (method, payload, read_timeout) -> result),
    clock (Unix-Zeit), monotonic, sleep.
    """

    def __init__(self, config: dict, pump_service, hardware, *,
                 api: Optional[ApiCall] = None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Optional[Callable] = None):
        self.cfg = load_telegram_config(config)
        self.pump = pump_service
        self.hw = hardware
        self._token = ((config or {}).get("telegram") or {}).get("token") or ""
        self._api = api
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep or asyncio.sleep

        self._outbox: deque = deque()
        self._outbox_lock = threading.Lock()
        self._dropped = 0
        self._sent_times: deque = deque()
        self._offset: Optional[int] = None
        self._pending: dict = {}              # nonce → (action, user_id, expires)
        self._unauthorized_seen: dict = {}    # user_id → monotonic
        self._handler: Optional[TelegramNotifyHandler] = None
        self._tasks: list = []
        self._offline_since: Optional[float] = None
        self._muted_until: Optional[float] = None    # monotonic; inf = bis /laut
        self._muted_until_wall: Optional[datetime] = None
        self._muted_count = 0
        self._running = False
        self.enabled = False
        self.disabled_reason = ""

    # ── Lebenszyklus ──────────────────────────────────────────

    def _check_enabled(self) -> bool:
        if not self.cfg["enabled"]:
            self.disabled_reason = "telegram.enabled ist false"
        elif not self._token and self._api is None:
            self.disabled_reason = "POOL_TELEGRAM_TOKEN fehlt"
        elif not self.cfg["allowed_user_ids"]:
            self.disabled_reason = ("telegram.allowed_user_ids ist leer — eigene "
                                    "Telegram-Nutzer-ID (Zahl) eintragen, siehe docs/telegram.md")
        else:
            return True
        return False

    def redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    async def start(self):
        """Bot starten. Wirft nie; bei Fehler/aus läuft der Dienst ohne Bot weiter."""
        if not self._check_enabled():
            if self.cfg["enabled"]:
                logger.warning(f"Telegram deaktiviert: {self.disabled_reason}")
            else:
                logger.info("Telegram deaktiviert (telegram.enabled: false)")
            return
        if self._api is None:
            try:
                self._api = HttpxApi(self._token)
            except ImportError:
                logger.warning("Telegram deaktiviert: Paket httpx fehlt")
                return
        # httpx loggt jede Anfrage samt URL (= Token) auf INFO → dämpfen
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)

        self.enabled = True
        self._running = True
        self._handler = TelegramNotifyHandler(
            logging.getLevelName(self.cfg["notify_level"]),
            self.cfg["notify_events"], self.submit, self.redact,
            repeat_suppress_s=self.cfg["repeat_suppress_min"] * 60.0,
            monotonic=self._monotonic,
            state_changes=self.cfg["notify_state_changes"],
            formatter=self.format_event, muted=self._is_muted)
        logging.getLogger().addHandler(self._handler)

        try:
            me = await self._api("getMe", {}, None)
            logger.info(f"Telegram aktiv: Bot @{(me or {}).get('username', '?')}, "
                        f"{len(self.cfg['allowed_user_ids'])} Nutzer freigegeben, "
                        f"Befehle {'an' if self.cfg['commands_enabled'] else 'aus'}")
            await self._set_commands()
        except TelegramApiError as e:
            if e.status == 401:
                logger.error("Telegram deaktiviert: Bot-Token ungültig (HTTP 401)")
                await self.stop()
                return
            logger.warning(f"Telegram beim Start nicht erreichbar ({e}) — versuche es weiter")

        # Abfrage läuft immer: /status und /hilfe gehen auch ohne Steuerbefehle
        self._tasks = [asyncio.create_task(self._sender_loop(), name="telegram-send"),
                       asyncio.create_task(self._poll_loop(), name="telegram-poll")]
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            raise
        except Exception as e:   # Bot stirbt, Steuerung läuft weiter
            logger.error(f"Telegram-Bot beendet: {type(e).__name__}: {self.redact(str(e))}")
            await self.stop()

    async def stop(self, flush_timeout_s: float = 0.0, final_message: Optional[str] = None):
        """Handler entfernen, optional letzte Nachrichten senden, Verbindung schließen."""
        self._running = False
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks = []
        if self.enabled and flush_timeout_s > 0:
            if final_message:
                self.submit(final_message)
            try:
                await asyncio.wait_for(self._flush(), timeout=flush_timeout_s)
            except (asyncio.TimeoutError, Exception):
                pass
        self.enabled = False
        close = getattr(self._api, "close", None)
        if close is not None:
            await close()

    # ── Senden ────────────────────────────────────────────────

    def submit(self, text: str, silent: bool = False):
        """
        Thread-sicher: Nachricht in den Postausgang legen (älteste fallen raus).
        silent=True → ohne Ton (disable_notification), solange die ganze
        gebündelte Nachricht nur aus leisen Zeilen besteht.
        """
        with self._outbox_lock:
            if len(self._outbox) >= OUTBOX_SIZE:
                self._outbox.popleft()
                self._dropped += 1
            self._outbox.append((text, silent))

    def _take_batch(self) -> Optional[tuple]:
        """(Text, leise) der nächsten gebündelten Nachricht oder None."""
        with self._outbox_lock:
            if not self._outbox:
                return None
            lines, size, silent = [], 0, True
            while self._outbox and size + len(self._outbox[0][0]) + 1 <= MAX_MESSAGE_LEN:
                line, quiet = self._outbox.popleft()
                lines.append(line)
                silent = silent and quiet
                size += len(line) + 1
            if not lines:   # einzelne überlange Zeile
                line, quiet = self._outbox.popleft()
                lines.append(line[:MAX_MESSAGE_LEN - 1] + "…")
                silent = quiet
            dropped, self._dropped = self._dropped, 0
        if dropped:
            lines.append(f"(… {dropped} ältere Meldungen verworfen)")
        return "\n".join(lines), silent

    def _put_back(self, batch: tuple):
        with self._outbox_lock:
            self._outbox.appendleft(batch)
            while len(self._outbox) > OUTBOX_SIZE:
                self._outbox.pop()
                self._dropped += 1

    def _rate_ok(self) -> bool:
        now = self._monotonic()
        while self._sent_times and now - self._sent_times[0] >= 60.0:
            self._sent_times.popleft()
        return len(self._sent_times) < self.cfg["max_messages_per_min"]

    async def _send_to_all(self, text: str, silent: bool = False):
        for uid in self.cfg["allowed_user_ids"]:
            payload = {"chat_id": uid, "text": text, "disable_web_page_preview": True}
            if silent:
                payload["disable_notification"] = True
            await self._api("sendMessage", payload, None)

    async def _flush(self):
        while True:
            batch = self._take_batch()
            if batch is None:
                return
            await self._send_to_all(*batch)

    async def _sender_loop(self):
        if self.cfg["notify_startup"]:
            await self._sleep(STARTUP_DELAY_S)
            self.submit(f"✅ Pool Marx {__version__} gestartet\n{self.status_text()}")
        backoff = 0.0
        while self._running:
            await self._sleep(max(self.cfg["bundle_interval_s"], backoff))
            self._check_mute_expired()
            if not self._rate_ok():
                continue   # Meldungen sammeln sich weiter, gehen gebündelt raus
            batch = self._take_batch()
            if batch is None:
                continue
            try:
                await self._send_to_all(*batch)
                self._sent_times.append(self._monotonic())
                backoff = 0.0
                self._mark_online()
            except TelegramApiError as e:
                self._put_back(batch)
                backoff = self._next_backoff(backoff, e)
                self._mark_offline(e)

    # ── Stumm / laut ──────────────────────────────────────────

    def _is_muted(self) -> bool:
        """Vom Handler (beliebiger Thread) gefragt: leise Meldung verwerfen?"""
        until = self._muted_until
        if until is None or self._monotonic() >= until:
            return False
        self._muted_count += 1
        return True

    def mute(self, hours: Optional[int]):
        now = self._monotonic()
        if hours:
            self._muted_until = now + hours * 3600.0
            self._muted_until_wall = datetime.now() + timedelta(hours=hours)
        else:
            self._muted_until = float("inf")
            self._muted_until_wall = None
        self._muted_count = 0

    def unmute(self) -> Optional[int]:
        """Stumm aufheben. Gibt die Anzahl unterdrückter Meldungen zurück (None = war nicht stumm)."""
        if self._muted_until is None:
            return None
        count = self._muted_count
        self._muted_until = None
        self._muted_until_wall = None
        self._muted_count = 0
        return count

    def _check_mute_expired(self):
        if self._muted_until is not None and self._monotonic() >= self._muted_until:
            count = self.unmute()
            self.submit(f"🔈 Statusmeldungen wieder an ({count} in der Zwischenzeit unterdrückt)",
                        silent=True)

    def _mute_text(self) -> Optional[str]:
        if self._muted_until is None:
            return None
        if self._muted_until_wall is None:
            return "🔇 Statusmeldungen stumm bis /laut"
        return f"🔇 Statusmeldungen stumm bis {self._muted_until_wall:%H:%M}"

    # ── Text für Zustandsänderungen ───────────────────────────

    def format_event(self, record: logging.LogRecord) -> Optional[str]:
        """
        Lesbarer Text für bekannte Ereignisse, sonst None (→ Log-Text).
        Wird im Handler aufgerufen, auch aus dem DAC-Thread: nur Attribute
        lesen, keine Locks nehmen.
        """
        event = getattr(record, "event.name", None)
        attr = lambda key: getattr(record, key, None)   # noqa: E731
        if event == "pump.setpoint_changed":
            old, new = attr("pump.setpoint.old"), attr("pump.setpoint.new")
            reason = attr("pump.setpoint.reason")
            why = REASON_NAMES.get(reason, reason or "?")
            if reason in ("schedule", "min_speed"):
                entry = getattr(self.pump, "_last_schedule_name", None)
                if entry:
                    why += f" „{entry}“"
            return f"Pumpe {old:g} → {new:g} % ({why})"
        if event == "light.changed":
            return "Licht an" if attr("light.on") else "Licht aus"
        if event == "backwash.started":
            return f"Rückspülung startet ({attr('backwash.speed_pct')} %)"
        if event == "backwash.finished":
            result = BACKWASH_RESULT_NAMES.get(attr("backwash.result"), attr("backwash.result"))
            return f"Rückspülung {result} nach {attr('backwash.duration_s') or 0:.0f} s"
        return None

    # ── Empfangen ─────────────────────────────────────────────

    async def _poll_loop(self):
        backoff = 0.0
        timeout = self.cfg["poll_timeout_s"]
        while self._running:
            if backoff:
                await self._sleep(backoff)
            payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
            if self._offset is not None:
                payload["offset"] = self._offset
            try:
                updates = await self._api("getUpdates", payload, timeout + 15)
                backoff = 0.0
                self._mark_online()
            except TelegramApiError as e:
                backoff = self._next_backoff(backoff, e)
                self._mark_offline(e)
                continue
            for update in updates or []:
                self._offset = max(self._offset or 0, int(update.get("update_id", 0)) + 1)
                try:
                    await self.handle_update(update)
                except Exception as e:   # ein kaputtes Update stoppt den Bot nicht
                    logger.error(f"Telegram-Update nicht verarbeitet: {type(e).__name__}: "
                                 f"{self.redact(str(e))}")

    @staticmethod
    def _next_backoff(current: float, err: TelegramApiError) -> float:
        if err.retry_after:
            return float(min(max(err.retry_after, 1), 300))
        return min(BACKOFF_MAX_S, max(5.0, current * 2))

    def _mark_offline(self, err: TelegramApiError):
        if self._offline_since is None:
            self._offline_since = self._monotonic()
            logger.warning(f"Telegram nicht erreichbar: {err} — neuer Versuch mit Wartezeit")

    def _mark_online(self):
        if self._offline_since is not None:
            down = self._monotonic() - self._offline_since
            self._offline_since = None
            logger.info(f"Telegram wieder erreichbar (nach {down:.0f}s)")

    # ── Updates / Befehle ─────────────────────────────────────

    def _authorized(self, user: dict, chat: Optional[dict] = None) -> bool:
        uid = (user or {}).get("id")
        ok = uid in self.cfg["allowed_user_ids"]
        if chat is not None and chat.get("type") != "private":
            ok = False
        if not ok:
            now = self._monotonic()
            last = self._unauthorized_seen.get(uid)
            if last is None or now - last >= UNAUTHORIZED_LOG_INTERVAL_S:
                self._unauthorized_seen[uid] = now
                log_event(audit, logging.WARNING, "telegram.unauthorized",
                          f"Telegram: Nachricht von nicht freigegebenem Nutzer {uid} "
                          f"(@{sanitize_text((user or {}).get('username'), 40) or '-'}) ignoriert",
                          **{"telegram.user_id": uid,
                             "telegram.chat_type": (chat or {}).get("type")})
        return ok

    async def _reply(self, chat_id: int, text: str, buttons: Optional[list] = None):
        payload = {"chat_id": chat_id, "text": text[:MAX_MESSAGE_LEN],
                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        try:
            await self._api("sendMessage", payload, None)
        except TelegramApiError as e:
            logger.warning(f"Telegram-Antwort nicht gesendet: {e}")

    async def handle_update(self, update: dict):
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return
        msg = update.get("message")
        if not msg or "text" not in msg:
            return
        chat = msg.get("chat") or {}
        if not self._authorized(msg.get("from"), chat):
            return
        chat_id = chat["id"]
        age = self._clock() - float(msg.get("date", 0))
        if age > self.cfg["max_command_age_s"]:
            log_event(audit, logging.INFO, "telegram.stale",
                      f"Telegram-Befehl verworfen (zu alt: {age:.0f}s)",
                      **{"telegram.user_id": msg["from"]["id"], "telegram.age_s": age})
            await self._reply(chat_id, f"Befehl ignoriert — er ist {age / 60:.0f} Minuten alt. "
                                       f"Bitte neu senden, falls noch gewünscht.")
            return
        text = msg["text"].strip()
        if not text.startswith("/"):
            await self._reply(chat_id, "Befehle beginnen mit / — siehe /hilfe")
            return
        parts = text[1:].split()
        cmd = parts[0].split("@", 1)[0].lower() if parts else ""
        args = parts[1:]
        log_event(audit, logging.INFO, "telegram.command",
                  f"Telegram-Befehl /{cmd} {' '.join(args)}".rstrip(),
                  **{"telegram.user_id": msg["from"]["id"], "telegram.command": cmd})
        reply, buttons = await self.execute(cmd, args, msg["from"]["id"])
        await self._reply(chat_id, reply, buttons)

    async def _handle_callback(self, cq: dict):
        user = cq.get("from") or {}
        try:
            await self._api("answerCallbackQuery", {"callback_query_id": cq.get("id")}, None)
        except TelegramApiError:
            pass
        if not self._authorized(user):
            return
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id", user.get("id"))
        answer, _, nonce = str(cq.get("data", "")).partition(":")
        pending = self._pending.pop(nonce, None)
        if pending is None:
            text = "Diese Abfrage ist nicht mehr gültig."
        else:
            action, owner, expires = pending
            if owner != user.get("id"):
                text = "Diese Abfrage gehört zu einem anderen Nutzer."
            elif self._monotonic() > expires:
                text = "Zeit abgelaufen — Befehl nicht ausgeführt."
            elif answer != "ok":
                text = "Abgebrochen."
            else:
                log_event(audit, logging.INFO, "telegram.confirmed",
                          f"Telegram: {action} bestätigt",
                          **{"telegram.user_id": user.get("id"), "telegram.command": action})
                text = await self._run_confirmed(action)
        try:
            await self._api("editMessageText", {"chat_id": chat_id,
                                                "message_id": message.get("message_id"),
                                                "text": text}, None)
        except TelegramApiError:
            await self._reply(chat_id, text)

    def _ask_confirm(self, action: str, user_id: int, question: str):
        now = self._monotonic()
        for k in [k for k, (_, _, exp) in self._pending.items() if exp < now]:
            del self._pending[k]
        nonce = secrets.token_hex(6)
        self._pending[nonce] = (action, user_id, now + self.cfg["confirm_timeout_s"])
        buttons = [{"text": "✅ Ja", "callback_data": f"ok:{nonce}"},
                   {"text": "✖️ Nein", "callback_data": f"no:{nonce}"}]
        return f"{question}\n(gilt {self.cfg['confirm_timeout_s']} s)", buttons

    def _duration_note(self, minutes: int) -> str:
        if not minutes:
            return "ohne Zeitlimit — zurück mit /zeitplan"
        until = (datetime.now() + timedelta(minutes=minutes)).strftime("%H:%M")
        return f"zurück zum Zeitplan um {until}"

    def _parse_duration(self, args: list) -> int:
        if not args:
            return self.cfg["manual_duration_min"]
        value = int(args[0])
        if not 0 <= value <= 1440:
            raise ValueError
        return value

    async def execute(self, cmd: str, args: list, user_id: int):
        """Befehl ausführen. Gibt (Antworttext, Knöpfe oder None) zurück."""
        if cmd in ("start", "hilfe", "help"):
            return HELP_TEXT.format(dur=self._duration_text()), None
        if cmd == "status":
            return self.status_text(), None
        # Meldungen steuern ist keine Pool-Steuerung → auch mit commands_enabled: false
        if cmd in ("stumm", "mute"):
            try:
                hours = int(args[0]) if args else 0
                if not 0 <= hours <= MAX_MUTE_HOURS:
                    raise ValueError
            except ValueError:
                return f"Aufruf: /stumm [Stunden 1–{MAX_MUTE_HOURS}, ohne = bis /laut]", None
            self.mute(hours)
            note = "" if self.cfg["notify_state_changes"] else (
                "\n(Zustandsänderungen sind in der Config ohnehin aus; "
                "betrifft nur die übrigen leisen Meldungen.)")
            return (f"{self._mute_text()}. Warnungen und Fehler kommen weiter.\n"
                    f"Gilt bis zum nächsten Neustart des Dienstes.{note}"), None
        if cmd in ("laut", "unmute"):
            count = self.unmute()
            if count is None:
                return "Statusmeldungen sind nicht stumm.", None
            return f"🔈 Statusmeldungen wieder an ({count} in der Zwischenzeit unterdrückt).", None
        known = ("pumpe", "aus", "zeitplan", "licht", "rueckspuelen", "rückspülen",
                 "abschalten", "notiz", "quittieren")
        if cmd not in known:
            return f"Unbekannter Befehl /{cmd} — siehe /hilfe", None
        if not self.cfg["commands_enabled"]:
            return "Steuerung per Telegram ist deaktiviert (telegram.commands_enabled).", None

        try:
            if cmd == "pumpe":
                try:
                    pct = int(args[0])
                    if not 0 <= pct <= 100:
                        raise ValueError
                    minutes = self._parse_duration(args[1:])
                except (IndexError, ValueError):
                    return "Aufruf: /pumpe <0–100> [Minuten, 0 = ohne Limit]", None
                await self.pump.manual_on(pct, minutes or None)
                return self._after_pump_command(pct, minutes), None
            if cmd == "aus":
                try:
                    minutes = self._parse_duration(args)
                except ValueError:
                    return "Aufruf: /aus [Minuten, 0 = ohne Limit]", None
                await self.pump.manual_on(0, minutes or None)
                return self._after_pump_command(0, minutes), None
            if cmd == "zeitplan":
                self.pump.resume_schedule()
                return f"Zurück zum Zeitplan.\n{self._pump_line()}", None
            if cmd == "licht":
                arg = args[0].lower() if args else ""
                if arg in ("an", "ein", "on"):
                    if self.pump.emergency_active:
                        raise EmergencyActive("Not-Aus aktiv")
                    self.hw.light_on()
                    return "Licht an.", None
                if arg in ("aus", "off"):
                    self.hw.light_off()
                    return "Licht aus.", None
                return "Aufruf: /licht an | /licht aus", None
            if cmd in ("rueckspuelen", "rückspülen"):
                if self.pump.emergency_active:
                    raise EmergencyActive("Not-Aus aktiv")
                if self.pump.backwash_running:
                    raise BackwashBusy("Rückspülung läuft bereits")
                return self._ask_confirm("backwash", user_id, "Rückspülung jetzt starten?")
            if cmd == "abschalten":
                return self._ask_confirm(
                    "shutdown", user_id,
                    "Alles abschalten und verriegeln? Pumpe wird vom Netz getrennt. "
                    "Wieder einschalten geht NUR am Dashboard im LAN.")
            if cmd == "notiz":
                status = self.pump.record_fault_note(" ".join(args) or None)
                return (f"Notiert: Modus {MODE_NAMES.get(status['mode'], status['mode'])}, "
                        f"Soll {status['setpoint_pct']:g} %, Ist {status['actual_pct']:g} %"), None
            if cmd == "quittieren":
                return ("Not-Aus quittieren geht aus Sicherheitsgründen nur am Dashboard "
                        "im LAN (jemand könnte gerade an der Pumpe arbeiten)."), None
        except EmergencyActive:
            return "Abgelehnt: Not-Aus ist aktiv. Quittieren nur am Dashboard im LAN.", None
        except BackwashBusy:
            return "Abgelehnt: Rückspülung läuft. Erst abwarten.", None
        return "?", None

    async def _run_confirmed(self, action: str) -> str:
        try:
            if action == "backwash":
                self.pump.start_backwash()
                return "Rückspülung gestartet. Du bekommst eine Meldung, wenn sie fertig ist."
            if action == "shutdown":
                self.pump.emergency_stop()
                return ("Abgeschaltet und verriegelt. Pumpe ist vom Netz getrennt.\n"
                        "Wieder einschalten: Dashboard im LAN → Quittieren.")
        except EmergencyActive:
            return "Abgelehnt: Not-Aus ist aktiv."
        except BackwashBusy:
            return "Abgelehnt: Rückspülung läuft bereits."
        except ValueError as e:
            return f"Abgelehnt: {e}"
        return "Unbekannte Aktion."

    def _duration_text(self) -> str:
        m = self.cfg["manual_duration_min"]
        return f"{m} Minuten" if m else "Zeitlimit: keins"

    def _after_pump_command(self, pct: int, minutes: int) -> str:
        state = self.pump.get_state()
        if state["mode"] == "frost" and state["speed_pct"] != pct:
            return (f"Frostschutz aktiv — Pumpe bleibt auf {state['speed_pct']} %.\n"
                    f"{self._pump_line()}")
        what = "Pumpe aus" if pct == 0 else f"Pumpe auf {pct} %"
        return f"{what}, {self._duration_note(minutes)}.\n{self._pump_line()}"

    # ── Status ────────────────────────────────────────────────

    def _pump_line(self) -> str:
        s = self.pump.pump_status_summary()
        mode = MODE_NAMES.get(s["mode"], s["mode"])
        if s["mode"] == "schedule" and s.get("schedule_entry"):
            mode += f" „{s['schedule_entry']}“"
        return f"Pumpe: Soll {s['setpoint_pct']:g} %, Ist {s['actual_pct']:g} % — {mode}"

    def status_text(self) -> str:
        try:
            s = self.pump.pump_status_summary()
            hw = self.hw.get_all_states()
            prot = self.pump.protection_status()
            frost, ms = prot["frost"], prot["min_speed"]
            lines = [f"🏊 Pool Marx {__version__} — {datetime.now():%d.%m. %H:%M}"]
            if self.pump.emergency_active:
                lines.append("🛑 NOT-AUS aktiv — quittieren nur am Dashboard im LAN")
            lines.append(self._pump_line())
            lines.append(f"Netz Pumpe: {'ein' if s['mains_connected'] else 'AUS'} · "
                         f"Licht: {'an' if hw['light'] else 'aus'} · "
                         f"Wasserstand: {'ok' if hw['level_ok'] else 'NIEDRIG'}")
            lines.append(f"Frostschutz: {FROST_NAMES.get(frost['state'], frost['state'])} "
                         f"(< {frost['threshold_c']} °C) · Winterbetrieb: "
                         f"{MIN_SPEED_NAMES.get(ms['state'], ms['state'])}")
            if self.pump.backwash_running:
                lines.append("Rückspülung läuft")
            if s["dac_write_errors"]:
                lines.append(f"DAC-Schreibfehler seit Start: {s['dac_write_errors']}")
            mute = self._mute_text()
            if mute:
                lines.append(mute)
            elif self.cfg["notify_state_changes"]:
                lines.append("Statusmeldungen: an (ohne Ton) — /stumm zum Abschalten")
            if hw.get("simulation"):
                lines.append("(Simulationsmodus)")
            return "\n".join(lines)
        except Exception as e:   # Status darf nie den Bot stoppen
            logger.error(f"Telegram-Status fehlgeschlagen: {type(e).__name__}: {e}")
            return "Status nicht verfügbar (Fehler im Log)."

    async def _set_commands(self):
        commands = [
            ("status", "Aktueller Zustand"), ("pumpe", "Drehzahl in %, z. B. /pumpe 60"),
            ("aus", "Pumpe aus"), ("zeitplan", "Zurück zum Zeitplan"),
            ("licht", "/licht an oder /licht aus"), ("rueckspuelen", "Rückspülung starten"),
            ("abschalten", "Alles abschalten und verriegeln"),
            ("notiz", "Pumpenfehler notieren"),
            ("stumm", "Statusmeldungen aus, z. B. /stumm 3 (Stunden)"),
            ("laut", "Statusmeldungen wieder an"), ("hilfe", "Alle Befehle"),
        ]
        if not self.cfg["commands_enabled"]:
            commands = [c for c in commands if c[0] in ("status", "stumm", "laut", "hilfe")]
        try:
            await self._api("setMyCommands", {"commands": [
                {"command": c, "description": d} for c, d in commands]}, None)
        except TelegramApiError as e:
            logger.info(f"Telegram-Befehlsmenü nicht gesetzt: {e}")
