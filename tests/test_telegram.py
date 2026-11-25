"""Telegram-Bot: Konfiguration, Log-Auswahl, Befehle, Sicherheit (ohne Netzwerk: Fake-API)"""

import asyncio
import copy
import logging
from datetime import datetime

import httpx
import pytest

import main as main_mod
from services import telegram_service as tg
from services.telegram_service import (TELEGRAM_DEFAULTS, HttpxApi, TelegramApiError,
                                       TelegramNotifyHandler, TelegramService,
                                       load_telegram_config)
from services.version import __version__
from tests.conftest import FakeClock, events_of, load_real_config, run

USER = 111111
OTHER = 222222
TOKEN = "123456:SECRET-token-value"
NOW = 1_800_000_000.0
WALL = datetime(2026, 7, 1, 10, 0)


class FakeApi:
    """Bot-API-Double: zeichnet Aufrufe auf, Fehler/Antworten pro Methode einstellbar."""

    def __init__(self):
        self.calls = []
        self.fail = {}          # method → TelegramApiError
        self.updates = []       # Listen, die getUpdates nacheinander liefert
        self.closed = False

    async def __call__(self, method, payload, read_timeout=None):
        self.calls.append((method, payload))
        await asyncio.sleep(0.001)
        if method in self.fail:
            raise self.fail[method]
        if method == "getMe":
            return {"username": "pool_test_bot"}
        if method == "getUpdates":
            return self.updates.pop(0) if self.updates else []
        if method == "sendMessage":
            return {"message_id": len(self.calls)}
        return True

    async def close(self):
        self.closed = True

    def sent(self, chat_id=None):
        return [p for m, p in self.calls if m == "sendMessage"
                and (chat_id is None or p["chat_id"] == chat_id)]

    def last_text(self):
        return self.sent()[-1]["text"]


def tg_config(config, **overrides):
    cfg = copy.deepcopy(config)
    cfg["telegram"] = {**load_real_config().get("telegram", {}), "enabled": True,
                       "allowed_user_ids": [USER], **overrides}
    cfg["telegram"]["token"] = TOKEN
    return cfg


@pytest.fixture
def bot(config, sim_factory):
    """TelegramService auf einer Sim (virtuelle Zeit), Fake-API, feste Uhr."""
    def _make(**overrides):
        cfg = tg_config(config, **overrides)
        sim = sim_factory(WALL, cfg=cfg)
        api = FakeApi()
        mono = FakeClock()
        svc = TelegramService(cfg, sim.svc, sim.hw, api=api, clock=lambda: NOW,
                              monotonic=mono)
        svc.sim, svc.fake, svc.mono = sim, api, mono
        return svc
    return _make


def msg(text, user=USER, chat_type="private", age=0, update_id=1):
    return {"update_id": update_id, "message": {
        "message_id": 5, "date": int(NOW - age), "text": text,
        "from": {"id": user, "username": "someone"},
        "chat": {"id": user, "type": chat_type}}}


def press(svc, answer="ok", user=USER):
    """Knopf der letzten Bestätigungsfrage drücken."""
    markup = svc.fake.sent()[-1]["reply_markup"]["inline_keyboard"][0]
    data = next(b["callback_data"] for b in markup if b["callback_data"].startswith(answer))
    return {"update_id": 9, "callback_query": {
        "id": "cb1", "data": data, "from": {"id": user},
        "message": {"message_id": 7, "chat": {"id": user}}}}


def last_edit(svc):
    return [p for m, p in svc.fake.calls if m == "editMessageText"][-1]["text"]


# ── Konfiguration ─────────────────────────────────────────────

def test_real_config_has_telegram_disabled_and_no_token():
    cfg = load_real_config()["telegram"]
    assert cfg["enabled"] is False and cfg["allowed_user_ids"] == []
    assert "token" not in cfg


def test_missing_section_uses_defaults():
    assert load_telegram_config({}) == TELEGRAM_DEFAULTS
    assert load_telegram_config({"telegram": None})["enabled"] is False


