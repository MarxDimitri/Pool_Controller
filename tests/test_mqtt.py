"""MQTT-Service (ohne Broker: Fake-Client)"""

import asyncio
import json

import pytest

from services import mqtt_service
from services.mqtt_service import MQTTService
from tests.conftest import run


class FakeClient:
    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    async def publish(self, topic, payload=None, retain=False):
        if self.fail:
            raise RuntimeError("weg")
        self.published.append((topic, payload, retain))


def test_publish_pump_state_and_values(config):
    m = MQTTService(config)
    m._client = FakeClient()
    run(m.publish_pump_state({"running": True, "speed_pct": 40, "mode": "schedule",
                              "frost_active": False}))
    run(m.publish_temperature(12.345))
    run(m.publish_water_chemistry(7.234, 700))
    run(m.publish_level(False))
    run(m.publish("x", {"a": 1}))
    topics = dict((t, p) for t, p, _ in m._client.published)
    assert topics["pool_marx/pump/state"] == "on" and topics["pool_marx/pump/speed"] == "40"
    assert topics["pool_marx/temperature"] == "12.3" and topics["pool_marx/level"] == "low"
    assert json.loads(topics["pool_marx/x"]) == {"a": 1}


def test_publish_offline_and_errors_do_not_raise(config):
    m = MQTTService(config)
    run(m.publish("x", 1))                 # offline
    m._client = FakeClient(fail=True)
    run(m.publish("x", 1))                 # Fehler wird geloggt


def test_ha_discovery(config):
    m = MQTTService(config)
    c = FakeClient()
    run(m._send_ha_discovery(c))
    assert len(c.published) == 10 and all(r for _, _, r in c.published)
    cfg = {t: json.loads(p) for t, p, _ in c.published}
    # bestehender Licht-Schalter bleibt (gleiche unique_id), jetzt mit Status
    sw = cfg["homeassistant/switch/pool_marx_light/config"]
    assert sw["unique_id"] == "pool_marx_light" and sw["state_topic"] == "pool_marx/light/state"
    num = cfg["homeassistant/number/pool_marx_light_brightness/config"]
    assert num["command_template"] == "brightness:{{ value }}" and num["command_topic"] == "pool_marx/cmd/light"
    sel = cfg["homeassistant/select/pool_marx_light_mode/config"]
    assert sel["options"] == ["schedule", "manual"]


def test_publish_light_state(config):
    m = MQTTService(config)
    m._client = FakeClient()
    run(m.publish_light_state({"on": True, "brightness_pct": 40, "mode": "manual"}))
    topics = {t: (p, r) for t, p, r in m._client.published}
    assert topics["pool_marx/light/state"] == ("on", True)
    assert topics["pool_marx/light/brightness"] == ("40", True)
    assert topics["pool_marx/light/mode"] == ("manual", True)


def test_handle_message_routes_commands(config):
    got = []

    async def handler(device, payload):
        got.append((device, payload))
    m = MQTTService(config, handler)
    run(m._handle_message("pool_marx/cmd/pump", "on"))
    run(m._handle_message("pool_marx/state/pump", "x"))
    assert got == [("pump", "on")]


def test_start_without_aiomqtt(config, monkeypatch):
    monkeypatch.setattr(mqtt_service, "MQTT_AVAILABLE", False)
    run(MQTTService(config).start())       # kehrt sofort zurück
    run(MQTTService(config).stop())
