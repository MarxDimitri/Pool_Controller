# Changelog

Alle nennenswerten Änderungen an Pool Marx. Format nach
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionierung nach
[Semantic Versioning](https://semver.org/lang/de/):

- **MAJOR**: inkompatible Änderung (z. B. API-Routen/MQTT-Topics entfernt oder umbenannt, Config muss umgebaut werden)
- **MINOR**: neue Funktion, rückwärtskompatibel (neue Config-Abschnitte mit Standardwerten zählen dazu)
- **PATCH**: Fehlerbehebung ohne neue Funktion

Neue Einträge kommen unter **[Unreleased]**. `release/release.py` macht daraus beim Release die neue Version.

## [Unreleased]

### Added
- Telegram: Zustandsänderungen melden mit `telegram.notify_state_changes: true` (Standard aus).
  Pumpe % mit Grund (z. B. `Pumpe 20 → 60 % (Zeitplan „Morgenfilterung“)`), Licht an/aus,
  Rückspülung Start/Ende. Kommen **ohne Ton**.
- Telegram: `/stumm [Stunden]` schaltet alle Meldungen ohne Ton ab, `/laut` wieder an.
  Warnungen und Fehler kommen weiter. Funktioniert auch mit `commands_enabled: false`.
- Ereignis `light.changed` (nur bei echter Änderung des Lichts)

### Changed
- Telegram: Meldungen unter WARNING (z. B. Frostschutz aus, Rückspülung fertig) kommen jetzt
  ohne Ton. Die Unterdrückung gleicher Meldungen gilt nur noch für Warnungen und Fehler.
- Log: „EVA Beleuchtung: AUS“ erscheint nicht mehr bei jedem Beenden/Abschalten, wenn das Licht
  ohnehin aus war (nur noch DEBUG)

### Einmalig beim Update auf dem Pi
Optional, für Zustandsmeldungen: in der `config.yaml` auf dem Pi unter `telegram:` die Zeile
`notify_state_changes: true` ergänzen, Dienst neu starten.

## [1.3.1] - 2026-11-25

### Fixed
- `docs/telegram.md`: Befehl zum Ermitteln der Nutzer-ID scheiterte mit „Unterminated quoted string“,
  wenn `secrets.env` Werte mit Anführungszeichen enthält. Er liest jetzt nur die Token-Zeile.
- Telegram: Steht in `allowed_user_ids` ein Name statt einer Zahl (z. B. der Bot-Name), erklärt die
  Warnung jetzt, dass die eigene Nutzer-ID gemeint ist, und verweist auf die Anleitung.

## [1.3.0] - 2026-11-25

### Added
- Optionaler Telegram-Bot (`services/telegram_service.py`, Anleitung `docs/telegram.md`):
  - Meldungen aufs Handy: alles ab `notify_level` (Standard WARNING) und ausgewählte Ereignisse
    (Frostschutz aus, Rückspülung fertig, Netz wieder an). Gebündelt, höchstens 6 pro Minute,
    gleiche Meldungen höchstens alle 15 Minuten. Meldung bei Start und Beenden.
  - Befehle `/status`, `/pumpe <0–100> [Minuten]`, `/aus`, `/zeitplan`, `/licht an|aus`,
    `/rueckspuelen` und `/abschalten` (beide mit Bestätigung), `/notiz`, `/hilfe`.
    Manuelle Befehle laufen nach 120 Minuten ab, dann gilt wieder der Zeitplan.
  - Nur freigegebene Nutzer-IDs, nur private Chats, alte Befehle werden verworfen.
    Quittieren der Abschaltung geht bewusst nur am Dashboard.
  - Nur ausgehende HTTPS-Verbindung zu `api.telegram.org` (Long Polling), keine Portfreigabe.
- Neue Abhängigkeit `httpx` (bisher nur für Tests)

### Einmalig beim Update auf dem Pi
Ohne diese Schritte bleibt Telegram aus, alles andere läuft wie bisher.
`httpx` installiert der Deploy automatisch im neuen venv.
1. Bot bei @BotFather anlegen, Token als `POOL_TELEGRAM_TOKEN=` in `/etc/pool_marx/secrets.env`
2. Eigene Nutzer-ID ermitteln (`docs/telegram.md`, Abschnitt 3)
3. Abschnitt `telegram:` aus `config/config.yaml` in die `config.yaml` auf dem Pi übernehmen,
   `enabled: true` und `allowed_user_ids` setzen
4. Dienst neu starten, Startmeldung in Telegram prüfen

## [1.2.1] - 2026-11-25

### Fixed
- Dashboard meldete dauerhaft „getrennt – erneuter Versuch…“: Im frischen venv pro Version fehlte
  eine WebSocket-Bibliothek für uvicorn, `/ws` antwortete mit 404. `websockets` steht jetzt in
  `requirements.txt`; ein Test startet einen echten Server und prüft die Verbindung.

## [1.2.0] - 2026-11-24

### Added
- `RELEASE.md`: Ablauf für Versionierung, Release und Deploy
- `release/deploy.sh` + `pi/install-release.sh`: Deploy getaggter Versionen auf den Pi mit eigenem
  Verzeichnis und venv pro Version, gemeinsamer Config, Health-Check, automatischem Rollback,
  `--rollback`, `--list` und einmaliger Migration (`--migrate`)
- Kurze README in `pi/` und `release/`, die sagt, wofür das Verzeichnis da ist

### Changed
- Verzeichnisse nach Einsatzort benannt: `deploy/` + Pi-Teile aus `scripts/` → **`pi/`**
  (alles, was auf den Pi kommt), restliche `scripts/` → **`release/`** (läuft auf dem Mac).
  Auf dem Pi z. B. jetzt `venv/bin/python pi/mcp4725_set_default_zero.py`.
- Inbetriebnahme (Kapitel 11): Updates empfohlen über `release/deploy.sh`, manueller Weg nur vor der Migration

### Einmalig beim Update auf dem Pi
Keine Änderung an Steuerung, API oder `config.yaml`. Für den neuen Deploy-Ablauf
(siehe RELEASE.md, Abschnitt 5) einmalig:
1. Mac: `cp release/deploy.env.example .deploy.env` und `POOL_PI=pi@<pi-ip>` eintragen
2. Pi: `sudo apt install -y python3-venv python3-dev gcc` (jede Version baut ihr eigenes venv)
3. Mac: `release/deploy.sh --migrate`: bisheriges Verzeichnis → `releases/legacy`,
   Config → `/opt/pool_marx/shared/config/`
4. Mac: `release/deploy.sh v1.2.0`

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

[Unreleased]: https://github.com/MarxDimitri/Pool_Controller/compare/v1.3.1...HEAD
[1.3.1]: https://github.com/MarxDimitri/Pool_Controller/releases/tag/v1.3.1
[1.3.0]: https://github.com/MarxDimitri/Pool_Controller/releases/tag/v1.3.0
[1.2.1]: https://github.com/MarxDimitri/Pool_Controller/releases/tag/v1.2.1
[1.2.0]: https://github.com/MarxDimitri/Pool_Controller/releases/tag/v1.2.0
[1.1.0]: https://github.com/MarxDimitri/Pool_Controller/releases/tag/v1.1.0
