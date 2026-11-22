# Telemetrie: Pool Marx → Elastic Observability

Ziel: Pumpenfehler (E-04) mit Zeitplanwechseln, Sollwertänderungen, DAC-Fehlern
und der Pi-Versorgung korrelieren. Der Code nutzt nur das Standard-OpenTelemetry-SDK
(OTLP/HTTP) und ist herstellerneutral.

```
pool_marx ──OTLP/HTTP──► (optional) otelcol-pi @127.0.0.1:4318 ──► Elastic
```

## 1. Endpunkt und API-Key in Elastic

1. Kibana → **Observability → Add data → OpenTelemetry** (bzw. „Application“ →
   OpenTelemetry). Dort steht der **Managed OTLP Endpoint**, z. B.
   `https://<projekt>.ingest.<region>.elastic.cloud:443`.
2. Über denselben Assistenten oder **Stack Management → API keys** einen API-Key
   erzeugen, nur mit Ingest-Rechten. Den Base64-Wert („Encoded“) notieren.
3. Ohne Collector: `/etc/pool_marx/telemetry.env` (Vorlage `deploy/telemetry.env.example`):
   ```
   OTEL_EXPORTER_OTLP_ENDPOINT=https://<projekt>.ingest.<region>.elastic.cloud:443
   OTEL_EXPORTER_OTLP_HEADERS=Authorization=ApiKey%20<encoded-key>
   OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
   ```
   Mit Collector: dort nur `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`.
   Der Key steht dann in `/etc/pool_marx/otelcol.env`.
4. In `config.yaml` `telemetry.enabled: true` setzen und den Dienst neu starten.
   Im Log muss `Telemetrie aktiv: OTLP/HTTP → …` erscheinen.

Selbst betriebenes Elastic ohne Managed OTLP: Als Ziel einen EDOT-Collector
(Gateway) oder den APM-Server mit OTLP/HTTP-Unterstützung verwenden.

Secrets stehen nur in den env-Dateien, nie in `config.yaml` und nie im Repo.
Der Code loggt weder Header noch Schlüssel. Dafür gibt es einen eigenen Test.

## 2. Was gesendet wird

**Metriken** (Standard alle 15 s, fast alle als Observable, also ohne Last in der Steuerung):

| Metrik | Einheit | Bedeutung |
|---|---|---|
| `pool.pump.setpoint` / `pool.pump.actual` | % | Soll / Ist (Rampe) |
| `pool.pump.dac_output` | V | berechnete Spannung 0–10 V |
| `pool.pump.mode` | 1 | Attribut `mode` = schedule/manual/frost/backwash/off/emergency |
| `pool.pump.mains_connected`, `pool.light.on`, `pool.level.ok`, `pool.frost.active`, `pool.pump.min_speed_active` | 1 | 0/1 |
| `pool.pump.setpoint_changes` | 1 | Zähler, Attribut `reason` |
| `pool.dac.write_errors` | 1 | Zähler (nach Retry) |
| `pool.dac.write_duration` | ms | Histogramm, Stichprobe alle ca. 5 s |
| `pool.backwash.runs` / `pool.backwash.duration` | 1 / s | Zähler / Histogramm (`result`) |
| `pool.pump.fault_notes` | 1 | Zähler aus `/api/pump/fault-note` |
| `pi.cpu.temperature` | Cel | aus thermal_zone0 |
| `pi.undervoltage_now/_occurred`, `pi.throttled_now/_occurred`, `pi.freq_capped_*`, `pi.soft_temp_limit_*` | 1 | `vcgencmd get_throttled` |

**Logs:** alle Zeilen ab INFO. Ereignisse tragen das Attribut `event.name`:
`pump.setpoint_changed` (alt/neu/Grund), `schedule.transition`, `frost.activated` / `frost.deactivated`,
`backwash.started` / `backwash.finished`, `emergency_stop`, `pump.mains_cutoff` /
`pump.mains_restore`, `pump.fault_note` (Status und Freitext), `dac.write_error` (max. 1×/min).

`telemetry.system_metrics: true` liefert CPU, RAM, Netz und Disk aus der App.
Wenn der Collector mit `hostmetrics` läuft, setze es auf `false`, sonst kommen die Daten doppelt.

## 3. Dashboards (Kibana → Dashboards → Lens)

1. **Pumpe:** `pool.pump.setpoint` und `pool.pump.actual` als Linien, dazu `pool.pump.dac_output`.
   Als Annotation `event.name : "pump.fault_note"` (rote Marker) und `"schedule.transition"`.
2. **Modus:** `pool.pump.mode` (max) nach `mode` gestapelt, dazu `pool.pump.min_speed_active` und `pool.frost.active`.
3. **DAC:** `pool.dac.write_errors` (Differenz/Rate), `pool.dac.write_duration` (p50/p99).
4. **Pi:** `pi.undervoltage_now`, `pi.undervoltage_occurred`, `pi.throttled_now`, `pi.cpu.temperature`.
5. **Ereignisliste:** Discover-Ansicht `service.name : "pool_marx" and event.name : *`, gespeichert.

Für die E-04-Korrelation lege Panel 1 und 4 untereinander mit gleicher Zeitachse.

