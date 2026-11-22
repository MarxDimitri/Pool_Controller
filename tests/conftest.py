"""
Gemeinsame Test-Infrastruktur (alles im Simulationsmodus: kein I²C, kein GPIO,
kein Netzwerk).

- FakeClock: monotone Uhr, die nur von Hand weiterläuft.
- VirtualTime: Mini-Scheduler für asyncio — sleep() wartet nicht real, sondern
  bis die virtuelle Zeit den Weckzeitpunkt erreicht. Beim Vorstellen der Zeit
  wird der DAC-Tick im Raster refresh_interval_s ausgeführt (Rampe läuft mit).
- Sim: HardwareController + PumpService auf virtueller Zeit.
"""

import asyncio
import copy
import hashlib
import heapq
import itertools
import logging
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import hardware as hw_mod  # noqa: E402
from services.hardware import HardwareController, MockDacBus  # noqa: E402
from services.pump_service import PumpService  # noqa: E402

CONFIG_PATH = ROOT / "config" / "config.yaml"
API_SECRET = "test-secret-0123456789"
AUTH = {"Authorization": f"Bearer {API_SECRET}"}


def pytest_configure(config):
    config.addinivalue_line("markers", "stage_b: Integrations-/Robustheitstests (Stufe B, länger)")


@pytest.fixture(scope="session", autouse=True)
def _simulation_and_config_untouched():
    """Alle Tests laufen im Simulationsmodus; config.yaml wird nie verändert."""
    assert hw_mod.SIMULATION is True, "Tests müssen im Simulationsmodus laufen"
    before = hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
    yield
    after = hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
    assert before == after, "config/config.yaml wurde während der Tests verändert!"


def load_real_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


@pytest.fixture
def config(tmp_path) -> dict:
    """Kopie der echten config.yaml (Struktur inkl. MQTT), Log-Datei nach tmp."""
    cfg = copy.deepcopy(load_real_config())
    cfg.setdefault("logging", {})["file"] = str(tmp_path / "pool.log")
    cfg.setdefault("api", {})["secret_key"] = API_SECRET
    return cfg


# ── Uhren ────────────────────────────────────────────────────

class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float):
        self.t += dt


# ── GPIO / I²C Doubles ───────────────────────────────────────

class RecordingGPIO:
    """GPIO-Double: zeichnet alle Aufrufe (in Reihenfolge) auf, active-low wie echt."""
    BCM, OUT, IN, HIGH, LOW, PUD_UP = "BCM", "OUT", "IN", True, False, "PUD_UP"

    def __init__(self, events=None):
        self.state = {}
        self.calls = []
        self.events = events if events is not None else []
        self.level = True   # Schwimmer: True = Pegel OK

    def setmode(self, mode): pass
    def setwarnings(self, v): pass

    def setup(self, pin, mode, initial=None, pull_up_down=None):
        self.calls.append(("setup", pin, mode, initial))
        if initial is not None:
            self.state[pin] = initial

    def output(self, pin, value):
        self.calls.append(("output", pin, bool(value)))
        self.events.append(("gpio", pin, bool(value)))
        self.state[pin] = bool(value)

    def input(self, pin):
        return self.level

    def cleanup(self):
        self.calls.append(("cleanup",))

    def outputs_for(self, pin):
        return [c for c in self.calls if c[0] == "output" and c[1] == pin]


class FlakyBus(MockDacBus):
    """I²C-Double mit Fehlerinjektion und Zeitstempel jedes Versuchs."""

    def __init__(self, events=None, fail_rate: float = 0.0, seed: int = 1):
        super().__init__()
        self.fail_rate = fail_rate
        self.fail_next = 0          # die nächsten n Versuche scheitern
        self.attempts = 0
        self.attempt_times = []
        self.events = events if events is not None else []
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def write_i2c_block_data(self, addr, cmd, data):
        with self._lock:
            self.attempts += 1
            self.attempt_times.append(time.monotonic())
            fail = False
            if self.fail_next > 0:
                self.fail_next -= 1
                fail = True
            elif self.fail_rate and self._rng.random() < self.fail_rate:
                fail = True
        if fail:
            raise OSError(121, "Remote I/O error")
        super().write_i2c_block_data(addr, cmd, data)
        self.events.append(("dac", self.last_code))


def max_interval(times, skip: int = 1) -> float:
    ts = list(times)[skip:]
    return max((b - a for a, b in zip(ts, ts[1:])), default=0.0)


