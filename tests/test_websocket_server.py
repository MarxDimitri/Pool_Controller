"""/ws über einen echten uvicorn-Server (nicht TestClient).

Der TestClient braucht keine WebSocket-Bibliothek, uvicorn schon. Fehlt sie im
venv, antwortet /ws mit 404 und das Dashboard zeigt dauerhaft „getrennt“.
"""

import json
import re
import socket
import threading
import time

import pytest
import uvicorn

import api.main as api_main
from services.hardware import HardwareController
from services.pump_service import PumpService
from tests.conftest import API_SECRET, ROOT, FlakyBus, RecordingGPIO


def test_requirements_list_websocket_library():
    names = {re.split(r"[\s;=<>\[]", line.strip(), maxsplit=1)[0].lower()
             for line in (ROOT / "requirements.txt").read_text().splitlines()
             if line.strip() and not line.startswith("#")}
    assert names & {"websockets", "wsproto"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_websocket_over_real_uvicorn(config):
    ws_client = pytest.importorskip("websockets.sync.client")
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=FlakyBus(), start_thread=False)
    app = api_main.create_app(PumpService(hw, config), object(), hw, {"secret_key": API_SECRET})
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn startet nicht"
            time.sleep(0.05)
        with ws_client.connect(f"ws://127.0.0.1:{port}/ws",
                               additional_headers={"Authorization": f"Bearer {API_SECRET}"},
                               open_timeout=5) as ws:
            msg = json.loads(ws.recv(timeout=5))
        assert msg["type"] == "init" and "pump" in msg["data"]
    finally:
        server.should_exit = True
        t.join(timeout=10)
