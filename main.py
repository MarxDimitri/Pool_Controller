"""
Pool Marx — Hauptprogramm
Startet alle Services: Hardware, Pumpe, MQTT, API
"""

import asyncio
import logging
import logging.handlers
import os
import signal
import socket
import sys
import time
import yaml
import uvicorn
from pathlib import Path

# Eigene Module
sys.path.insert(0, str(Path(__file__).parent))
from services.hardware import HardwareController
from services.pump_service import PumpService
from services.light_service import LightService
from services.mqtt_service import MQTTService
from services.telemetry import setup_telemetry
from services.telegram_service import TelegramService
from services.version import __version__
from api.main import create_app


def setup_logging(config: dict):
    """Logging konfigurieren"""
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO"))

    # Log-Verzeichnis erstellen
    log_file = Path(log_config.get("file", "/tmp/pool_marx.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    handlers = [
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=log_config.get("max_bytes", 10485760),
            backupCount=log_config.get("backup_count", 5)
        )
    ]

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers
    )


def sd_notify(message: str) -> bool:
    """Nachricht an systemd senden (READY=1, WATCHDOG=1). Ohne systemd: no-op."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]   # Abstract Namespace
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
        return True
    except OSError as e:
        logging.getLogger("pool_marx").error(f"sd_notify fehlgeschlagen: {e}")
        return False


async def systemd_watchdog(pump_service, max_heartbeat_age_s: float = 180):
    """
    Meldet systemd regelmäßig "lebendig" — aber nur, solange der Event-Loop
    läuft UND die Zeitplan-Schleife des PumpService tickt. Bleibt die Meldung
    aus, startet systemd den Dienst neu (WatchdogSec= in der Unit).
    """
    logger = logging.getLogger("pool_marx")
    sd_notify("READY=1")
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return
    interval = int(usec) / 1_000_000 / 2
    logger.info(f"systemd-Watchdog aktiv (Meldung alle {interval:.0f}s)")
    while True:
        age = time.monotonic() - pump_service.last_heartbeat
        if age < max_heartbeat_age_s:
            sd_notify("WATCHDOG=1")
        else:
            logger.critical(f"PumpService-Heartbeat seit {age:.0f}s aus — kein Watchdog-Ping")
        await asyncio.sleep(interval)


# Secrets stehen NICHT in config.yaml, sondern kommen aus Umgebungsvariablen.
# Auf dem Pi setzt systemd sie aus /etc/pool_marx/secrets.env
# (EnvironmentFile=, nur für root lesbar) — Vorlage: pi/secrets.env.example
SECRET_ENV = {
    ("mqtt", "password"):   "POOL_MQTT_PASSWORD",
    ("api",  "secret_key"): "POOL_API_SECRET",
    ("telegram", "token"):  "POOL_TELEGRAM_TOKEN",
}
OPTIONAL_ENV = {
    ("mqtt", "username"):   "POOL_MQTT_USERNAME",
}


def load_config() -> dict:
    config_path = Path(__file__).parent / "config" / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def apply_env_secrets(config: dict) -> list[str]:
    """
    Secrets aus der Umgebung in die Config übernehmen.
    Gibt die Schlüssel zurück, die noch (veraltet) in config.yaml stehen.
    """
    legacy = []
    for (section, key), env in {**SECRET_ENV, **OPTIONAL_ENV}.items():
        sect = config.setdefault(section, {}) or {}
        config[section] = sect
        value = os.environ.get(env)
        if value:
            sect[key] = value
        elif (section, key) in SECRET_ENV and sect.get(key):
            legacy.append(f"{section}.{key}")
    return legacy


async def main() -> int:
    # Konfiguration laden
    config = load_config()
    legacy_secrets = apply_env_secrets(config)
    setup_logging(config)
    logger = logging.getLogger("pool_marx")
    logger.info("=" * 50)
    logger.info(f"Pool Marx Steuerung startet — Version {__version__}")
    logger.info("=" * 50)
    for key in legacy_secrets:
        logger.warning(
            f"Secret '{key}' steht noch in config.yaml — bitte nach "
            f"/etc/pool_marx/secrets.env verschieben und aus config.yaml löschen"
        )

    # Telemetrie (OpenTelemetry, optional). Wirft nie; bei Fehler/aus → No-op.
    # Endpunkt/API-Key nur aus OTEL_EXPORTER_OTLP_* (/etc/pool_marx/telemetry.env)
    telemetry = setup_telemetry(config)

    # Hardware initialisieren
    hardware = HardwareController(config)

    # MQTT Service
    async def command_handler(device: str, payload: str):
        """Empfängt MQTT Befehle und leitet sie weiter"""
        if device == "pump":
            await pump_service.handle_mqtt_command(payload)
        elif device == "light":
            await light_service.handle_mqtt_command(payload)

    mqtt = MQTTService(config, command_handler)

    # Pump Service mit MQTT Callback
    async def on_state_change(device: str, state: dict):
        """Statusänderungen an MQTT publizieren"""
        if device == "pump":
            await mqtt.publish_pump_state(state)
        elif device == "light":
            await mqtt.publish_light_state(state)

    pump_service = PumpService(hardware, config, on_state_change)
    # Beleuchtung: Zeitplan / manuell, Dimmer (vorerst simuliert)
    light_service = LightService(hardware, config, pump_service, on_state_change)

    # Telemetrie liest Zustände per Callback (Metrik-Thread) — keine Aufrufe
    # aus DAC-Thread oder Zeitplan-Schleife
    telemetry.register_sources(hardware=hardware, pump_service=pump_service)
    hardware.set_write_duration_hook(telemetry.record_dac_write_ms)
    pump_service.telemetry = telemetry

    # Telegram (optional): Benachrichtigungen + Befehle, ruft pump_service
    # direkt auf (gleiche Sperren wie Dashboard). Verbindung nur ausgehend.
    telegram = TelegramService(config, pump_service, hardware, light=light_service)

    # FastAPI App
    api_config = config.get("api", {})
    app = create_app(pump_service, mqtt, hardware, api_config, light_service=light_service)
    telemetry.instrument_fastapi(app)   # nur wenn telemetry.fastapi_instrumentation: true
    port = api_config.get("port", 8080)

    # Uvicorn läuft im selben Event-Loop wie PumpService/MQTT (kein Thread):
    # keine Thread-Konflikte, und ein Absturz der Automatik beendet den
    # Prozess, damit systemd (Restart=always) neu startet.
    server = uvicorn.Server(uvicorn.Config(
        app,
        host=api_config.get("host", "0.0.0.0"),
        port=port,
        log_level="warning",
    ))

    # SIGTERM/SIGINT → Uvicorn geordnet beenden, danach Cleanup unten.
    # (Uvicorn installiert während serve() eigene Handler und löst das
    # Signal danach erneut aus — dann landet es harmlos hier.)
    def request_stop(signum, frame):
        logger.info("Shutdown Signal empfangen")
        server.should_exit = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT,  request_stop)

    logger.info(f"API startet auf Port {port}")
    pump_task = asyncio.create_task(pump_service.start(), name="pump")
    api_task = asyncio.create_task(server.serve(), name="api")
    mqtt_task = asyncio.create_task(mqtt.start(), name="mqtt")
    watchdog_task = asyncio.create_task(systemd_watchdog(pump_service), name="watchdog")
    telegram_task = asyncio.create_task(telegram.start(), name="telegram")
    light_task = asyncio.create_task(light_service.start(), name="light")

    done, _ = await asyncio.wait({pump_task, api_task}, return_when=asyncio.FIRST_COMPLETED)

    exit_code = 0
    if pump_task in done:
        exit_code = 1
        exc = pump_task.exception()
        logger.critical("PumpService beendet — Prozess wird beendet", exc_info=exc)
    elif api_task.exception():
        exit_code = 1
        logger.critical("API-Server abgestürzt", exc_info=api_task.exception())

    # Geordnet herunterfahren
    server.should_exit = True
    pump_service._running = False
    light_service.stop()
    for task in (pump_task, mqtt_task, watchdog_task, telegram_task, light_task):
        task.cancel()
    try:
        await asyncio.wait_for(api_task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        pass
    await asyncio.gather(pump_task, mqtt_task, watchdog_task, telegram_task, light_task,
                         return_exceptions=True)
    await telegram.stop(
        flush_timeout_s=3.0,
        final_message=("🛑 Pool Marx beendet sich nach einem Fehler — systemd startet neu"
                       if exit_code else "⏹ Pool Marx wird beendet"),
    )

    hardware.cleanup()
    logger.info(f"Pool Marx beendet (Exit-Code {exit_code})")
    telemetry.shutdown(timeout_s=3.0)   # Flush mit hartem Zeitlimit
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
