"""
Pool Marx — MQTT Service
Verbindet Pool-Controller mit Home Assistant via MQTT
"""

import asyncio
import json
import logging
from typing import Optional, Callable

from .version import __version__

logger = logging.getLogger(__name__)

try:
    import aiomqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    logger.warning("aiomqtt nicht installiert — MQTT deaktiviert")


class MQTTService:
    """
    Publiziert Pool-Messwerte und Zustände an Home Assistant.
    Empfängt Steuerbefehle via MQTT.
    """

    def __init__(self, config: dict, command_handler: Optional[Callable] = None):
        self.config = config["mqtt"]
        self.prefix = self.config["topic_prefix"]
        self.command_handler = command_handler
        self._client = None
        self._running = False

    def topic(self, suffix: str) -> str:
        return f"{self.prefix}/{suffix}"

    async def start(self):
        """MQTT Verbindung herstellen und Subscriptions einrichten"""
        if not MQTT_AVAILABLE:
            logger.warning("MQTT nicht verfügbar — überspringe")
            return

        self._running = True
        logger.info(
            f"MQTT verbindet zu {self.config['broker']}:{self.config['port']}"
        )

        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=self.config["broker"],
                    port=self.config["port"],
                    username=self.config.get("username"),
                    password=self.config.get("password"),
                ) as client:
                    self._client = client

                    # Alle Befehls-Topics abonnieren
                    await client.subscribe(f"{self.prefix}/cmd/#")
                    logger.info("MQTT verbunden und subscribed")

                    # Home Assistant Auto-Discovery senden
                    await self._send_ha_discovery(client)

                    # Nachrichten empfangen
                    async for message in client.messages:
                        await self._handle_message(
                            str(message.topic),
                            message.payload.decode()
                        )

            except Exception as e:
                logger.error(f"MQTT Fehler: {e} — reconnect in 10s")
                self._client = None
                await asyncio.sleep(10)

    async def stop(self):
        self._running = False

    async def _handle_message(self, topic: str, payload: str):
        """Eingehende MQTT Nachrichten verarbeiten"""
        logger.debug(f"MQTT empfangen: {topic} = {payload}")

        if self.command_handler:
            # Topic z.B. "pool_marx/cmd/pump" → device = "pump"
            parts = topic.split("/")
            if len(parts) >= 3 and parts[-2] == "cmd":
                device = parts[-1]
                await self.command_handler(device, payload)

    async def publish(self, subtopic: str, payload, retain: bool = False):
        """Wert an MQTT Broker publizieren"""
        if not self._client or not MQTT_AVAILABLE:
            logger.debug(f"MQTT publish (offline): {subtopic} = {payload}")
            return
        try:
            if isinstance(payload, dict):
                payload = json.dumps(payload)
            await self._client.publish(
                self.topic(subtopic),
                payload=str(payload),
                retain=retain
            )
        except Exception as e:
            logger.error(f"MQTT publish Fehler: {e}")

    async def publish_pump_state(self, state: dict):
        await self.publish("pump/state",   "on" if state["running"] else "off")
        await self.publish("pump/speed",   state["speed_pct"])
        await self.publish("pump/mode",    state["mode"])
        await self.publish("pump/frost",   state["frost_active"])

    async def publish_light_state(self, state: dict):
        await self.publish("light/state", "on" if state["on"] else "off", retain=True)
        await self.publish("light/brightness", state["brightness_pct"], retain=True)
        await self.publish("light/mode", state["mode"], retain=True)

    async def publish_temperature(self, temp_c: float):
        await self.publish("temperature", f"{temp_c:.1f}")

    async def publish_water_chemistry(self, ph: float, orp: int):
        await self.publish("ph",  f"{ph:.2f}")
        await self.publish("orp", str(orp))

    async def publish_level(self, level_ok: bool):
        await self.publish("level", "ok" if level_ok else "low")

    async def _send_ha_discovery(self, client):
        """
        Home Assistant MQTT Auto-Discovery Nachrichten senden.
        HA erkennt damit automatisch alle Pool-Entities.
        """
        base = "homeassistant"
        prefix = self.prefix

        # Pumpe — Switch
        await client.publish(
            f"{base}/switch/{prefix}_pump/config",
            json.dumps({
                "name": "Pool Pumpe",
                "unique_id": f"{prefix}_pump",
                "state_topic": f"{prefix}/pump/state",
                "command_topic": f"{prefix}/cmd/pump",
                "payload_on": "on",
                "payload_off": "off",
                "icon": "mdi:pump",
                "device": self._ha_device()
            }),
            retain=True
        )

        # Pumpe Drehzahl — Number
        await client.publish(
            f"{base}/number/{prefix}_pump_speed/config",
            json.dumps({
                "name": "Pool Pumpe Drehzahl",
                "unique_id": f"{prefix}_pump_speed",
                "state_topic": f"{prefix}/pump/speed",
                "command_topic": f"{prefix}/cmd/pump",
                "command_template": "speed:{{ value }}",
                "min": 0,
                "max": 100,
                "step": 5,
                "unit_of_measurement": "%",
                "icon": "mdi:speedometer",
                "device": self._ha_device()
            }),
            retain=True
        )

        # Wassertemperatur — Sensor
        await client.publish(
            f"{base}/sensor/{prefix}_temperature/config",
            json.dumps({
                "name": "Pool Wassertemperatur",
                "unique_id": f"{prefix}_temp",
                "state_topic": f"{prefix}/temperature",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "icon": "mdi:thermometer-water",
                "device": self._ha_device()
            }),
            retain=True
        )

        # pH-Wert — Sensor
        await client.publish(
            f"{base}/sensor/{prefix}_ph/config",
            json.dumps({
                "name": "Pool pH-Wert",
                "unique_id": f"{prefix}_ph",
                "state_topic": f"{prefix}/ph",
                "unit_of_measurement": "pH",
                "icon": "mdi:flask",
                "device": self._ha_device()
            }),
            retain=True
        )

        # ORP/Redox — Sensor
        await client.publish(
            f"{base}/sensor/{prefix}_orp/config",
            json.dumps({
                "name": "Pool ORP/Redox",
                "unique_id": f"{prefix}_orp",
                "state_topic": f"{prefix}/orp",
                "unit_of_measurement": "mV",
                "icon": "mdi:water-check",
                "device": self._ha_device()
            }),
            retain=True
        )

        # Frostschutz — Binary Sensor
        await client.publish(
            f"{base}/binary_sensor/{prefix}_frost/config",
            json.dumps({
                "name": "Pool Frostschutz",
                "unique_id": f"{prefix}_frost",
                "state_topic": f"{prefix}/pump/frost",
                "payload_on": "True",
                "payload_off": "False",
                "device_class": "cold",
                "icon": "mdi:snowflake-alert",
                "device": self._ha_device()
            }),
            retain=True
        )

        # Wasserstand — Binary Sensor
        await client.publish(
            f"{base}/binary_sensor/{prefix}_level/config",
            json.dumps({
                "name": "Pool Wasserstand",
                "unique_id": f"{prefix}_level",
                "state_topic": f"{prefix}/level",
                "payload_on": "ok",
                "payload_off": "low",
                "device_class": "moisture",
                "icon": "mdi:waves",
                "device": self._ha_device()
            }),
            retain=True
        )

        # Beleuchtung — Switch (unverändert, jetzt mit Status)
        await client.publish(
            f"{base}/switch/{prefix}_light/config",
            json.dumps({
                "name": "Pool Beleuchtung",
                "unique_id": f"{prefix}_light",
                "state_topic": f"{prefix}/light/state",
                "command_topic": f"{prefix}/cmd/light",
                "payload_on": "on",
                "payload_off": "off",
                "icon": "mdi:lightbulb-on",
                "device": self._ha_device()
            }),
            retain=True
        )

        # Beleuchtung Helligkeit — Number (Dimmer)
        await client.publish(
            f"{base}/number/{prefix}_light_brightness/config",
            json.dumps({
                "name": "Pool Beleuchtung Helligkeit",
                "unique_id": f"{prefix}_light_brightness",
                "state_topic": f"{prefix}/light/brightness",
                "command_topic": f"{prefix}/cmd/light",
                "command_template": "brightness:{{ value }}",
                "min": 0,
                "max": 100,
                "step": 5,
                "unit_of_measurement": "%",
                "icon": "mdi:brightness-6",
                "device": self._ha_device()
            }),
            retain=True
        )

        # Beleuchtung Modus — Select (Zeitplan / manuell)
        await client.publish(
            f"{base}/select/{prefix}_light_mode/config",
            json.dumps({
                "name": "Pool Beleuchtung Modus",
                "unique_id": f"{prefix}_light_mode",
                "state_topic": f"{prefix}/light/mode",
                "command_topic": f"{prefix}/cmd/light",
                "options": ["schedule", "manual"],
                "icon": "mdi:calendar-clock",
                "device": self._ha_device()
            }),
            retain=True
        )

        logger.info("Home Assistant Auto-Discovery Nachrichten gesendet")

    def _ha_device(self) -> dict:
        """Home Assistant Device Definition"""
        return {
            "identifiers": ["pool_marx"],
            "name": "Pool Marx",
            "manufacturer": "DIY",
            "model": "Raspberry Pi 4",
            "sw_version": __version__,
        }
