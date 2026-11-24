## Projektkontext

Ich baue "Projekt Marx" — eine Raspberry-Pi-Automatisierung für einen
Außenpool (ca. 42 m³).
Aktueller Stand: Pumpensteuerung ist hardwareseitig fertig und läuft im
Testbetrieb. Temperatursensor, pH/ORP, Schwimmerschalter, Kabeldurchführungen
und der Elektriker-Abnahmetermin sind bewusst noch offen (Phase 2/3).
Die Pumpe ist derzeit **nicht** am Pumpenrelais angeschlossen (Stand 2026-11-22).

## Hardware (bereits fertig verbaut)

- Raspberry Pi 4 (2GB), Raspberry Pi OS Lite 64-bit, Python 3.13 in venv
- MCP4725 12-Bit DAC (I²C, Adresse 0x60) erzeugt 0-3,3V (VDD 3,3 V, nicht 5 V)
- LM358 Non-Inverting-Verstärker (Gain 3,2× mit R1=1kΩ, R2=2,2kΩ, 12 V
  Versorgung) skaliert das auf 0-10V für die Pumpe. Schaltung: MCP-Ausgang →
  Pin 3 (IN+) direkt; R1 zwischen Pin 2 (IN-) und GND; R2 als Feedback Pin1→Pin2.
  Gemessen: 4095 → 10,483 V, Offset −0,021 V. Die Software begrenzt 100 % auf 9,5 V.
- Speck BADU Alpha ECO Soft Pumpe, analog per 0-10V angesteuert (Pumpen-
  interne "Steuerart" auf Modus 3 = 0-10V gestellt, nicht Werkseinstellung!)
- 8-Kanal-Relaisboard (Yizhet, active-low, Optokoppler) — ein Kanal ist als
  Not-Aus/Wartungsschalter für die 230V-Pumpenversorgung reserviert.
  **Wichtige Design-Entscheidung: Die Pumpe wird NICHT über das Relais
  ein/ausgeschaltet.** Der Hersteller rät explizit von häufigem 230V-Zyklen
  ab (Kondensatorbelastung). Ein/Aus/Drehzahl läuft ausschließlich über das
  0-10V-Signal (0V = Stopp, verifiziert per Hörtest). Das Relais bleibt im
  Normalbetrieb dauerhaft geschlossen und wird nur bei explizitem
  Not-Aus/Wartung geöffnet (`pump_mains_cutoff()` / `pump_mains_restore()`).
  Derzeit ist die Pumpe noch nicht an diesem Relais angeschlossen.
- Alles fest montiert auf Hutschiene im IP65-Schaltschrank (ARLI-Gehäuse):
  Pi, LM358-Platine, MCP4725, Relaisboard, MEAN WELL MDR-10-12 Netzteil.
  Zentrale GND-Verteilung über N/PE-Sammelklemmen.
- PTC-Heizelement + f&f RT-820-Thermostat für Frostschutz **im Schaltschrank**
  (nicht zu verwechseln mit der Software-Frostschutzfunktion der Pumpe).

## Software-Architektur

`pool_controller/` (auf dem Pi unter `/opt/pool_marx/pool_controller/`):
- `main.py` — Einstiegspunkt, Logging- und Telemetrie-Setup
- `api/main.py` — FastAPI-App (Port 8080), REST-Endpunkte + WebSocket
- `services/hardware.py` — GPIO/DAC Hardware Abstraction Layer. Pumpenbefehle
  setzen nur einen Sollwert; ein Daemon-Thread fährt alle 0,2 s mit max. 5 %/s
  dorthin und schreibt den DAC jedes Mal neu (Refresh gegen DAC-Reset).
  Not-Aus/Shutdown setzen 0 V sofort.
- `services/pump_service.py` — Zeitplan (mit Mindestdrehzahl im Winter),
  Frostschutz, manuelle Steuerung, Rückspülung (wartet auf den Istwert),
  Fehlernotiz für E-04
