# Abnahme an der echten Hardware (Stufe C)

Stufe A und B laufen automatisch: `pytest -q` auf dem Mac oder in CI. Diese Checkliste
deckt ab, was nur mit echter Hardware geht. Ergebnisse direkt hier eintragen.

Datum: ____________  Software-Stand: ____________  Prüfer: ____________

## C1 — Ausgangsspannung LM358 (Pumpe NICHT angeschlossen)

Soll-Werte aus der aktuellen Config (max_output_v 9,5 V, Kalibrierung 10,483 V / −0,021 V).
Toleranz ±0,2 V. Bei 100 % muss der Wert unter 10,0 V liegen.

Einstellen: `curl -X POST -H "Authorization: Bearer $POOL_API_SECRET" http://<pi>:8080/api/pump/speed/<pct>`,
danach ca. 25 s warten (Rampe).

| % | DAC-Code | Soll [V] | Gemessen [V] | OK |
|---|---|---|---|---|
| 0 | 0 | 0,00 | | |
| 20 | 750 | 1,90 | | |
| 40 | 1493 | 3,80 | | |
| 60 | 2235 | 5,70 | | |
| 80 | 2977 | 7,60 | | |
| 100 | 3719 | 9,50 (< 10,0) | | |

Oszilloskop oder Logger am Ausgang, Sprung 0 → 60 % und zurück:

- [ ] Rampe sichtbar, ca. 12 s, keine Stufen > 0,2 V
- [ ] kein Überschwingen über 10 V
- [ ] Rauschen (Spitze-Spitze): ______ mV

Erwartete Anzeige an der Pumpe (sie zeigt erst bei ≈ 10,13 V 100 %):
30 % → ≈ 28 %, 60 % → ≈ 56 %, 80 % → ≈ 75 %, 100 % → ≈ 93 %. Gilt für C3, wenn die Pumpe angeschlossen ist.

## C2 — Reset-Test (EEPROM-Startwert)

Vorher einmalig: `scripts/mcp4725_set_default_zero.py --yes`. Ausgabe muss „EEPROM = 0“ zeigen.

| Test | Ausgang beim Einschalten | Ausgang nach Dienststart | OK |
|---|---|---|---|
| `sudo reboot` | | | |
| Stromunterbrechung Pi und MCP | (Soll 0 V, nicht ca. 5 V) | | |
| `--check` nach dem Reboot meldet EEPROM = 0 | | | |

## C3 — Pumpe angeschlossen: Rampe und Start/Stopp

10 Wiederholungen: Start 0 → 60 %, 60 s laufen lassen, Stopp 60 → 0 %, 60 s warten.

| # | sanft angelaufen | sanft gestoppt | Fehler im Display | OK |
|---|---|---|---|---|
| 1–10 | | | | |

## C4 — Zeitplanübergänge (mind. 2 Tage)

Auswerten mit `grep -E "Zeitplanwechsel|Sollwert" /var/log/pool_marx/pool.log` oder in Elastic.

| Tag | 07:00 | 09:00 | 19:00 | 21:00 (Winter: 20 %) | Pumpenfehler? |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |

## C5 — A/B-Test E-04

- Phase A, 3 Tage: konstant 40 %, keine Nachtabschaltung (`/api/pump/speed/40`, Modus manuell).
- Phase B, 3 Tage: Zeitplan mit Rampe (`/api/pump/schedule`).
- Bei jedem Fehler: Dashboard-Button „Pumpenfehler notieren“ oder `POST /api/pump/fault-note`.

| Phase | Zeitpunkt E-04 | letzte Sollwertänderung (aus Notiz) | Pi-Unterspannung? | Bemerkung |
|---|---|---|---|---|
| | | | | |

Fazit: Tritt E-04 □ bei Übergängen, □ bei Unterspannung oder □ zufällig auf?

## C6 — Not-Aus und Wiederherstellung

> Stand 2026-11-22: Die Pumpe ist **nicht** am Pumpenrelais angeschlossen. Bis
> dahin prüft dieser Test nur das Relais und den DAC; die Pumpe behält ihre
> Netzspannung. Nach dem Anschließen den Test vollständig wiederholen.

- [ ] Dashboard „Alles abschalten“: Pumpe aus, Relais klickt, DAC sofort 0 V
- [ ] „Quittieren“: Relais zieht an, Pumpe bleibt aus
- [ ] erst nach neuem Befehl läuft die Pumpe per Rampe an

## C7 — Licht

- [ ] beide LED-Treiber schalten
- [ ] Relaiskanal entspricht GPIO 25
- [ ] Dashboard-Zustand korrekt (auch nach einem Reload)

## C8 — Telemetrie in Elastic

- [ ] Metriken und Logs erscheinen, `service.name = pool_marx`
- [ ] Zeitstempel stimmen (`timedatectl`: synchronized yes)
- [ ] Netzwerkkabel 15 Min ziehen: Alarm „keine Daten seit 10 Min“ löst aus
- [ ] Kabel wieder stecken: Daten der Lücke kommen nach (nur mit Collector)

## C9 — Stromausfall am Pool

- [ ] FI bzw. Sicherung aus und ein: Pi fährt hoch, DAC 0 V, Zeitplan übernimmt mit Rampe
- [ ] Pumpe braucht einen Netz-Reset? ______

## Aus Stufe A/B hierher verschoben (nicht sinnvoll automatisierbar)

| Thema | Warum manuell | Prüfung | OK |
|---|---|---|---|
| B3 `kill -9` / Stromausfall | Der Startwert hängt vom MCP4725-EEPROM ab und ist nicht simulierbar | `sudo kill -9 $(pidof python)`: Ausgang bleibt beim letzten Wert, bis systemd neu startet, dann 0 V und Rampe. Zeit bis zum Neustart: ____ s | |
| B4 systemd | Auf dem Mac gibt es kein systemd | `systemctl cat pool_marx` zeigt `EnvironmentFile=-…/telemetry.env`; Start ohne diese Datei klappt; `Restart=` steht auf ______; `getcap` auf python zeigt `cap_net_bind_service` (falls Port 80); Dienst läuft als `pi` | |
| B5 CPU-Last | Messwert auf dem Pi | 20 Dashboard-Tabs offen, `top`: CPU von pool_marx unter 50 %: ____ % | |
| B2 Disk voll | Auf echter SD-Karte riskant | Nur beobachten: `df -h /var/log`, Log-Rotation greift (≤ 6 × 10 MB) | |

**Hinweis zu `kill -9`:** Ohne geordnetes `cleanup()` bleibt der DAC auf dem letzten Wert,
bis der Dienst neu startet. Das ist Hardware-Verhalten. Die Pumpe läuft in dieser Zeit
einfach weiter, es entsteht also kein Sprung.
