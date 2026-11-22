# Pool Marx — Roadmap Sicherheit & Robustheit

Ergebnis des Sicherheitsreviews vom 2026-09-28 (Aufbau, Code, Web-Dashboard).
Aktualisiert am 2026-11-22 (E-04-Diagnose, DAC-Rampe, Mindestdrehzahl, Telemetrie, Tests).
Legende: 🔴 kritisch · 🟠 hoch · 🟡 mittel · 🔵 Vorgabe für spätere Phasen
Bereich: **[Code]** Software · **[Pi]** Konfiguration auf dem Raspberry Pi · **[HW]** Hardware/Elektrik (Elektriker)

---

## Stufe 1 — Vor unbeaufsichtigtem Dauerbetrieb

> Code-Punkte umgesetzt am 2026-09-28. Neu: Passwort `api.secret_key` in der config.yaml auf dem Pi ist Pflicht (Platzhalter = Web-Steuerung gesperrt).

### 🔴 1. Software-Not-Aus ist keine Wartungsfreischaltung [HW] [Code]
- Relais 1 ist softwaregesteuert, kann kleben, und jeder im LAN kann per
  „Wiederherstellen“ (ohne Rückfrage) die 230V zurückschalten.
- [ ] Abschließbarer Reparatur-/Hauptschalter vor der Pumpe (Elektriker)
- [x] Web-Button „NOT-AUS“ umbenennen in „Pumpe abschalten“ (kein Not-Aus im Sinne der Norm)
- [x] „Wiederherstellen“ nur mit Bestätigungsdialog

### 🔴 2. Not-Aus ist nicht verriegelt [Code]
`hardware.emergency_stop()` schaltet nur die Hardware ab, `pump_service` läuft weiter:
- Zeitplan setzt nach ≤60s wieder DAC-Spannung → Pumpe läuft bei „Wiederherstellen“ sofort an
- laufende/geplante Rückspülung öffnet trotzdem das Magnetventil, `finally` schaltet die Pumpe wieder ein
- Licht/Ventile/Dosierung per API/MQTT sofort wieder schaltbar
- Dashboard zeigt weiter den alten Pumpenmodus
- [x] Latched Zustand `emergency` in `pump_service`: blockiert Zeitplan, Frostschutz, Rückspülung, manuelle Befehle, API- und MQTT-Schaltbefehle
- [x] Laufende Rückspülung abbrechen (Task canceln, Ventil zu, Pumpe aus)
- [x] Explizite Quittierung (API + Dashboard); danach Pumpe AUS, Modus manuell wählen
- [x] Dashboard zeigt Not-Aus-Zustand deutlich an

### 🔴 3. Relais ziehen beim Booten an [Pi]
GPIO 17, 22–25, 27 haben beim Boot Pull-down → active-low Relaisboard schaltet
ein, bis der Dienst läuft (~20–40s; nach jedem Boot/Stromausfall).
- [ ] Test: Pi neu starten, Relais-LEDs beobachten
- [ ] `/boot/firmware/config.txt`: `gpio=17,22-25,27=op,dh`

### 🔴 4. API ohne Authentifizierung, CORS `*` [Code]
- Jedes Gerät im WLAN kann alles steuern; jede im LAN geöffnete Webseite kann
  per JavaScript z.B. `POST /api/pump/speed/100` auslösen (einfache POSTs
  ohne Body brauchen keinen CORS-Preflight).
- `api.secret_key` wird nicht verwendet, `/docs` (Swagger) ist offen.
- [x] CORS auf eigene Origin beschränken
- [x] Token-/Passwort-Login für alle schreibenden Routen und `/ws`
- [x] Origin-Prüfung im WebSocket
- [x] `/docs` und `/redoc` deaktivieren

### 🔴 5. GND-Verteilung über N/PE-Sammelklemmen klären [HW]
- 0V der Kleinspannung auf **N** → lebensgefährlich, sofort trennen
- 0V auf **PE** → als PELV zulässig, aber bewusst entscheiden und dokumentieren
- [ ] Eigene, andersfarbige und beschriftete Klemmen für 0V DC (blau/grün-gelb nur für N/PE)
- [ ] Mit Elektriker klären

