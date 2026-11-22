"""
Pool Marx — Strukturierte Ereignisse

Ereignisse werden als normale Log-Zeilen geschrieben (Datei/Konsole wie bisher)
und tragen zusätzlich Attribute im LogRecord (``extra``). Der OpenTelemetry-
LoggingHandler (services/telemetry.py) übernimmt diese Attribute 1:1 als
Log-Attribute → in Elastic filterbar über ``event.name``.

Regeln:
- Nur einfache Werte (str/int/float/bool). None wird weggelassen.
- Keine Secrets/Zugangsdaten als Attribut (Schlüsselnamen werden geprüft).
- Freitext (z. B. Fehlernotiz) wird gekürzt und von Zeilenumbrüchen befreit.
"""

import logging
import re

# Schlüssel, die nie als Attribut auftauchen dürfen
_SECRET_KEY_RE = re.compile(r"(passw|secret|token|api[_-]?key|authorization|credential)", re.I)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")

MAX_TEXT_LEN = 500


def sanitize_text(text, max_len: int = MAX_TEXT_LEN) -> str:
    """Freitext: Steuerzeichen/Zeilenumbrüche → Leerzeichen, Länge begrenzen."""
    if text is None:
        return ""
    text = _CTRL_RE.sub(" ", str(text)).strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def event_extra(name: str, **fields) -> dict:
    """``extra``-Dict für logger.*(): event.name + bereinigte Attribute."""
    extra = {"event.name": name}
    for key, value in fields.items():
        if value is None or _SECRET_KEY_RE.search(key):
            continue
        if isinstance(value, float):
            value = round(value, 3)
        elif not isinstance(value, (str, int, bool)):
            value = str(value)
        if isinstance(value, str):
            value = sanitize_text(value)
        extra[key] = value
    return extra


def log_event(logger: logging.Logger, level: int, name: str, message: str, **fields):
    """Ereignis loggen. Wirft nie (Logging darf die Steuerung nicht stören)."""
    try:
        logger.log(level, message, extra=event_extra(name, **fields))
    except Exception:   # pragma: no cover — reine Absicherung
        pass
