# Changelog

Alle nennenswerten Änderungen an Pool Marx. Format nach
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionierung nach
[Semantic Versioning](https://semver.org/lang/de/):

- **MAJOR**: inkompatible Änderung (z. B. API-Routen/MQTT-Topics entfernt oder umbenannt, Config muss umgebaut werden)
- **MINOR**: neue Funktion, rückwärtskompatibel (neue Config-Abschnitte mit Standardwerten zählen dazu)
- **PATCH**: Fehlerbehebung ohne neue Funktion

Neue Einträge kommen unter **[Unreleased]**. `scripts/release.py` macht daraus beim Release die neue Version.

## [Unreleased]

### Added
- `RELEASE.md`: Ablauf für Versionierung, Release und Deploy
- `release/deploy.sh` + `pi/install-release.sh`: Deploy getaggter Versionen auf den Pi mit eigenem
  Verzeichnis und venv pro Version, gemeinsamer Config, Health-Check, automatischem Rollback,
  `--rollback`, `--list` und einmaliger Migration (`--migrate`)

### Changed
- Verzeichnisse nach Einsatzort benannt: `deploy/` + Pi-Teile aus `scripts/` → **`pi/`**
  (alles, was auf den Pi kommt), restliche `scripts/` → **`release/`** (läuft auf dem Mac).
  Auf dem Pi z. B. jetzt `venv/bin/python pi/mcp4725_set_default_zero.py`.

## [1.1.0] - 2026-11-24

### Added
- DAC-Ausgabe über Hintergrundthread: Rampe 5 %/s, Refresh alle 0,2 s (gegen Drehzahlsprünge und DAC-Reset)
- Mindestdrehzahl statt 0 % im Winter (`pump.min_speed`)
- Fehlernotiz für Pumpenfehler (E-04): `POST /api/pump/fault-note` und Dashboard-Button
- Log jeder Sollwertänderung und jedes Zeitplanwechsels mit Grund
- Dashboard: Istwert, DAC-Fehler, Kacheln für Frostschutz und Winterbetrieb, Versionsanzeige
- Optionale Telemetrie per OpenTelemetry (Logs + Metriken, OTLP/HTTP), Collector-Konfiguration für den Pi
- Einmal-Skript `scripts/mcp4725_set_default_zero.py` (EEPROM-Startwert 0 V)
- pytest-Suite (Stufe A/B), Abnahme-Checkliste (Stufe C)
- Versionsnummer in API (`/api/health`, `/api/status`), Dashboard, Log, MQTT und Telemetrie

### Changed
- 0–100 % entspricht jetzt 0–`dac.max_output_v` (9,5 V), gerechnet mit der gemessenen Kalibrierung. Die Pumpe zeigt dadurch ca. 6 % weniger als den Sollwert.
- Rückspülung wartet auf den Istwert der Pumpe statt fester 3 s und bricht bei Timeout ab
- Frostschutz mit 0,5 °C Hysterese und monotoner Mindestlaufzeit
- API lehnt ungültige Drehzahlen mit 422 ab (statt still zu klemmen)

### Fixed
- `POST /api/pump/on` mit `speed_pct: 0` startete mit 60 %

## [1.0.0] - 2026-09-29

Ausgangsstand vor Einführung von Git (nicht getaggt): Zeitplan, Frostschutz,
Rückspülung, verriegelter Not-Aus, Web-Dashboard mit Login, MQTT.

[Unreleased]: https://github.com/MarxDimitri/Pool_Controller/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/MarxDimitri/Pool_Controller/releases/tag/v1.1.0
