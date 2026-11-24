# Release procedure

How versions are numbered, how a release is cut, and how it gets onto the Pi.

## 1. Versioning

[Semantic Versioning](https://semver.org/) `MAJOR.MINOR.PATCH`. Git tags are `vX.Y.Z`.

| Bump | When | Examples in this project |
|---|---|---|
| **MAJOR** | Incompatible change: something that worked before needs manual action | API route or MQTT topic renamed/removed, `config.yaml` key renamed, systemd unit/path changed |
| **MINOR** | New, backward-compatible functionality | New feature, new API route, new config section **with safe defaults** |
| **PATCH** | Bug fix only, no new behaviour | Fix a wrong log message, fix a crash, doc-only fixes may also go here |

The version lives in **one place**: [`services/version.py`](services/version.py).
API (`/api/health`, `/api/status`), dashboard footer, startup log, MQTT discovery
and telemetry all read it. Do not edit it by hand, the release script sets it.

## 2. During development

Every user-visible change gets a line under `## [Unreleased]` in
[CHANGELOG.md](CHANGELOG.md), in the same commit as the change. Use the sections
`### Added`, `### Changed`, `### Fixed`, `### Removed`.

Call out changes the Pi owner must act on, such as new config blocks, new packages
in `requirements.txt` or changed systemd files. Those go into the deploy steps (section 5).

## 3. Prerequisites (once)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # tests
gh auth login -h github.com --web                                        # GitHub release
```

## 4. Cutting a release

```bash
git switch main && git pull
python3 release/release.py 1.2.0 --dry-run   # shows the changelog entries that will ship
python3 release/release.py 1.2.0
```

The script ([release/release.py](release/release.py)):

1. **Refuses** to run if:
   - the version is not `X.Y.Z` or not higher than the current one
   - you are not on `main`, the working tree is dirty, or `main` is behind `origin/main`
   - the tag already exists or `[Unreleased]` is empty
2. Runs the full test suite and aborts on failure.
3. Sets `services/version.py`, turns `[Unreleased]` into `[1.2.0] - <date>` and
   updates the compare links.
4. Creates the commit `Release v1.2.0` and an **annotated** tag `v1.2.0`. The tag
   message contains the changelog section.
5. **Does not push.** Review first:

```bash
git show v1.2.0 --stat
```

Publish:

```bash
git push origin main v1.2.0
gh release create v1.2.0 --title "Pool Marx v1.2.0" --notes-from-tag
```

## 5. Deploying a release to the Pi

Deployment is separate from releasing: a release fixes a version once, a deploy
puts any tagged version onto the Pi, as often as needed (including rollbacks).

### Layout on the Pi

```
/opt/pool_marx/
  releases/v1.1.0/   releases/v1.2.0/    code + its own venv per version
  shared/config/                         config.yaml (kept across versions)
  pool_controller -> releases/v1.2.0     active version (systemd uses this path)
  previous        -> releases/v1.1.0     target for rollback
```

The last 3 versions are kept. The active and the previous version are never deleted.

### Setup (once)

1. Mac: create `.deploy.env` (not committed):
   ```bash
   cp release/deploy.env.example .deploy.env    # set POOL_PI=pi@<pi-ip>
   ```
2. Pi: build tools for `RPi.GPIO` (each version gets a fresh venv):
   ```bash
   sudo apt install -y python3-venv python3-dev gcc
   ```
3. Convert the existing installation into the layout above. The current
   directory becomes `releases/legacy`, the config moves to `shared/config`:
   ```bash
   release/deploy.sh --migrate
   ```

### Deploy

```bash
release/deploy.sh v1.2.0
```

What happens ([release/deploy.sh](release/deploy.sh) on the Mac,
[pi/install-release.sh](pi/install-release.sh) on the Pi):

1. `git archive` of the tag is copied to the Pi. Your working tree is not touched.
   The deploy asks before deploying a tag that is not pushed yet.
2. Unpacked into `releases/v1.2.0`, `config` linked to `shared/config`.
   The version's template config is kept as `config.template/`.
3. A new venv is built and `requirements.txt` installed. This takes a few minutes the first
   time, then the pip cache makes it faster. If this fails, the active version stays untouched.
4. Checks:
   - warns if `shared/config/config.yaml` lacks sections the new version knows
     (defaults apply; add them by hand)
   - warns if `pi/pool_marx.override.conf` differs from the installed one
5. Switch `pool_controller` to the new version (atomic symlink), restart the service.
6. Health check: the service must be active and `/api/health` must report the new
   version within 60 s. **Otherwise it rolls back automatically** to the previous version.

The sudo password for `systemctl` may be asked for.

### Rollback and overview

```bash
release/deploy.sh --rollback   # switch back to the previous version (and restart)
release/deploy.sh --list       # installed versions, active and previous marked
```

Rolling back keeps the config as it is. Older versions ignore config keys they
do not know.

## 6. Mistakes

| Situation | What to do |
|---|---|
| Release created locally, **not pushed** | `git tag -d v1.2.0 && git reset --hard HEAD~1`, fix, run the script again |
| Tag already **pushed** | Never move or delete a published tag. Fix on `main` and release `1.2.1`. |
| Bad GitHub release notes | Edit them on GitHub (`gh release edit v1.2.0`). The tag stays. |
| Urgent fix while `main` has unreleased features | Release them together as a patch only if they are safe. Otherwise finish them first. Hotfix branches are not used in this project. |

## 7. Checklist

- [ ] All changes have changelog entries under `[Unreleased]`
- [ ] Version bump matches the changes (section 1)
- [ ] `release/release.py X.Y.Z` succeeded (tests green)
- [ ] `git show vX.Y.Z` reviewed
- [ ] Pushed `main` and the tag, GitHub release created
- [ ] `release/deploy.sh vX.Y.Z` succeeded, no config warnings left, dashboard footer shows the new version