- `services/telemetry.py` — optional: OpenTelemetry-Logs und -Metriken per
  OTLP/HTTP (Ziel Elastic), standardmäßig aus
- `services/events.py` — strukturierte Log-Ereignisse (`event.name`)
- `web/index.html` — Dashboard (inkl. Istwert und Button „Pumpenfehler notieren“)
- `pi/` — alles, was auf den Pi kommt: systemd-/env-Vorlagen, `install-release.sh`,
  `mcp4725_set_default_zero.py` (einmalig: EEPROM-Startwert 0 V),
  `otelcol-pi.yaml`/`.service` (optionaler Collector)
- `release/` — läuft auf dem Mac: `release.py` (Release erstellen), `deploy.sh` (auf den Pi bringen)
- `tests/` — pytest-Suite im Simulationsmodus (Stufe A/B), `pytest -q`
- `docs/` — Inbetriebnahme, Telemetrie, Abnahme (Stufe C); das Netzwerkkonzept
  liegt nur lokal (`docs/NETZWERK_SICHERHEIT.md`, nicht im öffentlichen Repo)
- `services/mqtt_service.py` — MQTT/Home-Assistant-Discovery (Home Assistant
  wurde als Dashboard verworfen — FFmpeg/Bluetooth-Fehler, nicht behebbar
  ohne unverhältnismäßigen Aufwand; die MQTT-Reconnect-Fehler im Log alle
  ~10-15s sind bekannt und harmlos, solange der Broker nicht zwingend
  gebraucht wird)
- `config/config.yaml` — GPIO-Pin-Belegung, Pumpen-Drehzahlstufen, Zeitplan,
  Frostschutz-Parameter, MQTT-Broker (127.0.0.1). **Keine Secrets** —
  MQTT-Passwort und Dashboard-Passwort kommen aus Umgebungsvariablen
  (`POOL_MQTT_PASSWORD`, `POOL_API_SECRET`), auf dem Pi per systemd aus
  `/etc/pool_marx/secrets.env` (Vorlage: `pi/secrets.env.example`).
  Telemetrie-Zugangsdaten analog in `/etc/pool_marx/telemetry.env`
  (Vorlage: `pi/telemetry.env.example`, optional).
  Neue Abschnitte: `dac`, `pump.min_speed`, `telemetry`.
- Deployed als systemd-Service `pool_marx.service` (Restart=always),
  Ergänzung in `pi/pool_marx.override.conf`

Läuft aktuell stabil im Simulationsmodus getestet und im Realbetrieb
verifiziert (Pumpe reagiert hörbar auf `speed_pct`-Kommandos via REST API).

## Kürzlich behobene Bugs (bereits im aktuellen Code umgesetzt)

1. **Relais-Kopplung**: `pump_on()`/`pump_off()`/`pump_set_speed()` in
   `hardware.py` haben früher bei JEDEM Aufruf das 230V-Relais mitgeschaltet
   — widersprach der Design-Entscheidung oben. Fix: Diese Methoden fassen
   nur noch das DAC/0-10V-Signal an. Neu: `pump_mains_cutoff()` /
   `pump_mains_restore()` für expliziten Not-Aus/Wartungszugriff aufs
   Relais, genutzt von `emergency_stop()`. Neue API-Routen:
   `POST /api/pump/mains/restore`, `GET /api/pump/mains`.
2. **Frostschutz-Mindestlaufzeit**: `run_minutes: 10` aus der Config wurde
   im Code nie ausgewertet — die Pumpe hätte beim ersten "warmen" Messwert
   sofort wieder abgeschaltet. Fix in `pump_service.py`: `_frost_loop`
   trackt `_frost_activated_at` und deaktiviert erst, wenn zusätzlich zur
   Temperaturbedingung die Mindestlaufzeit erreicht ist. Seit 2026-11-22
   zusätzlich mit 0,5 °C Hysterese und monotoner Uhr.

## Offenes Problem: Pumpenfehler E-04 (Maßnahmen vom 2026-11-22)