## 4. Alarmregeln (Observability → Alerts → Create rule)

| | Regel | Bedingung |
|---|---|---|
| a | **Keine Daten** (Custom threshold) | Anzahl Dokumente mit `service.name:pool_marx` < 1 in 10 Min, „Alert on no data“ an. Deckt Pi-, Netz- und Dienstausfall ab. |
| b | **ERROR-Logs** (Log threshold oder ES query) | `service.name:pool_marx and severity_text:(ERROR or CRITICAL)`, Anzahl > 0 in 5 Min |
| c | **DAC-Fehler steigen** (Custom threshold) | Anstieg von `pool.dac.write_errors` > 0 in 5 Min |
| d | **Pumpenfehler-Notiz** (ES query) | `event.name:"pump.fault_note"`, Anzahl > 0 in 1 Min |
| e | **Unterspannung** (Custom threshold) | max `pi.undervoltage_now` ≥ 1 in 5 Min. Optional: `pi.undervoltage_occurred` als Hinweis seit Boot. |

Je nach Datenstrom heißen die Felder in Elastic mit Präfix, z. B. `attributes.event.name` oder `severity_text`.
Am besten in Discover nachsehen, bevor du die Regel anlegst.

## 5. Zeit synchron halten (wichtig für die Korrelation)

```
timedatectl status          # "System clock synchronized: yes", "NTP service: active"
sudo timedatectl set-ntp true
```
Ohne NTP stimmen die Zeitstempel nicht, und Pumpenfehler lassen sich nicht sauber
mit Zeitplanwechseln abgleichen. Der Pi hat keine RTC: Nach einem Stromausfall
ist die Uhr falsch, bis NTP synchronisiert hat.

## 6. Collector auf dem Pi (Phase 2, empfohlen)

**Wahl: `otelcol-contrib`** (offizielle OpenTelemetry-Distribution), .deb für linux/arm64.

- Die Upstream-Releases liefern für jede Version DEB-, RPM- und APK-Pakete für arm64 mit
  systemd-Unit. Installiert wird mit einem `dpkg -i`. contrib enthält alles Benötigte:
  `otlp`, `hostmetrics`, `resourcedetection`, `memory_limiter`, `batch`, `file_storage`, `otlphttp`.
- Alternative **Elastic EDOT Collector:** Den gibt es ebenfalls für aarch64 (tar.gz, DEB, RPM),
  aber als Teil des Elastic-Agent-Pakets. Das ist deutlich größer und bringt hier keinen Vorteil,
  weil wir nur OTLP an den Managed Endpoint weiterleiten. EDOT lohnt sich, wenn Elastic-spezifische
  Komponenten nötig sind, z. B. ein selbst betriebenes Elasticsearch ohne Managed OTLP oder Support durch Elastic.

Installation (Version anpassen, Release-Seite von `opentelemetry-collector-releases`):
```
wget https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v<VER>/otelcol-contrib_<VER>_linux_arm64.deb
sudo dpkg -i otelcol-contrib_<VER>_linux_arm64.deb
sudo systemctl disable --now otelcol-contrib            # Standarddienst aus
sudo install -m 644 scripts/otelcol-pi.yaml /etc/pool_marx/otelcol-pi.yaml
sudo install -m 600 deploy/otelcol.env.example /etc/pool_marx/otelcol.env   # dann Werte eintragen
sudo install -m 644 scripts/otelcol-pi.service /etc/systemd/system/
otelcol-contrib validate --config /etc/pool_marx/otelcol-pi.yaml
sudo systemctl daemon-reload && sudo systemctl enable --now otelcol-pi
curl -s http://127.0.0.1:13133/   # Health-Check
```
Speicher (Pi 4, 2 GB): `memory_limiter` 150 MiB, `GOMEMLIMIT=160MiB`, `MemoryMax=250M`.
Die persistente Queue (`/var/lib/otelcol-pi`) puffert Internetausfälle und Neustarts.

## 7. Paketversionen

Die OpenTelemetry-Pakete sind in `requirements.txt` fest gepinnt (SDK 1.45.2,
aktuell die neueste Version). Grund: `services/telemetry.py` nutzt den
`LoggingHandler` aus dem SDK, der ab 1.45 als veraltet markiert ist und in einer
künftigen Version entfallen kann. Vor einem Update auf den Handler aus
`opentelemetry-instrumentation-logging` umstellen und `pytest -q` laufen lassen
(offener Punkt in der [Roadmap](../ROADMAP.md)).

## 8. Fehlersuche

- Kein `Telemetrie aktiv` im Log: Ist `telemetry.enabled` true? Wurde die env-Datei gelesen?
  Prüfen mit `systemctl show pool_marx -p EnvironmentFiles`.
- `Telemetrie-Export gestört: …` erscheint höchstens alle 5 Min: Endpunkt oder Key prüfen.
  Die Steuerung läuft dabei normal weiter.
- `Telemetrie deaktiviert — Initialisierung fehlgeschlagen`: Paket fehlt im venv oder
  `OTEL_EXPORTER_OTLP_ENDPOINT` ist ungültig (muss mit `http://` oder `https://` beginnen).
