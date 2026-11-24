#!/usr/bin/env python3
"""
Pool Marx — Release erstellen (lokal, ohne Push)

    python3 release/release.py 1.2.0           # Release vorbereiten, committen, taggen
    python3 release/release.py 1.2.0 --dry-run # nur prüfen und anzeigen

Ablauf:
  1. Prüft: Versionsformat X.Y.Z, größer als die aktuelle, Branch main,
     Arbeitsbaum sauber, nicht hinter origin/main, Tag existiert noch nicht,
     Abschnitt [Unreleased] in CHANGELOG.md ist nicht leer.
  2. Führt die Tests aus (.venv/bin/pytest -q).
  3. Setzt services/version.py, macht aus [Unreleased] den Abschnitt
     [X.Y.Z] - Datum, aktualisiert die Vergleichslinks.
  4. Commit "Release vX.Y.Z" und annotierter Tag vX.Y.Z.
  5. Gibt die Befehle zum Veröffentlichen aus (Push + GitHub Release).
     Gepusht wird NICHT automatisch.
"""

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "services" / "version.py"
CHANGELOG = ROOT / "CHANGELOG.md"
REPO_URL = "https://github.com/MarxDimitri/Pool_Controller"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def die(msg: str):
    print(f"FEHLER: {msg}", file=sys.stderr)
    sys.exit(1)


def git(*args, check=True) -> str:
    res = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and res.returncode != 0:
        die(f"git {' '.join(args)}: {res.stderr.strip()}")
    return res.stdout.strip()


def current_version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', VERSION_FILE.read_text())
    if not m:
        die("__version__ in services/version.py nicht gefunden")
    return m.group(1)


def parse(v: str) -> tuple:
    m = SEMVER.match(v)
    if not m:
        die(f"'{v}' ist keine Version im Format X.Y.Z")
    return tuple(int(x) for x in m.groups())


def unreleased_notes(text: str) -> str:
    m = re.search(r"^## \[Unreleased\]\s*\n(.*?)(?=^## \[|\Z)", text, re.S | re.M)
    if not m:
        die("CHANGELOG.md hat keinen Abschnitt '## [Unreleased]'")
    return m.group(1).strip()


def bump_changelog(text: str, new: str, today: str) -> str:
    """[Unreleased] → [new] - today, neuer leerer [Unreleased], Links anpassen."""
    text = re.sub(r"^## \[Unreleased\]\s*\n", f"## [Unreleased]\n\n## [{new}] - {today}\n\n",
                  text, count=1, flags=re.M)
    text = re.sub(r"^\[Unreleased\]: .*$",
                  f"[Unreleased]: {REPO_URL}/compare/v{new}...HEAD\n"
                  f"[{new}]: {REPO_URL}/releases/tag/v{new}",
                  text, count=1, flags=re.M)
    return text


def section_for(text: str, version: str) -> str:
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[|\Z)", text, re.S | re.M)
    return m.group(1).strip() if m else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Release erstellen (Commit + Tag, ohne Push)")
    ap.add_argument("version", help="neue Version, z. B. 1.2.0")
    ap.add_argument("--dry-run", action="store_true", help="nur prüfen, nichts ändern")
    ap.add_argument("--skip-tests", action="store_true", help="Tests nicht ausführen (nicht empfohlen)")
    args = ap.parse_args()

    new, cur = args.version.lstrip("v"), current_version()
    if parse(new) <= parse(cur):
        die(f"{new} ist nicht größer als die aktuelle Version {cur}")
    tag = f"v{new}"

    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        die("Releases nur von Branch main")
    if git("status", "--porcelain"):
        die("Arbeitsbaum nicht sauber — erst committen")
    if git("tag", "--list", tag):
        die(f"Tag {tag} existiert bereits")
    git("fetch", "-q", "origin", check=False)
    behind = git("rev-list", "--count", "HEAD..origin/main", check=False) or "0"
    if behind != "0":
        die(f"lokales main ist {behind} Commit(s) hinter origin/main — erst pullen")

    text = CHANGELOG.read_text()
    notes = unreleased_notes(text)
    if not notes:
        die("Abschnitt [Unreleased] in CHANGELOG.md ist leer — Änderungen eintragen")

    print(f"Release {cur} → {new}\n\nÄnderungen:\n{notes}\n")
    if args.dry_run:
        print("--dry-run: nichts geändert.")
        return 0

    if not args.skip_tests:
        pytest = ROOT / ".venv" / "bin" / "pytest"
        if not pytest.exists():
            die(".venv/bin/pytest fehlt — siehe docs/INBETRIEBNAHME.md Kapitel 14")
        print("Tests laufen …")
        if subprocess.run([str(pytest), "-q", "-p", "no:warnings"], cwd=ROOT).returncode != 0:
            die("Tests fehlgeschlagen — kein Release")

    VERSION_FILE.write_text(re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{new}"',
                                   VERSION_FILE.read_text()))
    text = bump_changelog(text, new, date.today().isoformat())
    CHANGELOG.write_text(text)

    git("add", str(VERSION_FILE), str(CHANGELOG))
    git("commit", "-q", "-m", f"Release {tag}")
    # --cleanup=verbatim: sonst entfernt Git die "### Added"-Überschriften (Zeilen mit #)
    git("tag", "-a", "--cleanup=verbatim", tag, "-m", f"Pool Marx {tag}\n\n{section_for(text, new)}")

    print(f"\nRelease {tag} lokal erstellt (Commit + annotierter Tag).")
    print("Prüfen mit:   git show " + tag)
    print("Veröffentlichen:")
    print(f"  git push origin main {tag}")
    print(f"  gh release create {tag} --title 'Pool Marx {tag}' --notes-from-tag")
    print(f"Rückgängig (nur solange nicht gepusht):  git tag -d {tag} && git reset --hard HEAD~1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
