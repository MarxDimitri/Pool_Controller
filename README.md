# Pool Marx — Raspberry Pi pool controller

Automation for a ~42 m³ outdoor pool: a variable-speed pump driven by a 0–10 V
signal, daily schedule, frost protection, backwash, lighting, and a web dashboard.
Runs on a Raspberry Pi 4. Without Pi hardware it falls back to a simulation mode.

## Features

- **Pump control via 0–10 V only.** Commands set a setpoint. A background thread
  ramps to it at 5 %/s and rewrites the DAC every 0.2 s, which also restores the
  value after a DAC reset. Emergency stop goes to 0 V immediately.
- **Schedule** with named time windows. In winter months a minimum speed
  replaces 0 % so the water keeps circulating.
- **Frost protection** with hysteresis and minimum run time. It overrides manual
  commands.
- **Backwash sequence.** It waits until the pump has reached full speed before
  opening the valve.
- **Latched software stop.** It blocks all automation until acknowledged.
- **Web dashboard** (login, live updates via WebSocket), REST API, optional MQTT /
  Home Assistant discovery.
- **Diagnostics:** every setpoint change and schedule transition is logged with a
  reason. A "fault note" endpoint records pump errors together with the current state.
- **Optional telemetry** via OpenTelemetry (OTLP/HTTP), e.g. to Elastic. Off by
  default and never blocks the control loop.

## Hardware

```
Raspberry Pi 4 ──I²C──► MCP4725 DAC (0–3.3 V) ──► LM358, gain 3.2 ──► 0–10 V ──► Speck BADU Alpha Eco Soft
      │                                                                           (control mode 3 = 0–10 V)
      ├─GPIO──► 8-ch relay board (active-low)
      │           ├─ pump mains   (emergency / maintenance only)
      │           ├─ backwash solenoid valve
      │           ├─ fresh-water fill valve (logic planned)
      │           ├─ pH / chlorine dosing pumps (logic planned)
      │           └─ pool lighting
      ├─GPIO◄── float switch (water level)
      └─1-Wire◄─ DS18B20 temperature sensor (planned)
```

Design decision: the pump is **not** switched on and off via its 230 V relay,
because frequent mains cycling stresses the pump electronics. Start, stop and
speed all go through the 0–10 V signal. The relay is only for emergencies and
maintenance. 100 % is capped at 9.5 V (`dac.max_output_v`, hard limit 10 V). The
DAC code uses a measured two-point calibration of the amplifier.

Pin assignment, speed levels and schedule live in [`config/config.yaml`](config/config.yaml).

## Software

Single Python process, one asyncio event loop:

```
main.py
 ├─ api/main.py               FastAPI (uvicorn): REST, WebSocket, login, static dashboard (web/)
 ├─ services/pump_service.py  schedule, min speed, frost, backwash, manual mode, emergency latch
 ├─ services/hardware.py      GPIO relays, float switch, MCP4725 + ramp/refresh thread
 ├─ services/mqtt_service.py  optional MQTT + Home Assistant discovery
 ├─ services/telemetry.py     optional OpenTelemetry logs/metrics (background exporter threads)
 └─ services/events.py        structured log events (event.name + attributes)
```

- Secrets (dashboard password, MQTT password, telemetry API key) come from
  environment variables (systemd `EnvironmentFile`), never from `config.yaml`.
  Templates are in [`deploy/`](deploy/).
- Deployed as a systemd service with a watchdog. If a background task crashes,
  the process exits and systemd restarts it.
- `scripts/` contains a one-off tool that sets the MCP4725 power-on value to 0 V,
  plus an OpenTelemetry Collector config for the Pi.

## Quick start (simulation, no Pi needed)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q                  # ~230 tests, about 1 min
```

To run the service locally, set `logging.file` in `config/config.yaml` to a
writable path, then:

```bash
POOL_API_SECRET=choose-a-long-password .venv/bin/python main.py
# dashboard: http://localhost:8080
```

## Versions and releases

[Semantic Versioning](https://semver.org/) with annotated Git tags `vX.Y.Z`. The
single source of truth is [`services/version.py`](services/version.py). API
(`/api/health`, `/api/status`), dashboard footer, startup log, MQTT discovery and
telemetry all show this value. Changes are listed in [CHANGELOG.md](CHANGELOG.md).

How to cut and deploy a release: [RELEASE.md](RELEASE.md).

## Documentation (German)

- [docs/INBETRIEBNAHME.md](docs/INBETRIEBNAHME.md): installation on the Pi, configuration, calibration, operation, troubleshooting
- [docs/telemetry.md](docs/telemetry.md): OpenTelemetry / Elastic setup, dashboards, alert rules
- [docs/abnahme.md](docs/abnahme.md): acceptance checklist for the real hardware
- [ROADMAP.md](ROADMAP.md): open items (safety, hardware, software)

## Disclaimer

> [!WARNING]
> This is a private hobby project, published as-is for reference. It is
> provided **without any warranty**, express or implied, including fitness for
> a particular purpose. **Use at your own risk.** The author accepts no liability
> for damage to people, property, equipment or the environment.

- The project controls **mains-powered equipment, water and, potentially,
  pool chemicals**. Mistakes can cause electric shock, fire, flooding, pump damage
  or chlorine-gas hazards.
- Electrical installation and any work on 230 V must be done by a **qualified
  electrician** according to local regulations (e.g. DIN VDE 0100-702 for pools),
  including an RCD and a lockable isolator.
- The dashboard's "switch everything off" is a **software stop, not an emergency
  stop** in the regulatory sense, and no substitute for disconnecting power.
- Software and hardware are tested only on one specific setup. Your pump, wiring
  and configuration will differ. Verify voltages and behaviour yourself before
  connecting anything.
- Not affiliated with or endorsed by any of the manufacturers mentioned
  (e.g. Speck Pumpen, Raspberry Pi, Microchip). Product names are trademarks of
  their respective owners.
