#!/usr/bin/env bash
# Pool Marx — Release auf dem Raspberry Pi installieren / umschalten
#
# Wird normalerweise vom Mac aus über release/deploy.sh aufgerufen.
#
#   install-release.sh install <tag> <tarball>   neue Version installieren und aktivieren
#   install-release.sh rollback                  zur vorherigen Version zurück
#   install-release.sh list                      installierte Versionen anzeigen
#   install-release.sh migrate                   einmalig: altes Verzeichnis → Release-Struktur
#
# Struktur:
#   /opt/pool_marx/releases/<tag>/        Code + eigenes venv je Version
#   /opt/pool_marx/shared/config/         config.yaml (bleibt über Versionen hinweg)
#   /opt/pool_marx/pool_controller  ->    releases/<aktive Version>   (systemd nutzt diesen Pfad)
#   /opt/pool_marx/previous         ->    releases/<vorherige Version> (für rollback)
#
# Umgebungsvariablen (nur für Tests): POOL_BASE, POOL_SYSTEMCTL, POOL_PYTHON,
# POOL_HEALTH_PORT, POOL_HEALTH_TIMEOUT, POOL_KEEP, POOL_SKIP_PIP, POOL_OVERRIDE_PATH
set -euo pipefail

BASE="${POOL_BASE:-/opt/pool_marx}"
RELEASES="$BASE/releases"
SHARED="$BASE/shared"
CURRENT="$BASE/pool_controller"
PREVIOUS="$BASE/previous"
SERVICE="pool_marx"
SYSTEMCTL="${POOL_SYSTEMCTL:-sudo systemctl}"
PY="${POOL_PYTHON:-python3}"
HEALTH_TIMEOUT="${POOL_HEALTH_TIMEOUT:-60}"
KEEP="${POOL_KEEP:-3}"
OVERRIDE="${POOL_OVERRIDE_PATH:-/etc/systemd/system/pool_marx.service.d/override.conf}"

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNUNG: %s\n' "$*"; }
die()  { printf '\nFEHLER: %s\n' "$*" >&2; exit 1; }

svc() { $SYSTEMCTL "$@" "$SERVICE"; }

active_release() { if [ -L "$CURRENT" ]; then basename "$(readlink "$CURRENT")"; fi; }
previous_release() { if [ -L "$PREVIOUS" ]; then basename "$(readlink "$PREVIOUS")"; fi; }

# Symlink atomar umsetzen (os.replace = rename(2))
set_link() {  # set_link <linkpfad> <ziel relativ zu BASE>
  local tmp="$1.tmp.$$"
  ln -sfn "$2" "$tmp"
  "$PY" -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' "$tmp" "$1"
}

release_version() {  # Version aus services/version.py, leer bei Altversionen
  sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$RELEASES/$1/services/version.py" 2>/dev/null || true
}

yaml_python() {  # Interpreter mit PyYAML: venv der Version, sonst System-Python
  local p
  for p in "$RELEASES/$1/venv/bin/python" "$PY"; do
    if "$p" -c 'import yaml' 2>/dev/null; then echo "$p"; return 0; fi
  done
  return 1
}

api_port() {
  if [ -n "${POOL_HEALTH_PORT:-}" ]; then echo "$POOL_HEALTH_PORT"; return; fi
  local yp; yp="$(yaml_python "$1")" || { echo 8080; return; }
  "$yp" - "$SHARED/config/config.yaml" <<'EOF' 2>/dev/null || echo 8080
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print((cfg.get("api") or {}).get("port", 8080))
EOF
}

health_ok() {  # health_ok <release>: Dienst aktiv + /api/health meldet die erwartete Version
  local rel="$1" want port deadline got
  want="$(release_version "$rel")"
  port="$(api_port "$rel")"
  deadline=$((SECONDS + HEALTH_TIMEOUT))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if svc is-active --quiet; then
      got="$(curl -fsS --max-time 3 "http://127.0.0.1:$port/api/health" 2>/dev/null \
             | "$PY" -c 'import sys,json; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true)"
      if [ -n "$got" ] && { [ -z "$want" ] || [ "$got" = "$want" ]; }; then
        echo "Health-Check OK: Version $got"
        return 0
      fi
    fi
    sleep 2
  done
  return 1
}