def test_invalid_values_fall_back(caplog):
    cfg = load_telegram_config({"telegram": {
        "enabled": "yes", "allowed_user_ids": ["123", 456, "abc", -1, True],
        "notify_level": "LOUD", "manual_duration_min": 99999, "max_messages_per_min": "x",
        "bundle_interval_s": 0}})
    assert cfg["enabled"] is False                       # nur echtes true schaltet ein
    assert cfg["allowed_user_ids"] == [123, 456]
    assert cfg["notify_level"] == "WARNING"
    assert cfg["manual_duration_min"] == TELEGRAM_DEFAULTS["manual_duration_min"]
    assert cfg["max_messages_per_min"] == TELEGRAM_DEFAULTS["max_messages_per_min"]
    assert cfg["bundle_interval_s"] == TELEGRAM_DEFAULTS["bundle_interval_s"]
    assert "ungültig" in caplog.text


def test_bot_name_instead_of_user_id_explains_itself(caplog):
    cfg = load_telegram_config({"telegram": {"enabled": True,
                                             "allowed_user_ids": ["Pool77Bot"]}})
    assert cfg["allowed_user_ids"] == []
    assert "eigene Telegram-Nutzer-ID" in caplog.text and "docs/telegram.md" in caplog.text


def test_token_comes_from_environment(monkeypatch):
    cfg = {"telegram": {}}
    monkeypatch.setenv("POOL_TELEGRAM_TOKEN", TOKEN)
    main_mod.apply_env_secrets(cfg)
    assert cfg["telegram"]["token"] == TOKEN
    monkeypatch.delenv("POOL_TELEGRAM_TOKEN")
    legacy = main_mod.apply_env_secrets({"telegram": {"token": "in-config"}})
    assert "telegram.token" in legacy                    # Warnung: gehört in secrets.env


# ── Log-Auswahl ───────────────────────────────────────────────

def _record(name, level, msg_text, event=None):
    rec = logging.LogRecord(name, level, __file__, 1, msg_text, None, None)
    if event:
        setattr(rec, "event.name", event)
    return rec


def test_handler_selects_by_level_and_event():
    out = []
    h = TelegramNotifyHandler(logging.WARNING, ["backwash.finished"], out.append)
    h.handle(_record("services.pump_service", logging.WARNING, "NOT-AUS verriegelt"))
    h.handle(_record("services.pump_service", logging.INFO, "Zeitplanwechsel",
                     "schedule.transition"))
    h.handle(_record("services.pump_service", logging.INFO, "Rückspülung beendet: ok",
                     "backwash.finished"))
    h.handle(_record("services.hardware", logging.ERROR, "DAC Schreibfehler"))
    assert len(out) == 3
    assert out[0].startswith("⚠️") and "NOT-AUS" in out[0]
    assert out[1].startswith("ℹ️") and out[2].startswith("🔴")


def test_handler_excludes_own_transport_and_http_libraries():
    out = []
    h = TelegramNotifyHandler(logging.INFO, [], out.append)
    for name in ("services.telegram_service", "httpx", "httpcore.connection"):
        h.handle(_record(name, logging.ERROR, "Telegram nicht erreichbar"))
    h.handle(_record("pool_marx.telegram", logging.WARNING, "fremder Nutzer"))
    assert out == [out[0]] and "fremder Nutzer" in out[0]


def test_handler_redacts_token_and_never_raises():
    out = []
    h = TelegramNotifyHandler(logging.WARNING, [], out.append,
                              redact=lambda s: s.replace(TOKEN, "***"))
    h.handle(_record("x", logging.WARNING, f"url .../bot{TOKEN}/getMe"))
    assert TOKEN not in out[0] and "***" in out[0]

    def boom(_):
        raise RuntimeError("voll")
    TelegramNotifyHandler(logging.WARNING, [], boom).handle(_record("x", logging.ERROR, "x"))


def test_handler_suppresses_repeats():
    out, clock = [], FakeClock()
    h = TelegramNotifyHandler(logging.WARNING, [], out.append,
                              repeat_suppress_s=900, monotonic=clock)
    for i in range(30):   # MQTT-Reconnect alle 10 s, Zahlen unterscheiden sich
        h.handle(_record("services.mqtt_service", logging.ERROR,
                         f"MQTT Fehler: Connection refused (Versuch {i}) — reconnect in 10s"))
        clock.advance(10)
    h.handle(_record("services.hardware", logging.ERROR, "DAC Schreibfehler"))
    assert len(out) == 2                                 # einmal MQTT, einmal DAC
    clock.advance(900)
    h.handle(_record("services.mqtt_service", logging.ERROR,
                     "MQTT Fehler: Connection refused (Versuch 99) — reconnect in 10s"))
    assert "29× wiederholt" in out[-1]


