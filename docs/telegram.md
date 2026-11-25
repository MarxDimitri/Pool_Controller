# Telegram: Meldungen und Steuerung per Bot

Pool Marx kann wichtige Log-Meldungen an Telegram schicken und Befehle
annehmen (`/status`, `/pumpe 60`, `/abschalten` …). Die Funktion ist optional
und standardmäßig aus.

```
Pi (pool_marx) ──HTTPS, ausgehend──► api.telegram.org ◄──── Telegram-App
                 Long Polling (getUpdates)
```

## 1. Netzwerk

- **Nur ausgehende Verbindungen.** Der Pi fragt Telegram per HTTPS nach neuen
  Nachrichten (Long Polling). Es gibt keinen Webhook, keine Portweiterleitung,
  keine öffentliche IP, kein DynDNS. Das funktioniert auch hinter CGNAT oder
  einem Mobilfunk-Router.
- Der Pi braucht Internetzugang zu `api.telegram.org`, TCP 443. Hängt der Pi in
  einem eigenen Netz mit Ausgangsfilter (IoT-VLAN), genau das freigeben (dazu
  DNS, NTP und Paket-Updates).
- **Ohne Internet keine Meldungen.** Die Steuerung läuft normal weiter.
  Meldungen aus der Zeit ohne Internet werden gesammelt und später mit ihrer
  ursprünglichen Uhrzeit verschickt (höchstens 200, ältere fallen raus).
- **Dass der Pi selbst ausgefallen ist, kann er nicht melden.** Dafür braucht
  es eine Gegenstelle draußen, die Alarm schlägt, wenn sich der Pi nicht mehr
  meldet, zum Beispiel Healthchecks.io oder Uptime Kuma mit `/api/health`.
  Jeder Start schickt eine Meldung `✅ Pool Marx … gestartet`. So fallen auch
  Neustarts durch den Watchdog auf.
- Telegram-Bot-Chats sind **nicht Ende-zu-Ende-verschlüsselt**. Telegram kann
  Status und Befehle lesen. Passwörter oder Tokens stehen nie in den Meldungen.

## 2. Bot anlegen

1. In Telegram **@BotFather** öffnen, `/newbot`, Namen und Benutzernamen wählen.
   BotFather antwortet mit dem **Token** (`123456789:AA…`).
2. Optional bei BotFather: `/setjoingroups` → **Disable**. Der Bot antwortet
   in Gruppen ohnehin nicht.
3. Token auf dem Pi in `/etc/pool_marx/secrets.env` eintragen:
   ```
   POOL_TELEGRAM_TOKEN=123456789:AA…
   ```
   Nie in `config.yaml`, nie ins Repo. Wer den Token hat, kann im Namen des
   Bots schreiben, aber den Pool **nicht** steuern (siehe Abschnitt 4).

## 3. Eigene Nutzer-ID herausfinden

Dem neuen Bot in Telegram irgendeine Nachricht schicken, dann auf dem Pi,
**solange `telegram.enabled` noch `false` ist**:

```bash
TOKEN=$(sudo sed -n 's/^POOL_TELEGRAM_TOKEN=//p' /etc/pool_marx/secrets.env)
curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" | grep -o '"from":{"id":[0-9]*'
unset TOKEN
```

Die Zahl ist deine Nutzer-ID. Sie gehört in `allowed_user_ids`, **nicht** der
Name des Bots (`@…Bot`). Der Token wird aus der Datei gelesen und landet
nicht in der Shell-History. Die Datei wird bewusst nicht mit `.`/`source`
eingelesen: Passwörter mit Sonderzeichen (`'`, `"`, `$`) sind für systemd in
Ordnung, für die Shell aber nicht. Kommt keine Ausgabe, dem Bot erneut
schreiben und den `curl`-Befehl wiederholen.

## 4. Konfiguration

In `config.yaml` auf dem Pi (`/opt/pool_marx/shared/config/config.yaml`):

```yaml
telegram:
  enabled: true
  allowed_user_ids: [123456789]   # deine ID, weitere durch Komma
  notify_level: WARNING
  notify_events: [frost.deactivated, backwash.finished, pump.mains_restore]
  notify_startup: true
  notify_state_changes: true
  commands_enabled: true
  manual_duration_min: 120
  max_command_age_s: 120
  confirm_timeout_s: 60
  max_messages_per_min: 6
  repeat_suppress_min: 15
```

| Schlüssel | Bedeutung |
|---|---|
| `enabled` | Bot an/aus (Standard `false`) |
| `allowed_user_ids` | Nur diese Telegram-Nutzer bekommen Meldungen und dürfen Befehle geben. Leer = Bot aus. |
| `notify_level` | Alle Log-Meldungen ab dieser Stufe werden gemeldet (`WARNING`: Frostschutz an, Abschaltung, Fehlernotiz, DAC-Fehler …) |
| `notify_events` | Zusätzlich diese Ereignisse, auch wenn sie nur INFO sind |
| `notify_startup` | Meldung mit Status bei jedem Start; beim Beenden kommt `⏹ Pool Marx wird beendet` |
| `notify_state_changes` | `true` = jede Zustandsänderung melden: Pumpe % (mit Grund), Licht an/aus, Rückspülung Start/Ende. Ohne Ton, mit `/stumm` abschaltbar. Standard `false` |
| `commands_enabled` | `false` = nur `/status` und `/hilfe` |
| `manual_duration_min` | `/pumpe` und `/aus` gelten so lange, danach übernimmt wieder der Zeitplan. So bleibt ein vergessener Befehl nicht wochenlang aktiv. `0` = unbegrenzt |
| `max_command_age_s` | Ältere Befehle werden verworfen, z. B. wenn der Pi offline war und Telegram sie nachliefert |
| `confirm_timeout_s` | So lange gilt der Bestätigungsknopf bei `/rueckspuelen` und `/abschalten` |
| `max_messages_per_min` | Mehr Meldungen werden gesammelt und gebündelt verschickt |
| `repeat_suppress_min` | Gleiche Warnung/Fehlermeldung (z. B. MQTT-Reconnect alle 10 s) höchstens so oft; danach mit „… × wiederholt“. Gilt nicht für Zustandsänderungen. |