### 🟠 6. Relaisboard schaltet 230V [HW]
- Günstige Boards: oft zu geringe Luft-/Kriechstrecken, nicht für Motor-
  oder LED-Treiber-Einschaltströme ausgelegt (Kontakte kleben).
- [ ] Relaisboard nur noch als Ansteuerung für Hutschienen-Koppelrelais/Schütze
- [ ] JD-VCC-Jumper prüfen: 5V-Board an 3,3V-GPIO fällt evtl. nicht sauber ab

### 🟠 7. Hängende Software → Ausgänge bleiben, kein Neustart [Code] [Pi]
- Stirbt `pump_service`, läuft der Prozess weiter (Uvicorn-Thread hält ihn am
  Leben): API antwortet, Zeitplan/Frostschutz sind tot, systemd startet nicht neu.
- [x] Uvicorn im selben Event-Loop betreiben (`uvicorn.Server(...).serve()` statt `asyncio.to_thread`)
- [x] Absturz eines Hintergrund-Loops beendet den Prozess → systemd-Neustart
- [x] Heartbeat + `sd_notify` im Code (`main.py`)
- [x] DAC-Thread stirbt nie (Fehler werden gezählt), Telemetrie-Export läuft nur in eigenen Threads und kann die Steuerung nicht blockieren
- [ ] systemd-Unit: `WatchdogSec=60` ergänzen [Pi]
- [ ] Hardware-Watchdog des Pi: `RuntimeWatchdogSec=15` in `/etc/systemd/system.conf` [Pi]

### 🟠 8. Frostschutz-Crash bei Sensorfehler [Code]
- Liefert der Sensor bei aktivem Frostschutz `None`, wirft `f"{temp:.1f}"`
  einen `TypeError` (`pump_service.py` `_frost_loop`) → gesamter Pump-Service stirbt.
- Sensorfehler gilt als „warm“; fehlender Sensor liefert still 15 °C.
- [x] `None` sauber behandeln
- [x] Sensorfehler: aktiver Frostschutz wird nie beendet, Warnung im Dashboard (`sensor_error`)
- [x] Mindestdrehzahl in der Frostsaison: `pump.min_speed` mit `mode: winter` fährt in den Monaten 11–3 nachts 20 % statt 0 % (2026-11-22)
- [ ] Offen: „winter“ zusätzlich über die Außentemperatur aktivieren (z. B. < 5 °C), sobald der DS18B20 verbaut ist (TODO in `pump_service._min_speed_pct`; der Simulationswert 15,0 darf das nie auslösen)
- [x] Frostschutz mit Hysterese (0,5 °C) und monotoner Mindestlaufzeit, kein Flattern, korrekt über die Zeitumstellung
- [x] Simulationswert nur im Simulationsmodus

### 🟠 9. Frostschutz lässt sich aushebeln [Code]
- Manuelles Aus/Drehzahl während Frostschutz → `frost_active` bleibt `True`,
  Frostschutz greift nicht mehr ein.
- `frost.enabled` und `backwash.auto_enabled` werden ignoriert.
- [x] Frostschutz hat Vorrang vor manuellen Befehlen (oder mindestens Mindestdrehzahl)
- [x] `enabled`-Flags auswerten

### 🟠 10. Trockenlauf / Rückspülung ohne Prüfungen [Code]
- Schwimmerschalter wird nie ausgewertet.
- Rückspülung läuft auch bei niedrigem Pegel/nachts, kann doppelt starten
  (Button + MQTT + Zeitplan).
- MQTT `backwash` blockiert die MQTT-Schleife 3,5 Min (auch „off“ kommt nicht an).
- [x] Lock gegen parallele Rückspülungen
- [x] Rückspülung nur bei Pegel OK
- [x] Rückspülung per MQTT als Task starten
- [ ] Trockenlaufschutz, sobald Schwimmerschalter verbaut ist

---

## Stufe 2 — Aufräumen & Härtung

### 🟡 Port 80
- `setcap` auf `readlink -f venv/bin/python3` trifft `/usr/bin/python3.x` →
  jedes Python-Skript darf Port 80 binden.
