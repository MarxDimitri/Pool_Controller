"""A6 — API (FastAPI TestClient), WebSocket, statische Auslieferung"""

import logging
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import api.main as api_main
from services.hardware import HardwareController
from services.pump_service import PumpService
from tests.conftest import AUTH, API_SECRET, FlakyBus, RecordingGPIO, events_of


class DummyMQTT:
    pass


@pytest.fixture
def stack(config):
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=FlakyBus(), start_thread=True)
    svc = PumpService(hw, config)
    app = api_main.create_app(svc, DummyMQTT(), hw, {"secret_key": API_SECRET})
    yield app, svc, hw
    hw.cleanup()


@pytest.fixture
def client(stack):
    app, *_ = stack
    with TestClient(app) as c:
        yield c


def test_health_without_auth(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.mark.parametrize("method,path", [
    ("get", "/api/status"), ("get", "/api/pump"), ("post", "/api/pump/off"),
    ("post", "/api/emergency_stop"), ("get", "/api/pump/mains"), ("post", "/api/pump/fault-note"),
])
def test_auth_required(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_status_and_pump(client):
    st = client.get("/api/status", headers=AUTH).json()
    assert st["pump"]["mode"] == "off" and "actual_speed_pct" in st["hardware"]["pump"]
    assert st["protection"]["frost"]["state"] in ("ready", "sensor_missing", "active", "disabled")
    assert st["protection"]["min_speed"]["mode"] == "winter"
    assert client.get("/api/pump", headers=AUTH).json()["speed_pct"] == 0


def test_pump_on_off_speed_schedule(client, stack):
    _, svc, hw = stack
    r = client.post("/api/pump/on", json={"action": "on", "speed_pct": 70}, headers=AUTH)
    assert r.status_code == 200 and hw.pump_speed == 70
    r = client.post("/api/pump/on", json={"action": "on", "speed_pct": 0}, headers=AUTH)
    assert hw.pump_speed == 0                     # 0 bleibt 0 (früher: 60)
    r = client.post("/api/pump/on", json={"action": "on"}, headers=AUTH)
    assert hw.pump_speed == 60
    assert client.post("/api/pump/speed/45", headers=AUTH).json()["speed_pct"] == 45
    assert client.post("/api/pump/off", headers=AUTH).json()["state"]["speed_pct"] == 0
    assert client.post("/api/pump/schedule", headers=AUTH).json()["mode"] in ("schedule", "off")


@pytest.mark.parametrize("value", ["-1", "101", "abc", "99999999999999999999", "1e3", "50.5"])
def test_invalid_speed_rejected_state_unchanged(client, stack, value):
    _, svc, hw = stack
    client.post("/api/pump/speed/40", headers=AUTH)
    before = (svc.get_state(), hw.pump_speed)
    r = client.post(f"/api/pump/speed/{value}", headers=AUTH)
    assert r.status_code in (400, 422)
    assert (svc.get_state(), hw.pump_speed) == before


@pytest.mark.parametrize("body", [
    {"action": "on", "speed_pct": 101}, {"action": "on", "speed_pct": -1},
    {"action": "on", "speed_pct": "x"}, {"action": "on", "duration_min": -5},
])
def test_invalid_pump_on_body(client, stack, body):
    _, _, hw = stack
    assert client.post("/api/pump/on", json=body, headers=AUTH).status_code == 422
    assert hw.pump_speed == 0


def test_backwash_start_cancel(client, stack):
    _, svc, _ = stack
    assert client.post("/api/backwash/start", headers=AUTH).status_code == 200
    assert svc.backwash_running
    assert client.post("/api/backwash/start", headers=AUTH).status_code == 409
    assert client.post("/api/backwash/cancel?resume_schedule=true", headers=AUTH).status_code == 200
    assert not svc.backwash_running


def test_light(client, stack):
    _, _, hw = stack
    assert client.post("/api/light", json={"action": "on"}, headers=AUTH).json()["light"] is True
    assert client.post("/api/light", json={"action": "off"}, headers=AUTH).json()["light"] is False
    assert client.post("/api/light", json={"action": "blink"}, headers=AUTH).status_code == 400


def test_emergency_and_mains_restore(client, stack):
    _, svc, hw = stack
    client.post("/api/pump/speed/60", headers=AUTH)
    assert client.post("/api/emergency_stop", headers=AUTH).status_code == 200
    assert hw.pump_actual_speed == 0 and hw._dac_bus.last_code == 0
    assert client.get("/api/pump/mains", headers=AUTH).json()["mains_connected"] is False
    assert client.post("/api/pump/speed/50", headers=AUTH).status_code == 423
    assert client.post("/api/light", json={"action": "on"}, headers=AUTH).status_code == 423
    r = client.post("/api/pump/mains/restore", headers=AUTH)
    assert r.json()["mains_connected"] is True
    assert hw.pump_speed == 0                      # läuft erst nach neuem Befehl
    assert client.post("/api/emergency/ack", headers=AUTH).status_code == 200


def test_fault_note(client, stack, caplog):
    _, svc, hw = stack
    client.post("/api/pump/speed/40", headers=AUTH)
    long_text = "E-04 " + "x" * 3000 + "\nzweite Zeile"
    with caplog.at_level(logging.INFO):
        r = client.post("/api/pump/fault-note", json={"text": long_text}, headers=AUTH)
    assert r.status_code == 200
    st = r.json()["status"]
    assert st["mode"] == "manual" and st["setpoint_pct"] == 40 and st["last_change_reason"] == "manual"
    ev = events_of(caplog, "pump.fault_note")
    assert len(ev) == 1 and ev[0].levelno == logging.WARNING
    note = getattr(ev[0], "fault.note")
    assert note.startswith("E-04") and len(note) <= 501 and "\n" not in note
    assert getattr(ev[0], "pump.setpoint_pct") == 40
    assert "Soll=40%" in ev[0].getMessage()
    assert svc.fault_notes_count == 1
    # ohne Body und mit zu langem Body
    assert client.post("/api/pump/fault-note", headers=AUTH).status_code == 200
    assert client.post("/api/pump/fault-note", json={"text": "y" * 6000}, headers=AUTH).status_code == 422
    assert svc.fault_notes_count == 2


def test_login_session_logout(client):
    assert client.post("/api/login", json={"password": API_SECRET}).status_code == 200
    assert client.get("/api/session").json()["authenticated"] is True
    assert client.get("/api/status").status_code == 200        # Cookie
    client.post("/api/logout")
    client.cookies.clear()
    assert client.get("/api/session").json()["authenticated"] is False


def test_foreign_origin_rejected(client):
    r = client.post("/api/pump/off", headers={**AUTH, "Origin": "http://evil.example"})
    assert r.status_code == 403


# ── Static ────────────────────────────────────────────────────

def test_root_serves_index_and_api_not_swallowed(client):
    r = client.get("/")
    assert r.status_code == 200 and "Pool Marx" in r.text and "fault-note" in r.text
    assert client.get("/api/health").headers["content-type"].startswith("application/json")


def test_missing_web_dir_still_starts(config, monkeypatch, tmp_path):
    monkeypatch.setattr(api_main, "WEB_DIR", tmp_path / "gibt-es-nicht")
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=FlakyBus(), start_thread=False)
    app = api_main.create_app(PumpService(hw, config), DummyMQTT(), hw, {"secret_key": API_SECRET})
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/").status_code == 404


def test_no_secret_locks_control(config):
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=FlakyBus(), start_thread=False)
    app = api_main.create_app(PumpService(hw, config), DummyMQTT(), hw, {"secret_key": ""})
    with TestClient(app) as c:
        assert c.get("/api/status", headers=AUTH).status_code == 503