restart_and_check() { svc restart || true; health_ok "$1"; }

check_config() {  # fehlende Config-Abschnitte (bis 2 Ebenen) gegenüber der Vorlage melden
  local rel="$1"
  [ -f "$RELEASES/$rel/config.template/config.yaml" ] || return 0
  local yp; yp="$(yaml_python "$rel")" || { warn "Config-Prüfung übersprungen (kein Python mit PyYAML)"; return 0; }
  "$yp" - "$RELEASES/$rel/config.template/config.yaml" "$SHARED/config/config.yaml" <<'EOF' || true
import sys
try:
    import yaml
except ImportError:
    print("Config-Prüfung übersprungen (PyYAML fehlt)"); sys.exit(0)
tpl = yaml.safe_load(open(sys.argv[1])) or {}
cur = yaml.safe_load(open(sys.argv[2])) or {}
missing = []
for k, v in tpl.items():
    if k not in cur:
        missing.append(k)
    elif isinstance(v, dict) and isinstance(cur.get(k), dict):
        missing += [f"{k}.{s}" for s in v if s not in cur[k]]
if missing:
    print("WARNUNG: In shared/config/config.yaml fehlen Abschnitte der neuen Version")
    print("         (es gelten Standardwerte; Vorlage: config.template/config.yaml):")
    for m in missing:
        print(f"         - {m}")
else:
    print("Config vollständig.")
EOF
}

check_override() {
  local new="$RELEASES/$1/pi/pool_marx.override.conf"
  [ -f "$new" ] || new="$RELEASES/$1/deploy/pool_marx.override.conf"   # Tags vor v1.2
  if [ -f "$new" ] && [ -f "$OVERRIDE" ] && ! cmp -s "$new" "$OVERRIDE"; then
    warn "pi/pool_marx.override.conf weicht von $OVERRIDE ab. Bei Bedarf übernehmen:"
    echo "  sudo install -m 644 $new $OVERRIDE && sudo systemctl daemon-reload && sudo systemctl restart $SERVICE"
  fi
}

require_migrated() {
  [ -L "$CURRENT" ] || die "$CURRENT ist noch kein Symlink — einmalig 'migrate' ausführen (siehe RELEASE.md)"
  [ -d "$SHARED/config" ] || die "$SHARED/config fehlt"
}