- [ ] Stattdessen `AmbientCapabilities=CAP_NET_BIND_SERVICE` in `pool_marx.service` — oder bei 8080 bleiben

### 🟡 Secrets
- Secrets standen früher im Klartext in Doku und Config.
- [x] Passwort aus `PROJECT_DESCRIPTION.md` entfernen
- [ ] Zugangsdaten rotieren, die früher im Klartext lagen [Pi]
- [x] Secrets aus `config.yaml` entfernt → Umgebungsvariablen `POOL_API_SECRET`, `POOL_MQTT_PASSWORD` (systemd `EnvironmentFile=/etc/pool_marx/secrets.env`, Vorlagen in `deploy/`)
- [ ] Auf dem Pi: `secrets.env` anlegen, Secrets aus der dortigen `config.yaml` löschen [Pi]
- [x] Telemetrie-Zugangsdaten nur in `/etc/pool_marx/telemetry.env` (`EnvironmentFile=-`), nie in `config.yaml` oder Logs (per Test geprüft)
- [x] ~~`chmod 600 config/config.yaml`~~ — entfällt, `config.yaml` enthält keine Secrets mehr
- [ ] Mosquitto: nur `127.0.0.1`, kein anonymer Zugriff

### 🟡 Dienst-Neustart schaltet 230V
- `cleanup()` ruft `emergency_stop()` → jeder Restart/Deploy öffnet und schließt das Pumpenrelais.
- Derzeit ohne Wirkung: Die Pumpe ist (Stand 2026-11-22) **nicht** am Pumpenrelais angeschlossen.
- [ ] Vor dem Anschließen entscheiden: beim normalen Shutdown nur DAC auf 0 und Verbraucher aus, Pumpenrelais geschlossen lassen. Dazu gehören: `GPIO.cleanup()` nicht für den Pumpenrelais-Pin, Pumpenrelais beim Start direkt mit „ein“ initialisieren.

### 🟡 MCP4725 Power-on-Wert
- DAC lädt nach Stromausfall seinen EEPROM-Wert, bevor die Software läuft.
- [x] Einmal-Skript `scripts/mcp4725_set_default_zero.py` (mit `--check` zum Auslesen)
- [ ] Auf dem Pi ausführen und nach Stromunterbrechung 0 V messen [Pi] ([abnahme.md](docs/abnahme.md) C2)

### 🟡 Inkonsistente Zustände
- Nach Rückspülung im Nachtmodus: Pumpe läuft mit 40 %, Status „off“.
- MQTT `speed:500` → Status 500 % (Hardware klemmt auf 100 %).
- I²C-Schreibfehler wird nur geloggt, Status zeigt trotzdem den Sollwert.
- [x] State nach Rückspülung korrekt setzen
- [x] Eingaben im Service klemmen; API lehnt ungültige Drehzahlen mit 422 ab
- [x] DAC-Fehler an Status/Dashboard melden (1 Retry, Fehlerzähler in `get_all_states()`, Anzeige im Dashboard, Metrik `pool.dac.write_errors`)

### 🟡 MQTT-Status wird nie publiziert
- `on_state_change` ist async, wird in `_update_state` aber nicht awaited.
- [x] Korrekt schedulen (nach Umstellung auf gemeinsamen Event-Loop trivial)

### 🟡 HTTPS
- Passwort und Session-Cookie gehen im WLAN derzeit unverschlüsselt über HTTP.
- [ ] Variante wählen: Caddy mit `tls internal` (nur LAN) oder Tailscale (LAN + unterwegs, gültiges Zertifikat)
- [ ] Danach Cookie mit `Secure`-Flag
- [ ] Niemals Port-Forwarding am Router für das Dashboard

### 🟡 0-10V-Ausgang schützen [HW]
- [ ] Serienwiderstand (~100 Ω) + TVS-Diode am Ausgang zur Pumpe
- [ ] Laut Speck-Handbuch prüfen, ob der Steuereingang galvanisch getrennt ist