Die Pumpe schaltet sporadisch mit E-04 ab (Power-Modul-Überstrom,
Software-Ebene) und läuft erst nach einer Netztrennung wieder. Das passiert nur
bei Steuerung über 0-10V, nicht bei Tastenbedienung. Betrieb meist 30-60 %.
Verdacht: abrupte Drehzahlsprünge, Start/Stopp (Zeitplan setzte nachts 0 %),
Rauschen oder ein DAC-Reset auf den EEPROM-Startwert (≈ 50 %).

Umgesetzt in der Software:
- Rampe 5 %/s und DAC-Refresh alle 0,2 s (`dac.ramp_pct_per_s`, `dac.refresh_interval_s`)
- 100 % = 9,5 V (`dac.max_output_v`). Folge: Die Pumpe zeigt ca. 6 % weniger
  als den Sollwert, weil sie erst bei ≈ 10,13 V 100 % anzeigt.
- Mindestdrehzahl 20 % statt 0 % in den Monaten 11–3 (`pump.min_speed`)
- Jede Sollwertänderung und jeder Zeitplanwechsel mit Grund im Log
- Fehlernotiz (`POST /api/pump/fault-note`, Dashboard-Button)
- Telemetrie nach Elastic vorbereitet, inkl. Pi-Unterspannung

Noch offen: auf dem Pi einspielen, EEPROM auf 0 V setzen, A/B-Test
(`docs/abnahme.md` C5).

**Bekannter, noch nicht behobener Punkt:** `_read_temperature()` liest einen
DS18B20 über `/sys/bus/w1/devices/28-*/w1_slave`. Da der Sensor noch nicht
verbaut ist, liefert die Funktion auf dem Pi keinen Wert (im Simulationsmodus
`15.0`). Die Software-Frostschutzfunktion ist also wirkungslos, bis der Sensor
angeschlossen und 1-Wire aktiviert ist. Bis dahin schützen das PTC-Heizelement
im Schrank und, für das Wasser, die Mindestdrehzahl im Winter. Sobald der
Sensor da ist, soll „winter“ zusätzlich über die Außentemperatur aktivierbar
werden (TODO im Code).

## Deployment-Workflow

Kein Git-Remote/SSH-Zugriff vom Chat aus möglich — Code wird bisher manuell
als tar.gz übertragen und per `scp` auf den Pi kopiert. Pi erreichbar unter
`<pi-hostname>.local` oder `<pi-ip>` (mDNS teils unzuverlässig, dann IP
nutzen), User `pi`. Nach jeder Änderung:
```bash
scp main.py requirements.txt pi@<pi-ip>:/opt/pool_marx/pool_controller/
scp -r api services web pi pi@<pi-ip>:/opt/pool_marx/pool_controller/
ssh pi@<pi-ip> "sudo systemctl restart pool_marx"
```
Bei geänderter `requirements.txt` vorher `venv/bin/pip install -r requirements.txt`
auf dem Pi. **`config/config.yaml` NICHT überschreiben** — enthält die echten
Einstellungen (u. a. MQTT-Benutzer, Kalibrierung). Neue Abschnitte von Hand ergänzen.
Ausführlich: `docs/INBETRIEBNAHME.md`, Kapitel 11.

Empfehlenswert für die VS-Code-Phase: Git-Repo initialisieren und per
`git pull` auf dem Pi statt manuellem Kopieren deployen — sauberer als der
bisherige Ad-hoc-Transfer.

## Was ich von dir brauche

Ich entwickle ab jetzt hier in VS Code weiter. Bitte den Code in diesem
Workspace als Ausgangspunkt nehmen (aktueller Stand, s.o.) und mir bei der
Weiterentwicklung helfen — als Nächstes vermutlich: E-04-A/B-Test auswerten,
DS18B20-Integration (inkl. Außentemperatur-Schwelle für die Mindestdrehzahl),
danach pH/ORP (EZO-Sensoren) und Schwimmerschalter, jeweils inkl. Anpassung
von `hardware.py`/`pump_service.py`/`config.yaml` und der Tests in `tests/`.