cleanup_old() {  # nur die KEEP neuesten Versionen behalten, aktive und vorherige nie löschen
  local act prev n=0 d name
  act="$(active_release)"; prev="$(previous_release)"
  for d in $(ls -1dt "$RELEASES"/*/ 2>/dev/null); do
    name="$(basename "$d")"
    n=$((n + 1))
    if [ "$n" -gt "$KEEP" ] && [ "$name" != "$act" ] && [ "$name" != "$prev" ]; then
      echo "Entferne alte Version $name"
      rm -rf "$RELEASES/$name"
    fi
  done
}

cmd_install() {
  local tag="${1:-}" tarball="${2:-}"
  [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Tag '$tag' hat nicht das Format vX.Y.Z"
  [ -f "$tarball" ] || die "Archiv $tarball nicht gefunden"
  require_migrated
  local act; act="$(active_release)"
  [ "$tag" != "$act" ] || die "$tag ist bereits aktiv"

  local dir="$RELEASES/$tag"
  log "Installiere $tag nach $dir"
  rm -rf "$dir"                      # evtl. Rest eines abgebrochenen Versuchs (nicht aktiv)
  mkdir -p "$dir"
  trap 'rm -rf "$dir"; die "Installation abgebrochen — aktive Version $act unverändert"' ERR
  tar -x -C "$dir" -f "$tarball"
  [ -f "$dir/main.py" ] && [ -f "$dir/requirements.txt" ] || die "Archiv unvollständig"
  if [ -d "$dir/config" ]; then mv "$dir/config" "$dir/config.template"; fi
  ln -s ../../shared/config "$dir/config"

  log "Eigenes venv für $tag bauen (pip-Cache macht Folge-Deploys schneller)"
  "$PY" -m venv "$dir/venv"
  if [ "${POOL_SKIP_PIP:-0}" != "1" ]; then
    "$dir/venv/bin/pip" install -q --upgrade pip
    "$dir/venv/bin/pip" install -q -r "$dir/requirements.txt"
  fi
  trap - ERR

  log "Prüfungen"
  check_config "$tag"
  check_override "$tag"

  local old_prev; old_prev="$(previous_release)"
  log "Umschalten: ${act:-?} → $tag, Dienst neu starten"
  set_link "$PREVIOUS" "releases/$act"
  set_link "$CURRENT" "releases/$tag"
  if restart_and_check "$tag"; then
    cleanup_old
    log "Fertig: $tag aktiv (vorherige Version $act bleibt für rollback erhalten)"
    return 0
  fi

  warn "$tag startet nicht sauber — automatischer Rollback auf $act"
  set_link "$CURRENT" "releases/$act"
  if [ -n "$old_prev" ]; then set_link "$PREVIOUS" "releases/$old_prev"; else rm -f "$PREVIOUS"; fi
  rm -rf "$dir"
  if restart_and_check "$act"; then
    die "Deploy von $tag fehlgeschlagen, $act läuft wieder. Log: journalctl -u $SERVICE -n 100"
  fi
  die "Deploy von $tag fehlgeschlagen UND Rollback auf $act nicht bestätigt — sofort prüfen: journalctl -u $SERVICE -n 100"
}

cmd_rollback() {
  require_migrated
  local act prev
  act="$(active_release)"; prev="$(previous_release)"
  [ -n "$prev" ] && [ -d "$RELEASES/$prev" ] || die "keine vorherige Version vorhanden"
  [ "$prev" != "$act" ] || die "vorherige Version ist identisch mit der aktiven ($act)"
  log "Rollback: $act → $prev"
  set_link "$CURRENT" "releases/$prev"
  set_link "$PREVIOUS" "releases/$act"
  restart_and_check "$prev" || die "$prev startet nicht sauber — journalctl -u $SERVICE -n 100"
  log "Fertig: $prev aktiv"
}

cmd_list() {
  local act prev name v mark
  act="$(active_release)"; prev="$(previous_release)"
  echo "Installierte Versionen in $RELEASES:"
  for d in $(ls -1dt "$RELEASES"/*/ 2>/dev/null); do
    name="$(basename "$d")"; v="$(release_version "$name")"; mark=""
    [ "$name" = "$act" ] && mark="  ← aktiv"
    [ "$name" = "$prev" ] && mark="  ← vorherige (rollback)"
    printf '  %-12s %-10s%s\n' "$name" "${v:-?}" "$mark"
  done
}

cmd_migrate() {
  if [ -L "$CURRENT" ]; then echo "Bereits migriert."; cmd_list; return 0; fi
  [ -d "$CURRENT" ] || die "$CURRENT nicht gefunden"
  [ -f "$CURRENT/config/config.yaml" ] || die "$CURRENT/config/config.yaml fehlt"
  log "Migration: $CURRENT → $RELEASES/legacy, Config → $SHARED/config"
  svc stop || true
  mkdir -p "$RELEASES" "$SHARED"
  mv "$CURRENT" "$RELEASES/legacy"
  mv "$RELEASES/legacy/config" "$SHARED/config"
  ln -s ../../shared/config "$RELEASES/legacy/config"
  # venv bleibt in legacy/venv; sein absoluter Pfad /opt/pool_marx/pool_controller/venv
  # zeigt über den Symlink weiterhin auf genau dieses venv
  set_link "$CURRENT" "releases/legacy"
  if restart_and_check legacy; then
    log "Migration fertig. Nächster Schritt: release/deploy.sh vX.Y.Z"
  else
    die "Dienst startet nach der Migration nicht — journalctl -u $SERVICE -n 100"
  fi
}

case "${1:-}" in
  install)  shift; cmd_install "$@" ;;
  rollback) cmd_rollback ;;
  list)     cmd_list ;;
  migrate)  cmd_migrate ;;
  *) sed -n '2,19p' "$0"; exit 1 ;;
esac
