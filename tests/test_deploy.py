"""
deploy/install-release.sh — echter Ablauf in einem Temp-Verzeichnis.

Statt systemd startet ein Fake-systemctl einen Mini-HTTP-Server, der unter
/api/health die Version der aktiven Release meldet (Release mit Datei BROKEN
startet nicht → testet den automatischen Rollback). pip wird übersprungen.
"""

import io
import os
import socket
import subprocess
import tarfile
import textwrap
from pathlib import Path

import pytest

from tests.conftest import ROOT

pytestmark = pytest.mark.stage_b
SCRIPT = ROOT / "deploy" / "install-release.sh"

FAKE_SERVER = textwrap.dedent("""
    import http.server, json, os, re, sys
    d = os.path.realpath(sys.argv[1]); port = int(sys.argv[2])
    if os.path.exists(os.path.join(d, "BROKEN")):
        sys.exit(1)
    try:
        v = re.search(r'"([^"]+)"', open(os.path.join(d, "services", "version.py")).read()).group(1)
    except OSError:
        v = "1.0.0"
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "ok", "version": v}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
""")

FAKE_CTL = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Fake-systemctl: restart|start|stop|is-active [--quiet] <service>
    S="$FAKE_STATE"; pidf="$S/pid"
    stop() { if [ -f "$pidf" ]; then kill "$(cat "$pidf")" 2>/dev/null || true
             while kill -0 "$(cat "$pidf")" 2>/dev/null; do sleep 0.05; done; rm -f "$pidf"; fi; }
    case "$1" in
      restart|start) stop; python3 "$S/server.py" "$POOL_BASE/pool_controller" "$POOL_HEALTH_PORT" \\
                       >/dev/null 2>&1 & echo $! > "$pidf"; echo "$1" >> "$S/calls" ;;
      stop) stop; echo stop >> "$S/calls" ;;
      is-active) [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null ;;
    esac
""")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


@pytest.fixture
def pi(tmp_path):
    base, state = tmp_path / "opt", tmp_path / "state"
    base.mkdir(); state.mkdir()
    (state / "server.py").write_text(FAKE_SERVER)
    ctl = state / "systemctl"; ctl.write_text(FAKE_CTL); ctl.chmod(0o755)
    override = tmp_path / "override.conf"
    override.write_text((ROOT / "deploy" / "pool_marx.override.conf").read_text())
    env = {**os.environ, "POOL_BASE": str(base), "POOL_SYSTEMCTL": str(ctl), "FAKE_STATE": str(state),
           "POOL_HEALTH_PORT": str(free_port()), "POOL_HEALTH_TIMEOUT": "8",
           "POOL_SKIP_PIP": "1", "POOL_OVERRIDE_PATH": str(override),
           "POOL_PYTHON": str(ROOT / ".venv" / "bin" / "python")}   # hat PyYAML (Config-Prüfung)

    # Altzustand wie auf dem Pi: ein Verzeichnis mit Code, config und venv
    old = base / "pool_controller"
    (old / "config").mkdir(parents=True)
    (old / "config" / "config.yaml").write_text("pump:\n  speeds: {min: 20}\napi:\n  port: 8080\n")
    (old / "main.py").write_text("# alt\n")
    (old / "venv" / "bin").mkdir(parents=True)

    def run(*args, check=True):
        r = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=120)
        if check and r.returncode != 0:
            raise AssertionError(f"{args} rc={r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def tarball(version, broken=False, extra_cfg=""):
        path = tmp_path / f"v{version}.tar"
        files = {
            "main.py": "# neu\n", "requirements.txt": "pyyaml\n",
            "services/version.py": f'__version__ = "{version}"\n',
            "config/config.yaml": "pump:\n  speeds: {min: 20}\n  min_speed: {mode: winter}\napi:\n  port: 8080\n" + extra_cfg,
            "deploy/pool_marx.override.conf": override.read_text(),
        }
        if broken:
            files["BROKEN"] = "x"
        with tarfile.open(path, "w") as t:
            for name, content in files.items():
                data = content.encode(); info = tarfile.TarInfo(name); info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        return str(path)

    yield run, tarball, base, state
    subprocess.run([str(ctl), "stop", "pool_marx"], env=env)


def active(base):
    return os.readlink(base / "pool_controller")


def test_install_requires_migration(pi):
    run, tarball, base, _ = pi
    r = run("install", "v1.2.0", tarball("1.2.0"), check=False)
    assert r.returncode != 0 and "migrate" in r.stderr


def test_full_lifecycle(pi):
    run, tarball, base, state = pi
    # 1. Migration
    run("migrate")
    assert active(base) == "releases/legacy"
    assert (base / "shared" / "config" / "config.yaml").exists()
    assert os.readlink(base / "releases" / "legacy" / "config") == "../../shared/config"
    assert "Bereits migriert" in run("migrate").stdout

    # 2. Neue Version: eigenes venv, Config-Symlink, Warnung bei fehlendem Abschnitt
    r = run("install", "v1.2.0", tarball("1.2.0", extra_cfg="newsection:\n  a: 1\n"))
    assert active(base) == "releases/v1.2.0"
    assert os.readlink(base / "previous") == "releases/legacy"
    rel = base / "releases" / "v1.2.0"
    assert (rel / "venv" / "bin" / "python").exists()
    assert (rel / "config.template" / "config.yaml").exists()
    assert os.readlink(rel / "config") == "../../shared/config"
    assert "Health-Check OK: Version 1.2.0" in r.stdout
    assert "pump.min_speed" in r.stdout and "newsection" in r.stdout      # fehlende Abschnitte gemeldet

    # 3. Gleiche Version nochmal → abgelehnt
    assert run("install", "v1.2.0", tarball("1.2.0"), check=False).returncode != 0

    # 4. Defekte Version → automatischer Rollback auf v1.2.0
    r = run("install", "v1.3.0", tarball("1.3.0", broken=True), check=False)
    assert r.returncode != 0 and "automatischer Rollback" in r.stdout
    assert active(base) == "releases/v1.2.0"
    assert os.readlink(base / "previous") == "releases/legacy"   # previous wiederhergestellt
    assert not (base / "releases" / "v1.3.0").exists()           # defekte Version entfernt
    assert "v1.2.0 läuft wieder" in r.stderr

    # 5. Manueller Rollback und Liste
    run("rollback")
    assert active(base) == "releases/legacy"
    assert os.readlink(base / "previous") == "releases/v1.2.0"
    out = run("list").stdout
    assert "legacy" in out and "← aktiv" in out and "v1.2.0" in out


def test_cleanup_keeps_active_and_previous(pi):
    run, tarball, base, _ = pi
    run("migrate")
    for v in ("1.2.0", "1.3.0", "1.4.0", "1.5.0"):
        run("install", f"v{v}", tarball(v))
    names = sorted(p.name for p in (base / "releases").iterdir())
    # KEEP=3 (Standard): die 3 neuesten bleiben; legacy ist weder aktiv noch vorherig → weg
    assert names == ["v1.3.0", "v1.4.0", "v1.5.0"]
    assert active(base) == "releases/v1.5.0"


def test_failed_build_leaves_active_untouched(pi):
    run, tarball, base, _ = pi
    run("migrate")
    bad = Path(tarball("1.2.0")).with_name("kaputt.tar")
    bad.write_bytes(b"kein tar")
    r = run("install", "v1.2.0", str(bad), check=False)
    assert r.returncode != 0
    assert active(base) == "releases/legacy"
    assert not (base / "releases" / "v1.2.0").exists()