Fehlt der Abschnitt, ist der Bot aus. Dann `sudo systemctl restart pool_marx`.
Im Log muss erscheinen: `Telegram aktiv: Bot @…, 1 Nutzer freigegeben, Befehle an`.

### Mit Ton oder ohne

| Art | Beispiele | Ton | `/stumm` |
|---|---|---|---|
| Warnungen und Fehler (ab `notify_level`, mindestens WARNING) | Frostschutz aktiv, Abschaltung, DAC-Fehler, Fehlernotiz, fremder Nutzer | ja | wirkt nicht |
| Zustandsänderungen (`notify_state_changes: true`) | `🌀 07:00:00 Pumpe 20 → 60 % (Zeitplan „Morgenfilterung“)`, `💡 Licht an`, `🔄 Rückspülung fertig nach 212 s` | nein | stumm |
| Weitere Ereignisse (`notify_events`) | Frostschutz aus, Netz wieder an | nein | stumm |
| Start / Beenden des Dienstes | `✅ Pool Marx … gestartet` | ja | wirkt nicht |

Kommen eine laute und eine leise Meldung in derselben Nachricht an, klingelt
die Nachricht. Zustandsänderungen setzen `logging.level: INFO` voraus (Standard).

## 5. Befehle

| Befehl | Wirkung |
|---|---|
| `/status` | Pumpe (Soll/Ist, Modus), Netz, Licht, Wasserstand, Frostschutz, Winterbetrieb |
| `/pumpe 60` | 60 % für `manual_duration_min`, dann Zeitplan |
| `/pumpe 60 30` | 60 % für 30 Minuten; `/pumpe 60 0` ohne Zeitlimit |
| `/aus` | Pumpe aus (0 %), Zeitlimit wie oben; `/aus 0` ohne Zeitlimit |
| `/zeitplan` | Zurück zum Zeitplan |
| `/licht an`, `/licht aus` | Beleuchtung |
| `/rueckspuelen` | Rückspülung, **nach Bestätigung** per Knopf |
| `/abschalten` | Alles abschalten und verriegeln (wie im Dashboard), **nach Bestätigung** |
| `/notiz E-04` | Pumpenfehler mit aktuellem Status ins Log (wie im Dashboard) |
| `/stumm` | Leise Meldungen aus, bis `/laut`. Warnungen und Fehler kommen weiter. |
| `/stumm 3` | Leise Meldungen für 3 Stunden aus (1–168), danach automatisch wieder an |
| `/laut` | Leise Meldungen wieder an, mit Anzahl der unterdrückten |
| `/hilfe` | Befehlsübersicht |

`/stumm`, `/laut`, `/status` und `/hilfe` gehen auch mit `commands_enabled: false`.
Stumm gilt für alle Nutzer und bis zum nächsten Neustart des Dienstes.

Es gelten dieselben Regeln wie im Dashboard: Der Frostschutz hat Vorrang, bei
aktiver Abschaltung wird alles abgelehnt, und während einer Rückspülung gibt
es keine Drehzahlbefehle.

**Bewusst nicht per Telegram möglich:** Abschaltung quittieren bzw. Pumpe
wieder ans Netz nehmen. Wer gerade an der Pumpe arbeitet, darf nicht aus der
Ferne überrascht werden (ROADMAP Punkt 1). Das geht nur am Dashboard im LAN.

## 6. Sicherheit

- Befehle nur von `allowed_user_ids` und nur im privaten Chat. Fremde bekommen
  keine Antwort. Im Log erscheint `Telegram: Nachricht von nicht freigegebenem
  Nutzer …`, höchstens einmal pro Stunde und Nutzer, und die Meldung geht auch
  an dich.
- Jeder Befehl steht im Log (`event.name = telegram.command`, mit Nutzer-ID).
- Wer dein Telegram-Konto übernimmt, kann den Pool steuern. Deshalb in Telegram
  unter *Einstellungen → Datenschutz → Zweistufige Bestätigung* ein Passwort
  setzen.
- Der Token steht in keinem Log. Die Anfrage-Logs von httpx werden gedämpft,
  Fehlertexte werden bereinigt.

## 7. Fehlersuche

| Log-Meldung | Ursache / Abhilfe |
|---|---|
| `Telegram deaktiviert: POOL_TELEGRAM_TOKEN fehlt` | Token nicht in `/etc/pool_marx/secrets.env` oder Override nicht aktiv |
| `Telegram deaktiviert: telegram.allowed_user_ids ist leer` | Nutzer-ID eintragen (Abschnitt 3) |
| `Telegram deaktiviert: Bot-Token ungültig (HTTP 401)` | Token falsch kopiert oder bei BotFather neu erzeugt |
| `Telegram nicht erreichbar: …` | Kein Internet / DNS / Firewall. Wird automatisch wiederholt (5 s bis 60 s), danach `Telegram wieder erreichbar` |
| `HTTP 409: Conflict` | Ein zweiter Prozess fragt denselben Bot ab (z. B. Test-Instanz auf dem Mac mit demselben Token). Nur eine Instanz pro Bot. |
| Bot antwortet nicht, Log zeigt `nicht freigegebenem Nutzer` | Falsche ID in `allowed_user_ids` |
| `Befehl ignoriert — er ist … Minuten alt` | Pi war offline; Befehl neu senden |
