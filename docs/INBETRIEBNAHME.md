# Pool Marx — Inbetriebnahme der Software

Dieses Handbuch beschreibt, wie die Pool-Marx-Steuerung auf dem Raspberry Pi
eingerichtet, kalibriert, getestet und betrieben wird.

> ⚠️ **Sicherheit vorab**
> - Arbeiten an 230 V nur im spannungsfreien Zustand bzw. durch eine Elektrofachkraft.
> - „Alles abschalten“ im Dashboard ist eine **Software-Abschaltung**, kein Not-Aus
>   und keine Freischaltung. Vor Arbeiten an Pumpe oder Anlage immer den
>   **Reparaturschalter** öffnen und gegen Wiedereinschalten sichern.
> - Offene Sicherheitspunkte stehen in [ROADMAP.md](../ROADMAP.md) — vor dem
>   unbeaufsichtigten Dauerbetrieb Stufe 1 abarbeiten und die Elektrik abnehmen lassen.

---

## Inhalt

1. [Überblick](#1-überblick)
2. [Voraussetzungen](#2-voraussetzungen)
3. [Raspberry Pi vorbereiten](#3-raspberry-pi-vorbereiten)
4. [Software installieren](#4-software-installieren)
5. [Secrets einrichten](#5-secrets-einrichten)
6. [Konfiguration (config.yaml)](#6-konfiguration-configyaml)
7. [systemd-Dienst einrichten](#7-systemd-dienst-einrichten)
8. [Erster Start und Funktionstest](#8-erster-start-und-funktionstest)
9. [Kalibrierung des 0–10-V-Signals](#9-kalibrierung-des-010-v-signals)
10. [Bedienung](#10-bedienung)
11. [Updates einspielen](#11-updates-einspielen)
12. [Fehlersuche](#12-fehlersuche)
13. [Checkliste Inbetriebnahme](#13-checkliste-inbetriebnahme)
14. [Automatische Tests](#14-automatische-tests)

Weitere Dokumente: [telemetry.md](telemetry.md) (Logs und Metriken nach Elastic) ·
[abnahme.md](abnahme.md) (Abnahmetests an der echten Hardware) ·
`NETZWERK_SICHERHEIT.md` (Netzwerkkonzept, nur lokal, nicht im öffentlichen Repo)

---

## 1. Überblick

| Komponente | Aufgabe |
|---|---|
| `main.py` | Startet alle Dienste in einem Event-Loop, meldet sich beim systemd-Watchdog |
| `services/hardware.py` | GPIO (Relais, Schwimmerschalter) und MCP4725-DAC (0–10 V für die Pumpe) mit Rampe im Hintergrundthread |
| `services/pump_service.py` | Zeitplan mit Mindestdrehzahl, Frostschutz, Rückspülung, manuelle Steuerung, verriegelte Abschaltung, Fehlernotiz |
| `services/mqtt_service.py` | Optional: Status und Befehle über MQTT |
| `services/telemetry.py` | Optional: Logs und Metriken per OpenTelemetry (→ [telemetry.md](telemetry.md)) |
| `services/events.py` | Strukturierte Ereignisse im Log (`event.name`) für die Diagnose |
| `api/main.py` | REST-API, WebSocket für Live-Updates, Login |
| `web/index.html` | Dashboard im Browser |
| `config/config.yaml` | Einstellungen **ohne** Passwörter |
| `/etc/pool_marx/secrets.env` | Passwörter (nur auf dem Pi, nur für root lesbar) |
| `/etc/pool_marx/telemetry.env` | Optional: Endpunkt und API-Key für die Telemetrie |
| `pi/` | Alles, was auf den Pi kommt: systemd-/env-Vorlagen, Release-Installer, Einmal-Skript für den MCP4725, Collector-Konfiguration |
| `release/` | Läuft auf deinem Rechner: Release erstellen, auf den Pi bringen (→ [RELEASE.md](../RELEASE.md)) |
| `tests/` | Automatische Tests (laufen auf dem Mac, → Kapitel 14) |

**Grundprinzip der Pumpensteuerung:** Die Pumpe wird ausschließlich über das
0–10-V-Signal ein-, ausgeschaltet und in der Drehzahl geregelt (0 V = Stopp).
Das Pumpenrelais (230 V) bleibt im Normalbetrieb dauerhaft geschlossen und
wird nur bei „Alles abschalten“ geöffnet.

> **Aktueller Aufbau:** Die Pumpe ist derzeit **nicht** am Pumpenrelais
> angeschlossen. „Alles abschalten“ setzt das Signal sofort auf 0 V, die Pumpe
> steht still, **hat aber weiter Netzspannung**. Die Anzeige „Netzspannung Pumpe“
> zeigt nur den Zustand des Relais.

**Rampe und Refresh:** Befehle (Zeitplan, Dashboard, MQTT, Frostschutz) setzen
nur einen Sollwert. Ein Hintergrundthread fährt die Spannung alle 0,2 s mit
höchstens 5 %/s dorthin (0 → 60 % dauert also 12 s, 0 → 100 % 20 s) und schreibt
den DAC dabei jedes Mal neu. Das verhindert abrupte Drehzahlsprünge und setzt
einen DAC, der sich zurückgesetzt hat, sofort wieder auf den richtigen Wert.
„Alles abschalten“ und das Beenden des Dienstes setzen 0 V **sofort**, ohne Rampe.

---

## 2. Voraussetzungen

**Hardware**
- Raspberry Pi 4 mit Raspberry Pi OS Lite (64-bit)
- MCP4725-DAC an I²C (Adresse `0x60`), LM358-Verstärker (Verstärkung 3,2)
- 8-Kanal-Relaisboard (active-low) an den GPIOs laut `config.yaml`
- Speck BADU Alpha ECO Soft, **Steuerart an der Pumpe auf Modus 3 (0–10 V)** gestellt
- Multimeter für die Kalibrierung

**Netzwerk**
- Pi im Heimnetz, feste IP empfohlen (in diesem Handbuch `<pi-ip>`, durch die eigene Adresse ersetzen)
- SSH-Zugang als Benutzer `pi`

---

## 3. Raspberry Pi vorbereiten

### 3.1 I²C aktivieren und DAC prüfen

```bash
sudo raspi-config nonint do_i2c 0
sudo apt update && sudo apt install -y i2c-tools python3-venv
sudo reboot
# nach dem Neustart:
i2cdetect -y 1        # in der Tabelle muss "60" erscheinen
```

### 3.2 Relais beim Booten ausgeschaltet halten

Ohne diese Einstellung ziehen die active-low Relais beim Booten für
20–40 Sekunden an. In `/boot/firmware/config.txt` am Ende ergänzen:

```
gpio=17,22-25,27=op,dh
```

Neu starten und die Relais-LEDs beobachten: Sie dürfen während des Bootens
nicht aufleuchten.

### 3.3 Hardware-Watchdog des Pi (empfohlen)

In `/etc/systemd/system.conf` setzen:

```
RuntimeWatchdogSec=15
```

Danach `sudo reboot`. Hängt das gesamte System, startet der Pi dann
selbstständig neu.

### 3.4 Log-Verzeichnis

```bash
sudo install -d -o pi -g pi /var/log/pool_marx
```

### 3.5 Zeitsynchronisation

Zeitplan und Log-Zeitstempel hängen an der Uhrzeit. Der Pi hat keine Echtzeituhr.

```bash
timedatectl status          # "System clock synchronized: yes"
sudo timedatectl set-ntp true
```

### 3.6 MCP4725: Startwert 0 V ins EEPROM (einmalig)

Der MCP4725 lädt beim Einschalten seinen EEPROM-Wert, ab Werk meist die Mitte
(≈ 5 V am Verstärker). Bis der Dienst läuft, würde die Pumpe also mit halber
Drehzahl anlaufen. Das Skript schreibt einmalig 0 V als Startwert. Erst nach
Kapitel 4 ausführen, weil es das venv braucht. **Nicht** bei jedem Start ausführen,
das EEPROM verträgt nur begrenzt viele Schreibzyklen.

```bash
cd /opt/pool_marx/pool_controller
sudo systemctl stop pool_marx                                   # falls schon eingerichtet
venv/bin/python pi/mcp4725_set_default_zero.py --check     # nur lesen
venv/bin/python pi/mcp4725_set_default_zero.py --yes       # schreiben
sudo systemctl start pool_marx
```

Prüfen: Steuerung stromlos machen, wieder einschalten und **vor** dem Dienststart
am LM358-Ausgang messen. Soll ist 0 V, nicht ≈ 5 V (→ [abnahme.md](abnahme.md) C2).

---

## 4. Software installieren

Installationsort: `/opt/pool_marx/pool_controller`

```bash
# vom Mac aus (im Projektordner):
ssh pi@<pi-ip> "sudo install -d -o pi -g pi /opt/pool_marx/pool_controller"
scp -r main.py requirements.txt api services web config pi \
  pi@<pi-ip>:/opt/pool_marx/pool_controller/

# auf dem Pi:
cd /opt/pool_marx/pool_controller
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Test ohne Dienst (Abbruch mit Strg+C):

```bash
venv/bin/python main.py
```

Im Log muss `MCP4725 DAC initialisiert` stehen, **nicht** `Simulationsmodus aktiv`.

`requirements.txt` enthält auch die OpenTelemetry-Pakete (fest gepinnt, reines
HTTP, kein Kompilieren nötig). Sie werden nur benutzt, wenn
`telemetry.enabled: true` gesetzt ist.

---

## 5. Secrets einrichten

Passwörter stehen **nicht** in `config.yaml`, sondern als Umgebungsvariablen in
`/etc/pool_marx/secrets.env`. systemd liest die Datei als root und übergibt
die Werte an den Dienst.

```bash
cd /opt/pool_marx/pool_controller/pi
sudo install -d -m 700 /etc/pool_marx
sudo install -m 600 -o root -g root secrets.env.example /etc/pool_marx/secrets.env
sudo nano /etc/pool_marx/secrets.env
```

| Variable | Bedeutung |
|---|---|
| `POOL_API_SECRET` | Passwort für Dashboard und API, **mindestens 12 Zeichen**. Fehlt es, ist die Web-Steuerung gesperrt — die Automatik läuft trotzdem. |
| `POOL_MQTT_PASSWORD` | Passwort für den MQTT-Broker (nur wenn MQTT genutzt wird) |
| `POOL_MQTT_USERNAME` | optional, überschreibt `mqtt.username` |

Format: `KEY=wert`, ohne Anführungszeichen und ohne Leerzeichen um `=`.

### 5.1 Telemetrie-Zugangsdaten (optional)

Nur nötig, wenn Logs und Metriken nach Elastic gehen sollen (→ [telemetry.md](telemetry.md)).
Fehlt die Datei, startet der Dienst trotzdem.

```bash
cd /opt/pool_marx/pool_controller/pi
sudo install -m 600 -o root -g root telemetry.env.example /etc/pool_marx/telemetry.env
sudo nano /etc/pool_marx/telemetry.env
```

| Variable | Bedeutung |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Elastic-Endpunkt (`https://…:443`) oder lokaler Collector `http://127.0.0.1:4318` |
| `OTEL_EXPORTER_OTLP_HEADERS` | `Authorization=ApiKey%20<key>` (Leerzeichen als `%20`), nur ohne Collector |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` |

---

## 6. Konfiguration (config.yaml)

Die wichtigsten Abschnitte in `config/config.yaml`:

| Abschnitt | Inhalt |
|---|---|
| `gpio` | Pin-Belegung der Relais und Sensoren (BCM-Nummerierung) |
| `pump.dac_full_scale_v`, `dac_offset_v` | Gemessene Kalibrierung des Verstärkers (→ Kapitel 9) |
| `pump.input_full_scale_v` | Spannung, bei der die Pumpe 100 % anzeigt (nur noch Info im Log) |
| `pump.speeds` | Drehzahlstufen in % (`off`, `min`, `eco`, `normal`, `high`, `backwash`) |
| `pump.schedule` | Tageszeitprogramm (Zeitfenster → Drehzahlstufe) |
| `pump.min_speed` | Mindestdrehzahl statt 0 %: `mode` (`off`/`always`/`winter`), `speed` (Name aus `speeds`), `winter_months` |
| `dac` | `vdd` (3,3), `opamp_gain` (3,2), `max_output_v` (Spannung bei 100 %, max. 10), `ramp_pct_per_s` (5), `refresh_interval_s` (0,2) |
| `frost` | Software-Frostschutz: Schwelle, Drehzahl, Mindestlaufzeit, `enabled`, optional `hysteresis_c` (Standard 0,5) |
| `backwash` | Automatische Rückspülung (Tag/Uhrzeit, Dauer), `auto_enabled`, optional `ramp_timeout_sec` |
| `mqtt` | Broker, Port, Benutzer, Topic-Präfix (Passwort → secrets.env) |
| `api` | Host und Port des Dashboards (Standard 8080) |
| `logging` | Log-Level und Log-Datei |
| `telemetry` | `enabled` (Standard false), `metrics_interval_s`, `log_level`, `fastapi_instrumentation`, `system_metrics` |

Fehlen `dac`, `pump.min_speed` oder `telemetry`, gelten sichere Standardwerte
(Mindestdrehzahl aus, Telemetrie aus). Ungültige Werte werden mit einer Warnung
im Log auf den Standard gesetzt oder geklemmt, z. B. `max_output_v` über 10 V
hart auf 10 V.

**Mindestdrehzahl:** Würde der Zeitplan 0 % fahren (z. B. „Nacht“), läuft die
Pumpe stattdessen mit `min_speed.speed`. Mit `mode: winter` gilt das nur in den
Monaten aus `winter_months` (Standard November bis März). Ein manuelles „Aus“
bleibt aus. Der Frostschutz hat weiterhin Vorrang.

> Die `config.yaml` auf dem Pi enthält die Kalibrierwerte deiner Anlage.
> Beim Einspielen von Updates **nicht überschreiben** (→ Kapitel 11).

---

## 7. systemd-Dienst einrichten

### 7.1 Dienst-Unit

Falls noch nicht vorhanden, `/etc/systemd/system/pool_marx.service` anlegen
(Pfade ggf. anpassen):

```ini
[Unit]
Description=Pool Marx Steuerung
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/opt/pool_marx/pool_controller
ExecStart=/opt/pool_marx/pool_controller/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 7.2 Ergänzung für Secrets und Watchdog

```bash
cd /opt/pool_marx/pool_controller/pi
sudo install -D -m 644 pool_marx.override.conf \
  /etc/systemd/system/pool_marx.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl enable --now pool_marx
```

Die Ergänzung setzt `EnvironmentFile=/etc/pool_marx/secrets.env`,
`EnvironmentFile=-/etc/pool_marx/telemetry.env` (das `-` bedeutet: Die Datei
darf fehlen) und `WatchdogSec=60`: Meldet sich der Dienst 60 Sekunden nicht,
startet systemd ihn neu.

Optional läuft ein OpenTelemetry-Collector als eigener Dienst `otelcol-pi`
(→ [telemetry.md](telemetry.md), Abschnitt 6).

---

## 8. Erster Start und Funktionstest

### 8.1 Log prüfen

```bash
sudo systemctl status pool_marx
journalctl -u pool_marx -n 50 --no-pager
```

Erwartet:
- `MCP4725 DAC initialisiert`
- `DAC: 100 % = 9.50V (Code 3719), 4095 → 10.483V (gemessen 3.3V × 3.2), Offset -0.021V, Rampe 5%/s, Takt 0.2s, Pumpe zeigt bei 100 % ca. 94%`
- `Mindestdrehzahl: Modus 'winter', min = 20%, Monate [1, 2, 3, 11, 12]`
- `systemd-Watchdog aktiv (Meldung alle 30s)`
- `Pool Marx API gestartet`
- `Zeitplanwechsel: 'None' → '…'` und `Pumpe Sollwert 0% → …% (Grund: schedule …)`
- nur mit Telemetrie: `Telemetrie aktiv: OTLP/HTTP → …`

Nicht erwartet (beheben):
- `POOL_API_SECRET fehlt …` → Kapitel 5
- `Secret '…' steht noch in config.yaml` → Zeile aus `config.yaml` löschen
- `Simulationsmodus aktiv` → Pakete `RPi.GPIO`/`smbus2` fehlen (Kapitel 4)
- `DAC Schreibfehler (nach Retry)` → I²C-Verkabelung prüfen, `i2cdetect -y 1`
- `Config … ungültig` bzw. `… geklemmt` → Wert in `config.yaml` korrigieren
- `Telemetrie deaktiviert — Initialisierung fehlgeschlagen` → [telemetry.md](telemetry.md), Abschnitt 8

> Solange noch kein DS18B20-Temperatursensor verbaut ist, erscheint
> `Temperatursensor liefert keinen Wert` und im Dashboard ein gelbes Banner.
> Das ist korrekt: Der Software-Frostschutz kann dann nicht auslösen, nur das
> PTC-Heizelement im Schaltschrank schützt.

### 8.2 Dashboard

Im Browser `http://<pi-ip>:8080/` öffnen und mit `POOL_API_SECRET`
anmelden. Der grüne Punkt oben rechts zeigt die Live-Verbindung.

### 8.3 Funktionstest (mit Multimeter am 0–10-V-Ausgang)

Nach jeder Drehzahländerung die Rampe abwarten (5 %/s). Im Dashboard steht
unter der Drehzahl „Ist … % → Rampe auf … %“, bis der Wert erreicht ist.
Soll-Spannungen bei `max_output_v: 9.5`; die Anzeige der Pumpe ist dabei
niedriger als der Sollwert (→ Kapitel 9.2).

| Test | Erwartung |
|---|---|
| Preset **Eco (30 %)** | ≈ 2,85 V nach ca. 6 s, Pumpe zeigt ≈ 28 % |
| Preset **Hoch (80 %)** | ≈ 7,60 V, Pumpe zeigt ≈ 75 % |
| Preset **Aus** | Spannung fällt per Rampe auf 0 V, Pumpe stoppt und bleibt aus (Modus „Manuell“) |
| **Zurück zum Zeitplan** | Drehzahl laut aktuellem Zeitfenster; nachts im Winter 20 % (Mindestdrehzahl) |
| Beleuchtung an/aus | Relais 7 schaltet |
| Zweites Gerät (Handy) öffnen, am ersten Gerät schalten | Zweites Gerät aktualisiert sich innerhalb ~1 s |
| **Alles abschalten** | sofort 0 V (ohne Rampe), Pumpenrelais öffnet, rotes Banner, Bedienelemente gesperrt |
| Warten ≥ 60 s | Pumpe bleibt aus (Zeitplan ist gesperrt) |
| **Quittieren** | Pumpenrelais schließt, Pumpe bleibt aus |
| **Rückspülung starten** | Pumpe fährt per Rampe auf 100 % (bis 20 s), erst dann öffnet Relais 3, danach Rückkehr zum vorherigen Betrieb |
| **Pumpenfehler notieren** | WARNING-Zeile `PUMPENFEHLER-NOTIZ: …` im Log |
| `sudo systemctl restart pool_marx` | Dienst startet neu, DAC startet bei 0 V und fährt per Rampe auf den Zeitplanwert, Dashboard verbindet sich automatisch |

---

## 9. Kalibrierung des 0–10-V-Signals

Die Kette ist: **Prozent → Zielspannung → DAC-Wert.**

1. Zielspannung = Prozent / 100 × `dac.max_output_v` (aktuell 9,5 V bei 100 %).
2. DAC-Wert aus der gemessenen Kalibrierung:
   `DAC = round((Zielspannung − dac_offset_v) / dac_full_scale_v × 4095)`.
   Ohne Kalibrierung gilt der Nennwert `dac.vdd × dac.opamp_gain` (3,3 × 3,2 = 10,56 V).
3. Der DAC-Wert für 100 % wird nie überschritten. Eine Kalibrierung, die mehr als
   15 % vom Nennwert abweicht, ignoriert die Software mit Warnung.

| Wert | Bedeutung |
|---|---|
| `dac.max_output_v` | Spannung am Ausgang bei 100 % (höchstens 10 V) |
| `pump.dac_full_scale_v` | Gemessene Spannung am LM358-Ausgang bei DAC-Wert 4095 |
| `pump.dac_offset_v` | Offset des LM358 (Spannung bei DAC-Wert 0, meist wenige mV negativ) |
| `pump.input_full_scale_v` | Spannung, die die Pumpe als 100 % anzeigt. Wird nicht mehr zur Berechnung benutzt, nur für den Hinweis im Log |

Aktuelle Werte: `max_output_v` 9,5 · Kalibrierung vom 28.09.2026: `10.483`, `-0.021`, `10.13`.

Soll-Spannungen mit diesen Werten (Toleranz ±0,2 V):

| % | 0 | 20 | 40 | 60 | 80 | 100 |
|---|---|---|---|---|---|---|
| DAC-Wert | 0 | 750 | 1493 | 2235 | 2977 | 3719 |
| Spannung | 0,00 V | 1,90 V | 3,80 V | 5,70 V | 7,60 V | 9,50 V |

### 9.1 DAC/Verstärker kalibrieren (zwei Messpunkte)

1. Vorübergehend `dac_offset_v: 0` setzen, Dienst neu starten.
2. Zwei Drehzahlen einstellen, z. B. 30 % und 80 %, Rampe abwarten und jeweils die Spannung messen.
3. Die zugehörigen DAC-Werte berechnen:
   `DAC = round(Soll-V / dac_full_scale_v × 4095)` mit Soll-V = Prozent / 100 × `max_output_v`.
4. Aus den zwei Punkten (DAC₁, U₁) und (DAC₂, U₂):
   ```
   a                = (U₂ − U₁) / (DAC₂ − DAC₁)
   dac_offset_v     = U₁ − a × DAC₁
   dac_full_scale_v = a × 4095
   ```
5. Werte eintragen, Dienst neu starten. Kontrolle: 30 % → 2,85 V, 80 % → 7,60 V.

### 9.2 Anzeige der Pumpe und `max_output_v`

Die Pumpe zeigt erst bei ca. 10,13 V 100 % an (`input_full_scale_v`). Mit
`max_output_v: 9.5` erreicht sie also nur etwa 93–94 %, und jede Stufe liegt
etwa 6 % unter dem Sollwert (30 % → ≈ 28 %, 60 % → ≈ 56 %, 80 % → ≈ 75 %).
Die Pumpe schneidet Nachkommastellen ab.

Das ist der Preis für den Sicherheitsabstand zu 10 V. Wer die Anzeige näher am
Sollwert haben will, setzt `max_output_v` höher, aber nie über 10,0. Werte
darüber klemmt die Software auf 10 V. Danach C1 aus [abnahme.md](abnahme.md)
wiederholen (Ausgang bei 100 % muss unter 10,0 V bleiben).

---

## 10. Bedienung

### 10.1 Betriebsarten

| Anzeige | Bedeutung | Wie verlassen |
|---|---|---|
| 📅 Zeitplan | Drehzahl laut `pump.schedule`, im Winter nachts Mindestdrehzahl statt 0 % | Preset/Slider → Manuell |
| ✋ Manuell | Vom Benutzer gesetzte Drehzahl, auch „Aus“ bleibt aus | **Zurück zum Zeitplan** |
| ⏸ Aus | Zeitplan sieht gerade „off“ vor und keine Mindestdrehzahl aktiv | automatisch im nächsten Zeitfenster |
| ❄️ Frostschutz | Temperatur unter Schwelle, Pumpe mindestens auf `min` | automatisch, wenn die Temperatur mindestens Schwelle + 0,5 °C erreicht und die Mindestlaufzeit um ist |
| 🔄 Rückspülung | Sequenz läuft (≈ 4 Min inkl. Rampe), andere Befehle gesperrt | automatisch |
| 🛑 Abgeschaltet | „Alles abschalten“ wurde ausgelöst, alles gesperrt | **Quittieren** |

**Vorrangregeln**
- Während des Frostschutzes wird „Aus“ bzw. eine zu niedrige Drehzahl abgelehnt.
- Die Mindestdrehzahl ersetzt nur die 0 % des Zeitplans, nie ein manuelles „Aus“.
- Die Rückspülung startet nicht bei niedrigem Wasserstand und nicht doppelt.
  Sie öffnet das Ventil erst, wenn die Pumpe ihre Drehzahl erreicht hat, und
  bricht ab, wenn das nicht innerhalb von Rampendauer + 10 s gelingt.
- Nach „Alles abschalten“ sind Zeitplan, Frostschutz, Rückspülung, Licht und
  alle Befehle (Dashboard, API, MQTT) gesperrt. **Quittieren** schaltet die
  Netzspannung der Pumpe wieder zu — die Pumpe bleibt aus, bis jemand eine
  Drehzahl wählt oder den Zeitplan aktiviert.

### 10.2 API für Skripte

Alle Routen außer `/api/health`, `/api/session` und `/api/login` brauchen eine
Anmeldung. Für Skripte per Header:

```bash
PW='dein-passwort'
curl -H "Authorization: Bearer $PW" http://<pi-ip>:8080/api/status
curl -X POST -H "Authorization: Bearer $PW" http://<pi-ip>:8080/api/pump/speed/60
curl -X POST -H "Authorization: Bearer $PW" http://<pi-ip>:8080/api/pump/schedule
```

| Route | Funktion |
|---|---|
| `GET /api/status` | Gesamtstatus, inkl. Istwert (`hardware.pump.actual_speed_pct`) und DAC-Fehlerzähler (`hardware.dac.write_errors`) |
| `GET /api/pump` | Pumpenstatus |
| `POST /api/pump/speed/{0-100}` | Drehzahl setzen (Modus Manuell) |
| `POST /api/pump/on` `{"speed_pct": 60, "duration_min": 30}` | Manuell ein, optional mit Zeitlimit |
| `POST /api/pump/off` | Pumpe aus (bleibt aus) |
| `POST /api/pump/schedule` | Zurück zum Zeitplan |
| `POST /api/pump/fault-note` `{"text": "E-04"}` | Pumpenfehler mit aktuellem Status ins Log schreiben (Text optional, max. 500 Zeichen) |
| `POST /api/backwash/start` | Rückspülung starten |
| `POST /api/backwash/cancel?resume_schedule=true` | Rückspülung abbrechen |
| `POST /api/light` `{"action":"on"\|"off"}` | Beleuchtung |
| `POST /api/emergency_stop` | Alles abschalten (verriegelt) |
| `POST /api/emergency/ack` | Quittieren (Alias: `POST /api/pump/mains/restore`) |
| `GET /api/pump/mains` | Zustand des Pumpenrelais |

Antworten: `401` nicht angemeldet · `409` Rückspülung läuft / Wasserstand zu
niedrig · `422` ungültige Eingabe (z. B. Drehzahl außerhalb 0–100) · `423`
Abschaltung aktiv, erst quittieren · `503` kein Passwort gesetzt.

### 10.3 Pumpenfehler dokumentieren (E-04)

Zeigt die Pumpe einen Fehler, im Dashboard **„Pumpenfehler notieren“** drücken
(oder `POST /api/pump/fault-note`). Das Log bekommt eine WARNING-Zeile mit Modus,
Soll, Ist, DAC-Spannung, Zeitplaneintrag und der letzten Sollwertänderung. Jede
Sollwertänderung und jeder Zeitplanwechsel steht ohnehin mit Zeitstempel im Log:

```bash
grep -E "PUMPENFEHLER|Zeitplanwechsel|Sollwert" /var/log/pool_marx/pool.log
```

Den geplanten A/B-Test beschreibt [abnahme.md](abnahme.md), Abschnitt C5.

### 10.4 MQTT (optional)

Befehle an `pool_marx/cmd/pump`: `on`, `off`, `speed:60`, `backwash`, `schedule`;
an `pool_marx/cmd/light`: `on`, `off`. Status unter `pool_marx/pump/…`.
Mosquitto sollte nur auf `127.0.0.1` lauschen und anonyme Zugriffe verbieten.

---

## 11. Updates einspielen

> **Empfohlen:** getaggte Versionen mit `release/deploy.sh vX.Y.Z` einspielen.
> Jede Version bekommt ein eigenes Verzeichnis mit eigenem venv, die Umschaltung
> wird geprüft und bei Fehlern automatisch zurückgerollt
> (`release/deploy.sh --rollback` für den manuellen Weg).
> Einrichtung und Ablauf: [RELEASE.md](../RELEASE.md), Abschnitt 5. Nach der
> einmaligen Migration liegt die Config unter `/opt/pool_marx/shared/config/`.
>
> Der manuelle Weg unten funktioniert nur **vor** dieser Migration.

```bash
# vom Mac aus — config.yaml wird NICHT kopiert
scp main.py requirements.txt pi@<pi-ip>:/opt/pool_marx/pool_controller/
scp -r api services web pi pi@<pi-ip>:/opt/pool_marx/pool_controller/
ssh pi@<pi-ip> "sudo systemctl restart pool_marx"
```

Bei geänderten Abhängigkeiten zusätzlich auf dem Pi:
`/opt/pool_marx/pool_controller/venv/bin/pip install -r requirements.txt`

Neue Einstellungen aus der `config.yaml` im Projekt von Hand in die
`config.yaml` auf dem Pi übernehmen, also nur neue Blöcke ergänzen, nichts
überschreiben. Bei Änderungen an `pi/pool_marx.override.conf` diese neu
installieren (Kapitel 7.2) und `sudo systemctl daemon-reload` ausführen.
Offene Browser-Tabs einmal neu laden.

**Welche Version läuft?** Steht im Dashboard unten neben „Pool Marx Steuerung“,
im Startlog (`Pool Marx Steuerung startet — Version …`) und unter
`curl -s http://localhost:8080/api/health`. Am besten nur getaggte Releases
einspielen (`git checkout vX.Y.Z` vor dem Kopieren); Änderungen stehen in
[CHANGELOG.md](../CHANGELOG.md).

> Hinweis: Jeder Neustart des Dienstes öffnet und schließt kurz das
> Pumpenrelais (bekannter Punkt in der Roadmap). Solange die Pumpe nicht am
> Relais angeschlossen ist, hat das keine Wirkung auf die Pumpe.

---

## 12. Fehlersuche

| Symptom | Ursache / Lösung |
|---|---|
| Dashboard zeigt `{"detail":"Not Found"}` | `web/` fehlt oder altes `api/main.py` → Kapitel 11, Dienst neu starten |
| Login: „Kein Passwort konfiguriert“ | `POOL_API_SECRET` fehlt → Kapitel 5, `daemon-reload` + Neustart |
| Login klappt, Dashboard „getrennt“ | Seite neu laden; Log auf Fehler prüfen |
| Pumpe reagiert nicht | Steuerart an der Pumpe = Modus 3? Pumpenrelais geschlossen (Status „Netzspannung Pumpe“)? `i2cdetect -y 1` zeigt `60`? |
| Pumpe erreicht die Drehzahl nur langsam | Normal: Rampe 5 %/s (0 → 100 % in 20 s), `dac.ramp_pct_per_s` |
| Anzeige der Pumpe ca. 6 % unter dem Sollwert | Folge von `max_output_v: 9.5` → Kapitel 9.2 |
| Anzeige der Pumpe 1 % daneben | Kalibrierung → Kapitel 9 |
| Pumpe läuft nachts mit 20 % | Mindestdrehzahl im Winter (`pump.min_speed`), gewollt |
| Dashboard zeigt „DAC-Fehler: …“ | I²C-Schreibfehler; Verkabelung prüfen, Log `DAC Schreibfehler` |
| Pumpe zeigt E-04 | Im Dashboard „Pumpenfehler notieren“, dann Pumpe vom Netz trennen und wieder zuschalten (→ Kapitel 10.3) |
| Rückspülung „erreicht …% nicht innerhalb …s“ | DAC-Thread hängt oder I²C gestört; Log prüfen, Dienst neu starten |
| `Telemetrie-Export gestört` (alle 5 Min) | Endpunkt/Key prüfen; Steuerung läuft normal weiter → [telemetry.md](telemetry.md) |
| Befehle werden mit „Not-Aus aktiv“ abgelehnt | Abschaltung wurde ausgelöst → Quittieren |
| „Rückspülung gesperrt: Wasserstand zu niedrig“ | Schwimmerschalter meldet niedrig (Verkabelung/Pegel prüfen) |
| Dienst startet ständig neu | `journalctl -u pool_marx -n 100` — Exit-Code 1 = Absturz in der Automatik, Ursache steht davor im Log |
| MQTT-Reconnect-Meldungen alle 10 s | Broker nicht erreichbar; harmlos, wenn MQTT nicht genutzt wird |

Nützliche Befehle:

```bash
journalctl -u pool_marx -f                 # Live-Log
tail -f /var/log/pool_marx/pool.log        # Log-Datei
sudo systemctl restart pool_marx           # Neustart
curl http://localhost:8080/api/health      # lebt die API?
```

---

## 13. Checkliste Inbetriebnahme

- [ ] Steuerart der Pumpe auf Modus 3 (0–10 V)
- [ ] I²C aktiv, `i2cdetect` zeigt `60`
- [ ] `gpio=…=op,dh` in `config.txt`, Relais bleiben beim Booten aus
- [ ] Hardware-Watchdog (`RuntimeWatchdogSec=15`)
- [ ] NTP synchron (`timedatectl`)
- [ ] Software installiert, venv angelegt, kein Simulationsmodus
- [ ] MCP4725-EEPROM auf 0 V gesetzt und nach Stromunterbrechung 0 V gemessen
- [ ] `/etc/pool_marx/secrets.env` angelegt (Rechte 600, root), keine Secrets in `config.yaml`
- [ ] `config.yaml` auf dem Pi um `dac`, `pump.min_speed`, `telemetry` ergänzt
- [ ] Dienst + Override installiert, Watchdog im Log aktiv
- [ ] Dashboard-Login funktioniert, Live-Update auf zwei Geräten geprüft
- [ ] 0–10 V gemessen (0/20/40/60/80/100 %, [abnahme.md](abnahme.md) C1)
- [ ] Rampe sichtbar, Start/Stopp ohne Fehler an der Pumpe (C3)
- [ ] Alles abschalten → gesperrt → Quittieren getestet
- [ ] Rückspülung einmal vollständig beobachtet (Ventil öffnet erst nach der Rampe)
- [ ] Optional: Telemetrie eingerichtet, Daten in Elastic sichtbar ([telemetry.md](telemetry.md))
- [ ] Pumpe am Pumpenrelais angeschlossen (derzeit nicht)
- [ ] Reparaturschalter vor der Pumpe vorhanden
- [ ] Elektrik abgenommen (RCD 30 mA, Potentialausgleich, 0-V-Masse getrennt von N)

---

## 14. Automatische Tests

Die Tests laufen auf dem Mac (oder in CI) komplett im Simulationsmodus: kein
I²C, kein GPIO, kein Netzwerk. Zeitabhängige Abläufe (Rampe, Zeitplan über 7 Tage,
Sommer-/Winterzeit, Rückspülung) laufen auf einer virtuellen Uhr, also ohne echtes Warten.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q                              # alle Tests, ca. 1 Min
.venv/bin/pytest -q -m "not stage_b"             # nur Stufe A (schnell)
.venv/bin/pytest -q --cov=services --cov=api     # mit Abdeckung
```

| Stufe | Inhalt | Wo |
|---|---|---|
| A | Unit-Tests: DAC-Mapping, Rampe, DAC-Thread, Zeitplan/Modi, Relais, API, Konfiguration, Telemetrie, Logging | `tests/test_*.py` |
| B | Dauerlauf 7 Tage, Fehlerinjektion, Neustart, systemd-Dateien, Last mit 20 WebSocket-Clients | `tests/test_stage_b.py` (Marker `stage_b`) |
| C | Abnahme an der echten Hardware, manuell | [abnahme.md](abnahme.md) |

Die Tests prüfen auch, dass `config/config.yaml` dabei nicht verändert wird.
Das `.venv` gehört nur auf den Mac und wird nicht auf den Pi kopiert.
