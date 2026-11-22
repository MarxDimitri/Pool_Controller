"""A9 — Logging: Stream-/Datei-Handler bleiben, OTel-Handler kommt nur dazu"""

import logging
import logging.handlers

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import main as main_mod
from services import telemetry as tel
from services.events import event_extra, sanitize_text
from tests.conftest import events_of


@pytest.fixture
def clean_root():
    """setup_logging() nutzt basicConfig → Root muss dafür leer sein (pytest-Handler parken)."""
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    root.handlers = []
    yield root
    for h in root.handlers:
        if h not in saved:
            h.close()
    root.handlers = saved
    root.setLevel(level)


def test_handlers_coexist(clean_root, tmp_path, capsys):
    log_file = tmp_path / "logs" / "pool.log"
    clean_root.handlers = []          # pytest hängt pro Phase eigene Handler an
    main_mod.setup_logging({"logging": {"level": "INFO", "file": str(log_file),
                                        "max_bytes": 100000, "backup_count": 2}})
    original = list(clean_root.handlers)
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in original)
    assert any(type(h) is logging.StreamHandler for h in original)

    exporter = InMemoryLogRecordExporter()
    t = tel.setup_telemetry({"telemetry": {"enabled": True, "system_metrics": False}},
                            metric_readers=[InMemoryMetricReader()], log_exporter=exporter)
    assert t.enabled
    assert clean_root.handlers[:len(original)] == original     # unverändert, OTel nur ergänzt
    assert len(clean_root.handlers) == len(original) + 1

    logging.getLogger("services.x").info("Testzeile-123")
    t.force_flush()
    assert "Testzeile-123" in log_file.read_text()
    assert "Testzeile-123" in capsys.readouterr().err
    assert any(r.log_record.body == "Testzeile-123" for r in exporter.get_finished_logs())

    t.shutdown(timeout_s=1)
    assert clean_root.handlers == original                      # OTel-Handler wieder weg


def test_setpoint_changes_logged_with_old_new_reason(make_hw, caplog):
    hw = make_hw()
    with caplog.at_level(logging.INFO):
        hw.pump_on(40, reason="schedule")
        hw.pump_set_speed(40, reason="manual")     # keine Änderung → keine Zeile
        hw.pump_off(reason="manual")
        hw.pump_on(60, reason="frost")
        hw.emergency_stop()
    ev = events_of(caplog, "pump.setpoint_changed")
    got = [(getattr(r, "pump.setpoint.old"), getattr(r, "pump.setpoint.new"),
            getattr(r, "pump.setpoint.reason")) for r in ev]
    assert got == [(0, 40, "schedule"), (40, 0, "manual"), (0, 60, "frost"), (60, 0, "emergency")]
    assert "→" in ev[0].getMessage() and "schedule" in ev[0].getMessage()
    assert hw.get_dac_snapshot()["last_change"]["reason"] == "emergency"


def test_event_extra_sanitizes():
    ex = event_extra("x", a=1.23456, b=None, c=[1, 2], password="p", token="t", d="a\nb")
    assert ex == {"event.name": "x", "a": 1.235, "c": "[1, 2]", "d": "a b"}
    assert len(sanitize_text("y" * 900)) == 501
    assert sanitize_text(None) == ""