@pytest.fixture
def make_hw(config):
    """Fabrik für HardwareController — räumt Threads am Testende auf."""
    created = []

    def _make(cfg=None, **kw):
        kw.setdefault("gpio", RecordingGPIO())
        kw.setdefault("i2c_bus", FlakyBus())
        kw.setdefault("start_thread", False)
        hw = HardwareController(cfg or config, **kw)
        created.append(hw)
        return hw

    yield _make
    for hw in created:
        hw.stop_dac_thread(timeout=2.0)


# ── Virtuelle Zeit für asyncio ───────────────────────────────

class VirtualTime:
    def __init__(self, clock: FakeClock, hw: HardwareController, wall_start: datetime):
        self.clock = clock
        self.hw = hw
        self.t0 = clock.t
        self.wall_start = wall_start
        self._sleepers = []
        self._seq = itertools.count()
        self.tick_dac = True

    # Wanduhr aus der virtuellen monotonen Zeit (bei tz-bewussten Zeiten über UTC,
    # damit Sommer-/Winterzeit korrekt entsteht)
    def now(self) -> datetime:
        delta = timedelta(seconds=self.clock.t - self.t0)
        if self.wall_start.tzinfo is None:
            return self.wall_start + delta
        return (self.wall_start.astimezone(timezone.utc) + delta).astimezone(self.wall_start.tzinfo)

    async def sleep(self, seconds: float):
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.clock.t + max(0.0, seconds), next(self._seq), fut))
        await fut

    def advance_to(self, t_end: float):
        """Zeit vorstellen; DAC-Ticks im Takt (Schnellvorlauf, wenn Ist == Soll)."""
        step = self.hw.dac.refresh_interval_s
        while self.clock.t < t_end - 1e-9:
            if (self.tick_dac and self.hw._dac_actual_pct == self.hw._dac_target_pct) or not self.tick_dac:
                self.clock.t = t_end
                if self.tick_dac:
                    self.hw._dac_tick()
                break
            self.clock.t = min(t_end, self.clock.t + step)
            self.hw._dac_tick()

    def advance(self, seconds: float):
        self.advance_to(self.clock.t + seconds)

    async def settle(self, rounds: int = 10):
        for _ in range(rounds):
            await asyncio.sleep(0)

    async def run_for(self, seconds: float):
        await self.run_until(self.clock.t + seconds)

    async def run_until(self, t_end: float):
        while True:
            await self.settle()
            while self._sleepers and self._sleepers[0][2].done():
                heapq.heappop(self._sleepers)   # abgebrochene Sleeper
            if not self._sleepers or self._sleepers[0][0] > t_end:
                self.advance_to(t_end)
                await self.settle()
                return
            wake, _, fut = heapq.heappop(self._sleepers)
            self.advance_to(wake)
            if not fut.done():
                fut.set_result(None)


class Sim:
    """Hardware + PumpService auf virtueller Zeit (kein DAC-Thread, keine echten sleeps)."""

    def __init__(self, config: dict, wall_start: datetime, bus=None, gpio=None):
        self.clock = FakeClock()
        self.bus = bus or FlakyBus()
        self.gpio = gpio or RecordingGPIO()
        self.hw = HardwareController(config, gpio=self.gpio, i2c_bus=self.bus,
                                     clock=self.clock, start_thread=False)
        self.vt = VirtualTime(self.clock, self.hw, wall_start)
        self.svc = PumpService(self.hw, config, now=self.vt.now,
                               monotonic=self.clock, sleep=self.vt.sleep)

    def at(self, wall: datetime):
        """Virtuelle Zeit auf eine Wanduhrzeit vorstellen (ohne Tasks laufen zu lassen)."""
        delta = (wall - self.vt.now()).total_seconds()
        assert delta >= 0
        self.vt.advance(delta)


@pytest.fixture
def sim_factory(config):
    def _make(wall_start: datetime, cfg=None, **kw):
        return Sim(cfg or config, wall_start, **kw)
    return _make


def run(coro):
    """Coroutine in frischem Event-Loop ausführen (kein pytest-asyncio nötig)."""
    return asyncio.run(coro)


def events_of(caplog, name):
    return [r for r in caplog.records if getattr(r, "event.name", None) == name]


@pytest.fixture
def info_logs(caplog):
    caplog.set_level(logging.INFO)
    return caplog