# ── Start / Stop ──────────────────────────────────────────────

@pytest.mark.parametrize("overrides,reason", [
    ({"enabled": False}, "enabled"),
    ({"allowed_user_ids": []}, "allowed_user_ids"),
])
def test_disabled_installs_nothing(bot, overrides, reason):
    svc = bot(**overrides)
    before = list(logging.getLogger().handlers)
    run(svc.start())
    assert not svc.enabled and reason in svc.disabled_reason
    assert logging.getLogger().handlers == before and svc.fake.calls == []


def test_disabled_without_token(config, sim_factory):
    cfg = tg_config(config)
    cfg["telegram"].pop("token")
    sim = sim_factory(WALL, cfg=cfg)
    svc = TelegramService(cfg, sim.svc, sim.hw)
    run(svc.start())
    assert not svc.enabled and "POOL_TELEGRAM_TOKEN" in svc.disabled_reason


def test_start_sends_startup_message_and_stop_cleans_up(bot):
    svc = bot(bundle_interval_s=0.5)
    svc._sleep = lambda s: asyncio.sleep(0.01)

    async def scenario():
        task = asyncio.create_task(svc.start())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if svc.fake.sent():
                break
        assert svc._handler in logging.getLogger().handlers
        assert logging.getLogger("httpx").level == logging.WARNING
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await svc.stop(flush_timeout_s=1.0, final_message="⏹ Pool Marx wird beendet")

    run(scenario())
    methods = [m for m, _ in svc.fake.calls]
    assert methods[:2] == ["getMe", "setMyCommands"] and "getUpdates" in methods
    texts = [p["text"] for p in svc.fake.sent(USER)]
    assert f"Pool Marx {__version__} gestartet" in texts[0]
    assert texts[-1] == "⏹ Pool Marx wird beendet"
    assert svc._handler is None and svc.fake.closed


def test_invalid_token_disables_bot(bot, caplog):
    svc = bot()
    svc.fake.fail["getMe"] = TelegramApiError("HTTP 401: Unauthorized", status=401)
    run(svc.start())
    assert not svc.enabled and svc._handler is None
    assert "Bot-Token ungültig" in caplog.text


# ── Senden: Bündeln, Begrenzen, Wiederholen ───────────────────

def test_sender_bundles_rate_limits_and_retries(bot):
    svc = bot(max_messages_per_min=2, notify_startup=False)
    svc._running = True

    async def step():
        svc._sleep = lambda s: asyncio.sleep(0)
        task = asyncio.create_task(svc._sender_loop())
        await asyncio.sleep(0.05)
        svc._running = False
        await asyncio.gather(task, return_exceptions=True)

    for i in range(3):
        svc.submit(f"Meldung {i}")
    svc._running = True
    run(step())
    assert len(svc.fake.sent()) == 1 and svc.fake.last_text().count("Meldung") == 3

    svc.submit("zweite")
    svc._running = True
    run(step())
    svc.submit("dritte (über Limit)")
    svc._running = True
    run(step())
    assert len(svc.fake.sent()) == 2                    # Limit 2/min erreicht
    svc.mono.advance(61)
    svc._running = True
    run(step())
    assert svc.fake.last_text() == "dritte (über Limit)"

    # Netzfehler: Nachricht bleibt im Postausgang und geht später raus
    svc.fake.fail["sendMessage"] = TelegramApiError("ConnectError")
    svc.submit("offline erzeugt")
    svc._running = True
    run(step())
    assert list(svc._outbox) == ["offline erzeugt"]
    del svc.fake.fail["sendMessage"]
    svc.mono.advance(61)
    svc._sleep = lambda s: asyncio.sleep(0)
    svc._running = True
    run(step())
    assert svc.fake.last_text() == "offline erzeugt"


def test_outbox_is_bounded(bot):
    svc = bot()
    for i in range(tg.OUTBOX_SIZE + 5):
        svc.submit(f"m{i}")
    text = svc._take_batch()
    assert "m0\n" not in text and "5 ältere Meldungen verworfen" in text


# ── Befehle ───────────────────────────────────────────────────

