"""Versionierung: eine Quelle, überall gleich; Changelog- und Release-Logik"""

import importlib.util
import re

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from services import telemetry as tel
from services.hardware import HardwareController
from services.mqtt_service import MQTTService
from services.pump_service import PumpService
from services.version import __version__
from tests.conftest import API_SECRET, AUTH, ROOT, FlakyBus, RecordingGPIO


def load_release():
    spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_no_other_hardcoded_versions():
    for path in ["api/main.py", "services/mqtt_service.py", "services/telemetry.py", "main.py"]:
        assert not re.search(r'["\']\d+\.\d+\.\d+["\']', (ROOT / path).read_text()), path


def test_version_everywhere(config):
    assert tel.SERVICE_VERSION == __version__
    assert MQTTService(config)._ha_device()["sw_version"] == __version__
    hw = HardwareController(config, gpio=RecordingGPIO(), i2c_bus=FlakyBus(), start_thread=False)
    app = api_main.create_app(PumpService(hw, config), object(), hw, {"secret_key": API_SECRET})
    with TestClient(app) as c:
        assert c.get("/api/health").json()["version"] == __version__
        assert c.get("/api/status", headers=AUTH).json()["version"] == __version__
        assert 'id="versionTag"' in c.get("/").text


def test_changelog_has_current_version_and_unreleased():
    text = (ROOT / "CHANGELOG.md").read_text()
    assert re.search(r"^## \[Unreleased\]", text, re.M)
    assert re.search(rf"^## \[{re.escape(__version__)}\] - \d{{4}}-\d{{2}}-\d{{2}}", text, re.M)
    assert f"[{__version__}]: " in text


SAMPLE = """# Changelog

## [Unreleased]

### Added
- Neues Feature

## [1.1.0] - 2026-11-24

### Added
- Alt

[Unreleased]: https://github.com/MarxDimitri/Pool_Controller/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/MarxDimitri/Pool_Controller/releases/tag/v1.1.0
"""


def test_release_changelog_bump():
    rel = load_release()
    assert rel.unreleased_notes(SAMPLE) == "### Added\n- Neues Feature"
    out = rel.bump_changelog(SAMPLE, "1.2.0", "2026-12-01")
    assert "## [Unreleased]\n\n## [1.2.0] - 2026-12-01\n\n### Added\n- Neues Feature" in out
    assert rel.unreleased_notes(out) == ""
    assert "[Unreleased]: https://github.com/MarxDimitri/Pool_Controller/compare/v1.2.0...HEAD" in out
    assert "[1.2.0]: https://github.com/MarxDimitri/Pool_Controller/releases/tag/v1.2.0" in out
    assert "[1.1.0]: https://github.com/MarxDimitri/Pool_Controller/releases/tag/v1.1.0" in out
    assert rel.section_for(out, "1.2.0") == "### Added\n- Neues Feature"
    assert rel.section_for(out, "1.1.0") == "### Added\n- Alt"


@pytest.mark.parametrize("bad", ["1.2", "v1.2.x", "1.2.3.4", "abc"])
def test_release_rejects_bad_versions(bad):
    with pytest.raises(SystemExit):
        load_release().parse(bad)
