"""A3 — DAC-Refresh-Thread: zyklisches Schreiben, Retry, Robustheit, Jitter"""

import logging
import random
import threading
import time

import pytest

from services import hardware as hw_mod
from tests.conftest import FakeClock, FlakyBus, max_interval


def test_rewrites_cyclically_even_without_change(make_hw):
    bus = FlakyBus()
    hw = make_hw(i2c_bus=bus, start_thread=True)
    time.sleep(1.1)
    writes = bus.writes
    # Init-Schreiben (1) + ~5 Refresh in 1,1 s bei 0,2 s Takt
    assert 5 <= writes <= 8
    assert all(code == 0 for _, code in bus.history)


def test_single_failure_is_retried_once(make_hw):
    bus = FlakyBus()
    hw = make_hw(i2c_bus=bus)
    before = bus.attempts
    bus.fail_next = 1
    hw._dac_tick(now=1.0)
    assert bus.attempts - before == 2          # genau ein Retry
    assert hw.get_dac_snapshot()["write_errors"] == 0


def test_double_failure_counts_error_and_no_third_try(make_hw):
    bus = FlakyBus()
    hw = make_hw(i2c_bus=bus)
    before = bus.attempts
    bus.fail_next = 2
    hw._dac_tick(now=1.0)
    assert bus.attempts - before == 2
    snap = hw.get_dac_snapshot()
    assert snap["write_errors"] == 1 and snap["consecutive_errors"] == 1


def test_error_logging_is_rate_limited(make_hw, caplog):
    bus = FlakyBus(fail_rate=1.0)
    hw = make_hw(i2c_bus=bus)
    with caplog.at_level(logging.INFO, logger="services.hardware"):
        for i in range(50):
            hw._dac_tick(now=1.0 + i * 0.2)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) <= 1                     # max. 1 Meldung pro Minute
    assert hw.get_dac_snapshot()["write_errors"] >= 50
    # Wiederherstellung: normales Schreiben, Erholung wird einmal gemeldet
    bus.fail_rate = 0.0
    with caplog.at_level(logging.INFO, logger="services.hardware"):
        hw._dac_tick(now=20.0)
    assert hw.get_dac_snapshot()["consecutive_errors"] == 0
    assert any("wieder normal" in r.getMessage() for r in caplog.records)


def test_thread_survives_permanent_oserror_and_recovers(make_hw):
    bus = FlakyBus(fail_rate=1.0)
    hw = make_hw(i2c_bus=bus, start_thread=True)
    time.sleep(0.7)
    assert hw.dac_thread_alive
    errs = hw.get_dac_snapshot()["write_errors"]
    assert errs >= 2
    bus.fail_rate = 0.0
    ok_before = bus.writes
    time.sleep(0.5)
    assert hw.dac_thread_alive
    assert bus.writes > ok_before
    assert hw.get_dac_snapshot()["write_errors"] in (errs, errs + 1)


def test_missing_bus_counts_errors_without_crash(make_hw):
    hw = make_hw(i2c_bus=FlakyBus())
    hw._dac_bus = None                       # z. B. SMBus(1) nicht verfügbar
    hw._dac_tick(now=1.0)
    assert hw.get_dac_snapshot()["write_errors"] == 1


def test_thread_safety_stress(make_hw):
    """Parallele Sollwerte aus "API", "Zeitplan", "Frost" + Not-Aus — kein Deadlock, konsistent."""
    bus = FlakyBus(fail_rate=0.05)
    hw = make_hw(i2c_bus=bus, start_thread=True)
    stop = threading.Event()
    errors = []

    def worker(reason, seed):
        rng = random.Random(seed)
        while not stop.is_set():
            try:
                r = rng.random()
                if r < 0.05:
                    hw.pump_mains_cutoff()
                    hw.pump_mains_restore()
                elif r < 0.5:
                    hw.pump_set_speed(rng.randint(0, 100), reason=reason)
                else:
                    snap = hw.get_dac_snapshot()
                    assert 0 <= snap["actual_pct"] <= 100
                    assert 0 <= snap["code"] <= hw.dac.max_code
                    hw.get_all_states()
            except Exception as e:    # pragma: no cover
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(r, i))
               for i, r in enumerate(["manual", "schedule", "frost", "manual", "schedule", "frost"])]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=2)
        assert not t.is_alive(), "Deadlock: Worker hängt"
    assert not errors
    assert hw.dac_thread_alive
    hw.emergency_stop()
    assert hw._dac_bus.last_code == 0 and hw._dac_actual_pct == 0


def test_cleanup_stops_thread_and_leaves_zero(make_hw):
    bus = FlakyBus()
    hw = make_hw(i2c_bus=bus, start_thread=True)
    hw.pump_on(50)
    time.sleep(0.5)
    t0 = time.monotonic()
    hw.cleanup()
    assert time.monotonic() - t0 < 2.5
    assert not hw.dac_thread_alive
    assert bus.last_code == 0
    n = bus.writes
    time.sleep(0.5)
    assert bus.writes == n                    # nach cleanup schreibt niemand mehr


def test_jitter_under_cpu_load(make_hw):
    """Kein Intervall > 3× refresh_interval_s, auch wenn andere Threads rechnen."""
    bus = FlakyBus()
    hw = make_hw(i2c_bus=bus, start_thread=True)
    stop = threading.Event()

    def burn():
        x = 0
        while not stop.is_set():
            x = (x * 31 + 7) % 1000003

    burners = [threading.Thread(target=burn, daemon=True) for _ in range(3)]
    for b in burners:
        b.start()
    hw.pump_on(80)
    time.sleep(3.0)
    stop.set()
    for b in burners:
        b.join()
    assert max_interval(bus.attempt_times) < 3 * hw.dac.refresh_interval_s


def test_steady_state_ticks_produce_no_logs(make_hw, caplog):
    hw = make_hw()
    clock_t = 1.0
    with caplog.at_level(logging.DEBUG, logger="services"):
        for _ in range(100):
            clock_t += 0.2
            hw._dac_tick(now=clock_t)
    assert caplog.records == []


def test_duration_hook_is_sampled_not_per_write(make_hw):
    hw = make_hw()
    calls = []
    hw.set_write_duration_hook(calls.append)
    for i in range(100):
        hw._dac_tick(now=1.0 + i * 0.2)
    assert 1 <= len(calls) <= 100 // hw._duration_sample_every + 1
    hw.set_write_duration_hook(lambda ms: 1 / 0)    # kaputter Hook stört nicht
    for i in range(30):
        hw._dac_tick(now=50.0 + i * 0.2)
