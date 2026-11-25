"""
Pool Marx — Dimmer für die Beleuchtung

Beide Lichtquellen hängen an derselben Schaltung: Das Relais (light_relay)
ist der Hauptschalter, der Dimmer stellt die Helligkeit ein.

Der Dimmer ist noch nicht verbaut. Bis dahin gibt es nur den Typ
"simulation": Er merkt sich die Helligkeit, schaltet aber nichts. Ein
echter Dimmer (z. B. PWM an einem GPIO oder 0–10 V über einen zweiten DAC)
kommt als weitere Klasse mit derselben Schnittstelle dazu:

    set_level(pct: int) -> None     0 = aus, sonst min_pct..100
    level: int                      zuletzt gesetzter Wert
    kind: str                       Typname für Status und Log
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DIMMER_TYPES = ("simulation",)
DIMMER_DEFAULTS = {"type": "simulation", "min_pct": 10}


@dataclass(frozen=True)
class DimmerSettings:
    type: str = "simulation"
    min_pct: int = 10


def load_dimmer_settings(config: dict) -> DimmerSettings:
    """lighting.dimmer lesen; fehlt er → Simulation, min 10 %."""
    raw = ((config or {}).get("lighting") or {}).get("dimmer") or {}
    kind = str(raw.get("type", DIMMER_DEFAULTS["type"])).lower()
    if kind not in DIMMER_TYPES:
        logger.warning(f"lighting.dimmer.type={raw.get('type')!r} unbekannt — verwende "
                       f"'simulation' (möglich: {', '.join(DIMMER_TYPES)})")
        kind = "simulation"
    min_pct = raw.get("min_pct", DIMMER_DEFAULTS["min_pct"])
    try:
        if isinstance(min_pct, bool):
            raise TypeError
        min_pct = int(min_pct)
        if not 1 <= min_pct <= 100:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning(f"lighting.dimmer.min_pct={raw.get('min_pct')!r} ungültig — "
                       f"verwende {DIMMER_DEFAULTS['min_pct']}")
        min_pct = DIMMER_DEFAULTS["min_pct"]
    return DimmerSettings(type=kind, min_pct=min_pct)


class SimulatedDimmer:
    """Merkt sich die Helligkeit, ohne Hardware anzusteuern."""

    kind = "simulation"

    def __init__(self):
        self.level = 0
        self.history: list = []   # für Tests: alle gesetzten Werte

    def set_level(self, pct: int):
        self.level = int(pct)
        self.history.append(self.level)
        logger.debug(f"Dimmer (simuliert): {self.level}%")


def create_dimmer(settings: DimmerSettings):
    # Einziger Typ bisher; echte Dimmer hier ergänzen
    return SimulatedDimmer()
