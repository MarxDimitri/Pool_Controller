# release/ — läuft auf deinem Rechner

Werkzeuge, die du auf dem Mac aufrufst, um ein Release zu bauen und auf den Pi
zu bringen. Nichts davon wird auf den Pi kopiert. Ablauf: [RELEASE.md](../RELEASE.md).

| Datei | Zweck |
|---|---|
| `release.py` | Release erstellen: Tests, Version, Changelog, Commit, Tag |
| `deploy.sh` | Getaggte Version auf den Pi bringen, `--rollback`, `--list`, `--migrate` |
| `deploy.env.example` | Vorlage für `.deploy.env` (Adresse des Pi) |
