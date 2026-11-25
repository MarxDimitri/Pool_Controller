"""
Pool Marx — FastAPI REST Backend
Stellt API für Web-App und externe Systeme bereit
"""

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Optional
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import (
    Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
)
from fastapi import Path as ApiPath
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from services.light_service import LightService
from services.pump_service import BackwashBusy, EmergencyActive
from services.version import __version__

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

SESSION_COOKIE = "pool_session"
SESSION_MAX_AGE_S = 365 * 24 * 3600
# Platzhalter aus Repo/Vorlagen gelten als "nicht gesetzt" (öffentlich bekannt)
PLACEHOLDER_SECRETS = {"", "pool_marx_secret_2026", "change_me", "bitte_aendern"}

# ── Pydantic Models ──────────────────────────────────────────

class PumpCommand(BaseModel):
    action: str           # "on", "off", "speed"
    speed_pct: Optional[int] = Field(60, ge=0, le=100)
    duration_min: Optional[int] = Field(None, ge=0, le=24 * 60)

class FaultNote(BaseModel):
    # Freitext wird im Service auf 500 Zeichen gekürzt; extrem lange Eingaben → 422
    text: Optional[str] = Field(None, max_length=5000)

class LightCommand(BaseModel):
    action: str           # "on", "off", "brightness", "schedule", "manual"
    brightness_pct: Optional[int] = Field(None, ge=0, le=100)

class LoginRequest(BaseModel):
    password: str

# ── WebSocket Manager ─────────────────────────────────────────