def test_unauthorized_user_is_ignored_and_logged_once(bot, caplog):
    svc = bot()
    run(svc.handle_update(msg("/pumpe 100", user=OTHER)))
    run(svc.handle_update(msg("/pumpe 100", user=OTHER)))
    assert svc.fake.sent() == []                         # keine Antwort an Fremde
    assert svc.sim.svc.get_state()["mode"] != "manual"
    assert len(events_of(caplog, "telegram.unauthorized")) == 1


def test_group_chat_is_rejected_even_for_allowed_user(bot):
    svc = bot()
    run(svc.handle_update(msg("/aus", chat_type="group")))
    assert svc.fake.sent() == [] and svc.sim.svc.get_state()["mode"] != "manual"


def test_stale_command_is_not_executed(bot):
    svc = bot()
    run(svc.handle_update(msg("/pumpe 100", age=3600)))
    assert svc.sim.svc.get_state()["mode"] != "manual"
    assert "ignoriert" in svc.fake.last_text()


def test_pump_speed_with_default_duration(bot, info_logs):
    svc = bot()

    async def scenario():
        await svc.handle_update(msg("/pumpe 60"))
        state = svc.sim.svc.get_state()
        assert state["mode"] == "manual" and state["speed_pct"] == 60
        assert svc.sim.svc._override_task is not None     # läuft nach 120 min ab
        await svc.sim.vt.run_for(120 * 60 + 1)
        assert svc.sim.svc.get_state()["mode"] == "schedule"

    run(scenario())
    assert "Pumpe auf 60 %" in svc.fake.sent()[0]["text"]
    assert "zurück zum Zeitplan um" in svc.fake.sent()[0]["text"]
    assert events_of(info_logs, "telegram.command")


def test_pump_bad_arguments(bot):
    svc = bot()
    for text in ("/pumpe", "/pumpe 150", "/pumpe abc", "/pumpe 50 9999"):
        run(svc.handle_update(msg(text)))
        assert "Aufruf: /pumpe" in svc.fake.last_text()
    assert svc.sim.svc.get_state()["mode"] != "manual"


def test_off_without_limit_and_back_to_schedule(bot):
    svc = bot()

    async def scenario():
        await svc.handle_update(msg("/aus 0"))
        assert svc.sim.svc.get_state()["speed_pct"] == 0
        assert svc.sim.svc._override_task is None
        await svc.handle_update(msg("/zeitplan"))
        assert svc.sim.svc.get_state()["mode"] == "schedule"

    run(scenario())
    assert "ohne Zeitlimit" in svc.fake.sent()[0]["text"]


def test_light_and_emergency_lock(bot):
    svc = bot()

    async def scenario():
        await svc.handle_update(msg("/licht an"))
        assert svc.sim.hw.get_all_states()["light"] is True
        svc.sim.svc.emergency_stop()
        await svc.handle_update(msg("/licht an"))
        await svc.handle_update(msg("/pumpe 50"))

    run(scenario())
    assert all("Not-Aus ist aktiv" in p["text"] for p in svc.fake.sent()[1:])
    assert svc.sim.svc.get_state()["mode"] == "emergency"


def test_shutdown_needs_confirmation(bot):
    svc = bot()

    async def scenario():
        await svc.handle_update(msg("/abschalten"))
        assert not svc.sim.svc.emergency_active           # erst nach Bestätigung
        await svc.handle_update(press(svc, "ok"))

    run(scenario())
    assert svc.sim.svc.emergency_active
    assert not svc.sim.hw.pump_mains_connected
    assert "Abgeschaltet und verriegelt" in last_edit(svc)


def test_confirmation_cancel_expiry_and_other_user(bot):
    svc = bot(allowed_user_ids=[USER, OTHER])

    async def scenario():
        await svc.handle_update(msg("/abschalten"))
        await svc.handle_update(press(svc, "no"))
        assert "Abgebrochen" in last_edit(svc)

        await svc.handle_update(msg("/abschalten"))
        cb = press(svc, "ok", user=OTHER)
        await svc.handle_update(cb)
        assert "anderen Nutzer" in last_edit(svc)
        await svc.handle_update(cb)                        # Nonce verbraucht
        assert "nicht mehr gültig" in last_edit(svc)

        await svc.handle_update(msg("/abschalten"))
        svc.mono.advance(61)
        await svc.handle_update(press(svc, "ok"))
        assert "abgelaufen" in last_edit(svc)

    run(scenario())
    assert not svc.sim.svc.emergency_active