# ── WebSocket ─────────────────────────────────────────────────

def test_websocket_init_and_update(client):
    with client.websocket_connect("/ws", headers=AUTH) as ws:
        init = ws.receive_json()
        assert init["type"] == "init" and init["data"]["mains"] is True
        assert "frost" in init["data"]["protection"] and "min_speed" in init["data"]["protection"]
        client.post("/api/light", json={"action": "on"}, headers=AUTH)
        msg = ws.receive_json()
        while msg["type"] != "state" or not msg["data"]["hardware"]["light"]:
            msg = ws.receive_json()
        assert msg["data"]["hardware"]["light"] is True


def test_websocket_rejects_unauthenticated(client):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
    assert e.value.code == 1008


def test_websocket_multiple_clients_and_disconnect(client):
    with client.websocket_connect("/ws", headers=AUTH) as b:
        b.receive_json()
        with client.websocket_connect("/ws", headers=AUTH) as a:
            a.receive_json()
            n = len(api_main.manager.active)
            assert n >= 2
        # a ist getrennt (Verbindungsabbruch) → wird aus der Liste entfernt
        deadline = time.time() + 2
        while len(api_main.manager.active) >= n and time.time() < deadline:
            time.sleep(0.05)
        assert len(api_main.manager.active) == n - 1
        client.post("/api/light", json={"action": "on"}, headers=AUTH)
        msg = b.receive_json()                     # b bekommt weiter Updates
        assert msg["type"] == "state"


def test_ws_broadcast_drops_dead_clients():
    import asyncio

    class Dead:
        async def send_json(self, data):
            raise RuntimeError("weg")

    mgr = api_main.ConnectionManager()
    mgr.active.append(Dead())
    asyncio.run(mgr.broadcast({"x": 1}))
    assert mgr.active == []