class ConnectionManager:
    """Verwaltet WebSocket-Verbindungen für Live-Updates"""

    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        logger.info(f"WebSocket verbunden. Aktiv: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        logger.info(f"WebSocket getrennt. Aktiv: {len(self.active)}")

    async def broadcast(self, data: dict):
        """Statusänderung an alle verbundenen Clients senden"""
        dead = []
        for ws in list(self.active):   # Kopie: Liste kann sich während await ändern
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()

# Intervall, in dem der Watcher Zustandsänderungen erkennt, die nicht über
# die API ausgelöst wurden (Zeitplan, Frostschutz, Rückspülung, MQTT, Schwimmer)
STATE_WATCH_INTERVAL_S = 1.0


def create_app(pump_service, mqtt_service, hardware, api_config: Optional[dict] = None,
               light_service: Optional[LightService] = None) -> FastAPI:
    """FastAPI App mit allen Pool-Routen erstellen"""
    if light_service is None:   # ohne Zeitplan (Tests, Skripte): nur manuelle Befehle
        light_service = LightService(hardware, {}, pump_service)

    # ── Authentifizierung ─────────────────────────────────────
    # Passwort = POOL_API_SECRET (Umgebung, siehe main.py). Login setzt ein HttpOnly-
    # Cookie (SameSite=Strict) → fremde Webseiten können keine Befehle
    # auslösen. Skripte können alternativ "Authorization: Bearer <secret_key>"
    # senden.

    secret = str((api_config or {}).get("secret_key") or "")
    auth_configured = secret not in PLACEHOLDER_SECRETS
    if not auth_configured:
        logger.error(
            "POOL_API_SECRET fehlt oder ist ein Platzhalter — Web-Steuerung "
            "gesperrt. Bitte in /etc/pool_marx/secrets.env setzen."
        )
    elif len(secret) < 12:
        logger.warning("POOL_API_SECRET ist kürzer als 12 Zeichen — bitte verlängern")

    session_token = hmac.new(
        secret.encode(), b"pool-marx-session-v1", hashlib.sha256
    ).hexdigest()

    def is_authenticated(cookies, headers) -> bool:
        if not auth_configured:
            return False
        cookie = cookies.get(SESSION_COOKIE, "")
        if cookie and hmac.compare_digest(cookie, session_token):
            return True
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return hmac.compare_digest(auth[7:].strip(), secret)
        return False

    def same_origin(headers) -> bool:
        """Origin-Header (falls vorhanden) muss zum Host passen"""
        origin = headers.get("origin")
        if not origin:
            return True   # z.B. curl / Skripte
        return urlparse(origin).netloc == headers.get("host", "")

    async def require_auth(request: Request):
        if not auth_configured:
            raise HTTPException(503, "Kein Dashboard-Passwort gesetzt (POOL_API_SECRET)")
        if request.method != "GET" and not same_origin(request.headers):
            raise HTTPException(403, "Fremde Herkunft")
        if not is_authenticated(request.cookies, request.headers):
            raise HTTPException(401, "Nicht angemeldet")

    auth = [Depends(require_auth)]

    # ── Live-Zustand ──────────────────────────────────────────

    def state_snapshot() -> dict:
        """Kompletter Live-Zustand für alle Web-Clients"""
        return {
            "pump": pump_service.get_state(),
            "hardware": hardware.get_all_states(),
            "mains": hardware.pump_mains_connected,
            "protection": pump_service.protection_status(),
            "light": light_service.get_state(),
        }

    last_pushed: dict = {}

    async def push_state():
        """Aktuellen Zustand an alle Clients senden (nur bei Änderung)"""
        snapshot = state_snapshot()
        if snapshot == last_pushed.get("state"):
            return
        last_pushed["state"] = snapshot
        await manager.broadcast({"type": "state", "data": snapshot})

    async def watch_state():
        """
        Zustand regelmäßig vergleichen und Änderungen an alle Geräte pushen —
        erfasst auch Änderungen durch Zeitplan, Frostschutz, Rückspülung,
        MQTT und Schwimmerschalter.
        """
        while True:
            try:
                if manager.active:
                    await push_state()
            except Exception as e:
                logger.error(f"State-Watcher Fehler: {e}")
            await asyncio.sleep(STATE_WATCH_INTERVAL_S)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        watcher = asyncio.create_task(watch_state())
        logger.info("Pool Marx API gestartet")
        yield
        watcher.cancel()
        logger.info("Pool Marx API beendet")

    app = FastAPI(
        title="Pool Marx Steuerung",
        description="Pool Automatisierung",
        version=__version__,
        lifespan=lifespan,
        # Swagger/OpenAPI aus: wäre eine zweite, ungeschützte Bedienoberfläche
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Kein CORS: Dashboard wird vom selben Server ausgeliefert (same origin)

    @app.exception_handler(EmergencyActive)
    async def on_emergency_active(request: Request, exc: EmergencyActive):
        return JSONResponse({"detail": str(exc)}, status_code=423)

    @app.exception_handler(BackwashBusy)
    async def on_backwash_busy(request: Request, exc: BackwashBusy):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    # ── Login ─────────────────────────────────────────────────

    @app.post("/api/login")
    async def login(body: LoginRequest, request: Request, response: Response):
        if not auth_configured:
            raise HTTPException(503, "Kein Dashboard-Passwort gesetzt (POOL_API_SECRET)")
        if not same_origin(request.headers):
            raise HTTPException(403, "Fremde Herkunft")
        if not hmac.compare_digest(body.password.encode(), secret.encode()):
            await asyncio.sleep(1)   # Bremst Durchprobieren
            logger.warning(f"Fehlgeschlagener Login von {request.client.host if request.client else '?'}")
            raise HTTPException(401, "Falsches Passwort")
        response.set_cookie(
            SESSION_COOKIE, session_token,
            max_age=SESSION_MAX_AGE_S, httponly=True, samesite="strict",
        )
        return {"success": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(SESSION_COOKIE)
        return {"success": True}

    @app.get("/api/session")
    async def session(request: Request):
        return {
            "authenticated": is_authenticated(request.cookies, request.headers),
            "configured": auth_configured,
        }

    # ── Status Endpoints ──────────────────────────────────────

    @app.get("/api/status", dependencies=auth)
    async def get_status():
        """Kompletter Systemstatus"""
        hw_state = hardware.get_all_states()
        pump_state = pump_service.get_state()
        return {
            "timestamp": datetime.now().isoformat(),
            "version": __version__,
            "pump": pump_state,
            "hardware": hw_state,
            "protection": pump_service.protection_status(),
            "light": light_service.get_state(),
            "pool": {
                "name": "Pool Marx",
                "volume_m3": 42,
            }
        }

    @app.get("/api/pump", dependencies=auth)
    async def get_pump():
        """Pumpenstatus"""
        return pump_service.get_state()

    # ── Pump Endpoints ────────────────────────────────────────

    @app.post("/api/pump/on", dependencies=auth)
    async def pump_on(cmd: PumpCommand):
        """Pumpe einschalten"""
        speed = 60 if cmd.speed_pct is None else cmd.speed_pct
        await pump_service.manual_on(speed, cmd.duration_min)
        state = pump_service.get_state()
        await push_state()
        return {"success": True, "state": state}

    @app.post("/api/pump/off", dependencies=auth)
    async def pump_off():
        """Pumpe ausschalten"""
        await pump_service.manual_off()
        state = pump_service.get_state()
        await push_state()
        return {"success": True, "state": state}

    @app.post("/api/pump/speed/{speed_pct}", dependencies=auth)
    async def pump_speed(speed_pct: int = ApiPath(..., ge=0, le=100)):
        """Pumpen-Drehzahl setzen (0-100%, sonst 422)"""
        await pump_service.manual_on(speed_pct)
        await push_state()
        return {"success": True, "speed_pct": pump_service.get_state()["speed_pct"]}

    @app.post("/api/pump/schedule", dependencies=auth)
    async def pump_schedule():
        """Zurück zum automatischen Zeitplan"""
        pump_service.resume_schedule()
        await push_state()
        return {"success": True, "mode": pump_service.get_state()["mode"]}

    @app.post("/api/pump/fault-note", dependencies=auth)
    async def pump_fault_note(note: Optional[FaultNote] = None):
        """
        Pumpenfehler (z. B. E-04) notieren: WARNING-Log mit aktuellem Pumpenstatus
        (Modus, Soll, Ist, letzte Änderung) + optionalem Freitext — zur
        Korrelation mit Zeitplanwechseln in den Logs/Elastic.
        """
        status = pump_service.record_fault_note(note.text if note else None)
        return {"success": True, "status": status}

    # ── Rückspülung ───────────────────────────────────────────

    @app.post("/api/backwash/start", dependencies=auth)
    async def start_backwash():
        """Rückspül-Sequenz starten"""
        try:
            pump_service.start_backwash()
        except ValueError as e:
            raise HTTPException(409, str(e))
        await push_state()
        return {"success": True, "message": "Rückspülung gestartet"}

    @app.post("/api/backwash/cancel", dependencies=auth)
    async def cancel_backwash(resume_schedule: bool = False):
        """Laufende Rückspülung abbrechen (optional direkt zurück zum Zeitplan)"""
        await pump_service.cancel_backwash(resume_schedule)
        await push_state()
        return {"success": True, "mode": pump_service.get_state()["mode"]}

    # ── Beleuchtung ───────────────────────────────────────────

    def light_response() -> dict:
        state = light_service.get_state()
        return {"success": True, "light": state["on"], "state": state}

    @app.get("/api/light", dependencies=auth)
    async def get_light():
        """Beleuchtung: an/aus, Helligkeit, Modus, nächste Zeitplan-Änderung"""
        return light_service.get_state()

    @app.post("/api/light", dependencies=auth)
    async def control_light(cmd: LightCommand):
        """
        Beleuchtung steuern. on/off/brightness schalten auf manuell,
        schedule → zurück zum Zeitplan, manual → Zeitplan pausieren.
        """
        try:
            if cmd.action == "on":
                light_service.turn_on(cmd.brightness_pct)
            elif cmd.action == "off":
                light_service.turn_off()
            elif cmd.action == "brightness":
                if cmd.brightness_pct is None:
                    raise HTTPException(400, "brightness_pct fehlt")
                light_service.set_brightness(cmd.brightness_pct)
            elif cmd.action == "schedule":
                light_service.resume_schedule()
            elif cmd.action == "manual":
                light_service.set_manual()
            else:
                raise HTTPException(400, f"Unbekannte Aktion: {cmd.action}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        await push_state()
        return light_response()

    @app.post("/api/light/brightness/{pct}", dependencies=auth)
    async def light_brightness(pct: int = ApiPath(..., ge=0, le=100)):
        """Helligkeit 0–100 % (0 = aus), schaltet auf manuell"""
        light_service.set_brightness(pct)
        await push_state()
        return light_response()

    @app.post("/api/light/schedule", dependencies=auth)
    async def light_schedule():
        """Beleuchtung zurück zum Zeitplan"""
        light_service.resume_schedule()
        await push_state()
        return light_response()

    # ── Not-Aus (Software, verriegelt) ────────────────────────

    @app.post("/api/emergency_stop", dependencies=auth)
    async def emergency_stop():
        """Alle Verbraucher abschalten und Automatik sperren bis zur Quittierung"""
        pump_service.emergency_stop()
        logger.warning("NOTAUS über API ausgelöst")
        await manager.broadcast({"type": "emergency_stop"})
        await push_state()
        return {"success": True, "message": "Notaus ausgeführt"}

    @app.post("/api/emergency/ack", dependencies=auth)
    async def acknowledge_emergency():
        """Not-Aus quittieren: Pumpe wieder ans Netz, Pumpe bleibt aus"""
        pump_service.acknowledge_emergency()
        await push_state()
        return {"success": True, "mains_connected": hardware.pump_mains_connected}

    @app.post("/api/pump/mains/restore", dependencies=auth)
    async def restore_pump_mains():
        """Alias für /api/emergency/ack (Kompatibilität)"""
        return await acknowledge_emergency()

    @app.get("/api/pump/mains", dependencies=auth)
    async def get_pump_mains():
        """Status: liegt Netzspannung an der Pumpe an?"""
        return {"mains_connected": hardware.pump_mains_connected}

    # ── WebSocket für Live-Updates ────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """
        WebSocket für Echtzeit-Updates an die Web-App.
        Sendet Statusänderungen sofort nach Auslösung.
        """
        if not same_origin(websocket.headers) or not is_authenticated(
                websocket.cookies, websocket.headers):
            await websocket.close(code=1008)
            return
        await manager.connect(websocket)
        try:
            # Ersten Status sofort senden
            await websocket.send_json({"type": "init", "data": state_snapshot()})
            # Verbindung offen halten
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)

    # ── Health Check ──────────────────────────────────────────

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "version": __version__
        }

    # ── Web-Dashboard ─────────────────────────────────────────
    # Muss NACH allen /api- und /ws-Routen registriert werden,
    # sonst schattet der Mount auf "/" sie ab.
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app
