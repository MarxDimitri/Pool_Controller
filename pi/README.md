# pi/ — alles, was auf den Raspberry Pi kommt

Diese Dateien laufen auf dem Pi oder werden dort installiert. Auf dem Mac
rufst du hier nichts direkt auf (dafür: [`release/`](../release/)).

| Datei | Zweck |
|---|---|
| `install-release.sh` | Version installieren/umschalten/zurückrollen. Wird von `release/deploy.sh` aufgerufen |
| `pool_marx.override.conf` | systemd-Ergänzung (Secrets, Watchdog) |
| `secrets.env.example`, `telemetry.env.example`, `otelcol.env.example` | Vorlagen für `/etc/pool_marx/*.env` |
| `mcp4725_set_default_zero.py` | Einmalig: Startwert des DAC auf 0 V |
| `otelcol-pi.yaml`, `otelcol-pi.service` | Optionaler OpenTelemetry-Collector |
