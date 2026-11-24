#!/usr/bin/env bash
# Pool Marx — getaggte Version auf den Raspberry Pi bringen (vom Mac aus)
#
#   scripts/deploy.sh v1.2.0      Version installieren, umschalten, prüfen (Auto-Rollback bei Fehler)
#   scripts/deploy.sh --rollback  zur vorherigen Version zurück
#   scripts/deploy.sh --list      installierte Versionen auf dem Pi anzeigen
#   scripts/deploy.sh --migrate   einmalig: Pi auf die Release-Struktur umstellen
#
# Ziel-Pi aus .deploy.env im Projektordner (nicht im Repo), z. B.:
#   POOL_PI=pi@<pi-ip>
# Vorlage: deploy/deploy.env.example
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
die() { printf 'FEHLER: %s\n' "$*" >&2; exit 1; }

# shellcheck disable=SC1091
[ -f "$ROOT/.deploy.env" ] && source "$ROOT/.deploy.env"
[ -n "${POOL_PI:-}" ] || die "POOL_PI nicht gesetzt — .deploy.env anlegen (Vorlage: deploy/deploy.env.example)"

INSTALLER_LOCAL="$ROOT/deploy/install-release.sh"
INSTALLER_REMOTE="/tmp/pool_marx-install-release.sh"
PATHS=(main.py requirements.txt api services web deploy scripts config)

# Installer immer aus dem aktuellen Arbeitsstand (funktioniert auch für ältere Tags)
push_installer() { scp -q "$INSTALLER_LOCAL" "$POOL_PI:$INSTALLER_REMOTE"; }

case "${1:-}" in
  --rollback) push_installer; ssh -t "$POOL_PI" "bash $INSTALLER_REMOTE rollback" ;;
  --list)     push_installer; ssh "$POOL_PI" "bash $INSTALLER_REMOTE list" ;;
  --migrate)  push_installer; ssh -t "$POOL_PI" "bash $INSTALLER_REMOTE migrate" ;;
  v*)
    TAG="$1"
    [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Tag muss vX.Y.Z sein"
    git -C "$ROOT" rev-parse -q --verify "refs/tags/$TAG" >/dev/null || die "Tag $TAG existiert lokal nicht"
    if ! git -C "$ROOT" ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
      echo "WARNUNG: $TAG ist noch nicht auf GitHub gepusht."
      read -r -p "Trotzdem deployen? [j/N] " a; [ "$a" = "j" ] || exit 1
    fi
    TARBALL="/tmp/pool_marx-$TAG.tar"
    echo "==> Übertrage $TAG nach $POOL_PI"
    git -C "$ROOT" archive --format=tar "$TAG" "${PATHS[@]}" | ssh "$POOL_PI" "cat > $TARBALL"
    push_installer
    ssh -t "$POOL_PI" "bash $INSTALLER_REMOTE install $TAG $TARBALL; rc=\$?; rm -f $TARBALL; exit \$rc"
    ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