### 🟠 Pumpenfehler E-04 bei 0-10V-Steuerung [Code] [Pi] [HW]
Die Pumpe schaltet sporadisch mit E-04 ab (Überstrom im Power-Modul, Software-
Ebene) und läuft erst nach einer Netztrennung wieder. Das passiert nur bei
Steuerung über 0–10 V, nicht bei Tastenbedienung. Verdacht: abrupte
Drehzahlsprünge, Start/Stopp durch den Zeitplan, Rauschen oder ein DAC-Reset
auf den EEPROM-Startwert (≈ 50 %).
- [x] Sanfte Rampe (5 %/s) im Hintergrundthread, DAC-Refresh alle 0,2 s (2026-11-22)
- [x] `max_output_v` 9,5 V als Abstand zu 10 V
- [x] Mindestdrehzahl im Winter statt nächtlichem Stopp
- [x] Jede Sollwertänderung und jeder Zeitplanwechsel im Log (alt → neu, Grund)
- [x] Fehlernotiz per Dashboard-Button bzw. `POST /api/pump/fault-note`
- [x] Telemetrie (OpenTelemetry → Elastic) inkl. Pi-Unterspannung, vorbereitet ([telemetry.md](docs/telemetry.md))
- [ ] Neue Software und `config.yaml`-Ergänzungen auf dem Pi einspielen [Pi]
- [ ] EEPROM des MCP4725 auf 0 V (siehe oben) [Pi]
- [ ] A/B-Test: 3 Tage konstant 40 %, dann 3 Tage Zeitplan mit Rampe ([abnahme.md](docs/abnahme.md) C5)
- [ ] Oszilloskop/Logger am 0–10-V-Ausgang: Rauschen, Überschwinger ([abnahme.md](docs/abnahme.md) C1)
- [ ] Entscheiden, ob `max_output_v` höher soll: Mit 9,5 V zeigt die Pumpe ca. 6 % weniger als den Sollwert (100 % → ≈ 93 %)

### 🟡 Telemetrie & Diagnose [Pi]
- [ ] Endpunkt und API-Key in Elastic anlegen, `/etc/pool_marx/telemetry.env`, `telemetry.enabled: true`
- [ ] Optional Collector `otelcol-pi` (persistente Queue bei Internetausfall)
- [ ] Alarmregeln in Elastic: keine Daten > 10 Min, ERROR-Logs, DAC-Fehler, Fehlernotiz, Unterspannung
- [ ] Auf den neuen Log-Handler aus `opentelemetry-instrumentation-logging` umstellen (der SDK-Handler ist ab 1.45 als veraltet markiert, Versionen daher gepinnt) [Code]

### 🟡 Automatische Tests [Code]
- [x] pytest-Suite im Simulationsmodus: Stufe A (Unit) und B (Dauerlauf, Fehlerinjektion, Last), Abdeckung `services/` > 80 %
- [x] Abnahme-Checkliste Stufe C für die echte Hardware ([abnahme.md](docs/abnahme.md))
- [ ] Git-Repo + CI, die `pytest -q` bei jeder Änderung ausführt

---

## Stufe 3 — Vorgaben für Phase 3 (Nachspeisung, Dosierung)

### 🔵 Dosierung
- [ ] Hardware-Verriegelung: Dosierpumpen nur mit Strom, wenn Filterpumpe läuft **und** Strömungswächter Durchfluss meldet
- [ ] Chlor und pH-Minus nie gleichzeitig / nie ohne Durchfluss (**Chlorgas-Gefahr**)
- [ ] Zusätzliche Hardware-Zeitbegrenzung pro Dosierzyklus

### 🔵 Nachspeisung
- [ ] `max_fill_minutes` zusätzlich als Hardware-Zeitrelais (Wasserschaden bei hängender Software)
- [ ] Drahtbruchverhalten des Schwimmerschalters bewusst festlegen

### 🔵 Elektrik / Abnahme
- [ ] DIN VDE 0100-702: RCD 30 mA, Potentialausgleich, Schaltschrank außerhalb Bereich 0/1
- [ ] Beleuchtung: EVA-Treiber ist SELV — Installation der Leuchte gem. Bereichen prüfen
- [ ] Elektriker-Abnahme vor Dauerbetrieb

---

## Nicht betroffen
- Web-Dashboard: kein XSS (nur statische Strings in `innerHTML`), keine externen CDNs.