def test_backwash_with_confirmation(bot):
    svc = bot()

    async def scenario():
        await svc.handle_update(msg("/rueckspuelen"))
        await svc.handle_update(press(svc, "ok"))
        assert svc.sim.svc.backwash_running
        await svc.sim.svc.cancel_backwash()

    run(scenario())
    assert "Rückspülung gestartet" in last_edit(svc)


def test_acknowledge_is_not_possible_remotely(bot):
    svc = bot()
    svc.sim.svc.emergency_stop()
    run(svc.handle_update(msg("/quittieren")))
    assert svc.sim.svc.emergency_active and not svc.sim.hw.pump_mains_connected
    assert "nur am Dashboard" in svc.fake.last_text()


def test_commands_disabled_still_allows_status(bot):
    svc = bot(commands_enabled=False)
    run(svc.handle_update(msg("/pumpe 80")))
    assert "deaktiviert" in svc.fake.last_text()
    run(svc.handle_update(msg("/status")))
    assert f"Pool Marx {__version__}" in svc.fake.last_text()
    assert svc.sim.svc.get_state()["mode"] != "manual"


def test_status_and_fault_note(bot, caplog):
    svc = bot()
    svc.sim.svc.emergency_stop()
    run(svc.handle_update(msg("/status@pool_test_bot")))
    text = svc.fake.last_text()
    assert "NOT-AUS aktiv" in text and "Netz Pumpe: AUS" in text and "Frostschutz" in text
    run(svc.handle_update(msg("/notiz E-04 im Display")))
    assert "Notiert" in svc.fake.last_text()
    assert events_of(caplog, "pump.fault_note")[0].__dict__["fault.note"] == "E-04 im Display"


def test_unknown_command_and_plain_text(bot):
    svc = bot()
    run(svc.handle_update(msg("/foo")))
    assert "Unbekannter Befehl" in svc.fake.last_text()
    run(svc.handle_update(msg("hallo")))
    assert "/hilfe" in svc.fake.last_text()
    run(svc.handle_update(msg("/hilfe")))
    assert "/abschalten" in svc.fake.last_text() and "quittieren" in svc.fake.last_text()


def test_poll_loop_advances_offset_and_survives_errors(bot):
    svc = bot()
    svc.fake.updates = [[msg("/status", update_id=41)], [msg("/status", update_id=42)]]
    svc.fake.fail["getUpdates"] = TelegramApiError("ReadTimeout")
    svc._running = True
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        svc.fake.fail.pop("getUpdates", None)            # Netz wieder da
        await asyncio.sleep(0)

    svc._sleep = fake_sleep

    async def scenario():
        task = asyncio.create_task(svc._poll_loop())
        for _ in range(200):
            await asyncio.sleep(0.001)
            if len(svc.fake.sent()) >= 2:
                break
        svc._running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())
    assert sleeps and sleeps[0] == 5.0
    assert svc._offset == 43
    offsets = [p.get("offset") for m, p in svc.fake.calls if m == "getUpdates"]
    assert offsets[-1] == 42                              # nach Update 41


# ── HTTP: Token taucht nie in Fehlertexten auf ────────────────

def _api_with(handler):
    api = HttpxApi(TOKEN)
    run(api._client.aclose())
    api._client = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{TOKEN}/",
                                    transport=httpx.MockTransport(handler))
    return api


def test_http_errors_are_redacted():
    def unreachable(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    async def call(api, method="getMe"):
        try:
            return await api(method, {})
        finally:
            await api.close()

    with pytest.raises(TelegramApiError) as exc:
        run(call(_api_with(unreachable)))
    assert TOKEN not in str(exc.value) and "ConnectError" in str(exc.value)

    def unauthorized(request):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    with pytest.raises(TelegramApiError) as exc:
        run(call(_api_with(unauthorized)))
    assert exc.value.status == 401

    def flood(request):
        return httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
                                         "parameters": {"retry_after": 7}})

    with pytest.raises(TelegramApiError) as exc:
        run(call(_api_with(flood)))
    assert exc.value.retry_after == 7

    def ok(request):
        assert request.url.path.endswith("/getMe")
        return httpx.Response(200, json={"ok": True, "result": {"username": "b"}})

    assert run(call(_api_with(ok))) == {"username": "b"}
