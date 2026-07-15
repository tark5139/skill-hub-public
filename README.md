# Personal Skill Hub

Personal Skill Hub is a private-first registry for immutable Agent Skills. PostgreSQL owns metadata,
permissions, lifecycle, labels, reviews and audit evidence; Tencent COS or another S3-compatible
store holds quarantined uploads and content-addressed release artifacts. The macOS CLI verifies each
download before it touches an Agent directory.

GitHub is an optional, explicitly authorized public mirror. It is never a second source of truth.
Nacos is limited to a future one-way migration or compatibility adapter.

## Version 0.1 boundary

| Surface | First-release support |
|---|---|
| Server | FastAPI, PostgreSQL, COS/S3, background worker, OpenAPI 3.1 |
| Official client | macOS 13+, Apple Silicon first |
| Formal local adapters | Codex, Claude Code, TRAE CN IDE, OpenClaw, Hermes Agent |
| Preview | WorkBuddy guided ZIP import |
| Cloud connector | Feishu Aily list/get/start plus drift mapping; no invented definition CRUD |
| Public distribution | Per-version approval to immutable GitHub Release in `tark5139/skill-hub-public` |

## Architecture

```text
macOS skillctl / Management Console / Feishu bot backends
                         │
                    Registry API v1
                         │
       ┌─────────────────┼──────────────────┐
       │                 │                  │
 PostgreSQL          Worker queue       Private search
 lifecycle + ACL     scan + verify      event feed
       │                 │
       └────────────── COS/S3 ──────────────┐
                 quarantine → immutable     │
                                            ▼
                              opt-in GitHub Release mirror
```

The exact decision is recorded in `docs/adr/0001-independent-skill-hub.md`. Package schema and the
committed API contract are under `docs/api/`.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
.venv/bin/alembic upgrade head
.venv/bin/skillhub-api
```

Run the worker in another terminal:

```bash
.venv/bin/skillhub-worker
```

Open `http://localhost:8080/console/`, enter the configured administrator token, and create a Skill.
The local profile may use SQLite and filesystem storage for tests; production configuration rejects
both and requires PostgreSQL, COS/S3 and trusted signature verification.

## CLI

```bash
skillctl login --url http://localhost:8080 --token YOUR_TOKEN
skillctl search
skillctl describe personal/hello-skill
skillctl install personal/hello-skill --agent codex --label stable
skillctl status
skillctl doctor
```

The Codex adapter installs Skills into `~/.codex/skills` by default. Use `--root` only when an
explicit nonstandard Codex Skill directory is required.

The installer downloads into a temporary directory, verifies manifest and artifact digests, rejects
unsafe ZIP paths, compares the managed baseline with local changes, creates a backup, and then uses
an atomic rename. Remote and local concurrent changes become an explicit conflict; they are never
silently overwritten.

## Publishing lifecycle

1. Create the Skill shell; visibility defaults to `private`.
2. Create an upload session and send one immutable ZIP to quarantine.
3. The worker validates ZIP structure and limits, checks secrets and license data, builds the
   server-owned manifest, verifies Ed25519, and promotes only a passing artifact.
4. Submission freezes artifact, manifest and signature state. Review evidence binds their digests.
5. Publication creates an immutable version and monotonic change events; label changes use CAS.
6. A separate explicit authorization may publish that exact public version to GitHub.

## Verification

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests migrations
SKILLHUB_ENV=test SKILLHUB_DATABASE_URL=sqlite:////tmp/skillhub.db \
  .venv/bin/alembic upgrade head
```

The test suite covers malicious archives, Ed25519 trust, API lifecycle, immutable storage, event-feed
tombstones, atomic Agent installs, conflict/rollback behavior, secure COS redirects, Aily boundaries,
and the GitHub draft → asset verification → immutable publish flow.

## macOS Apple Silicon package

The official first-release launcher targets macOS 13+ on Apple Silicon. Build a local QA artifact
with:

```bash
.venv/bin/pip install -e '.[dev]'
./ops/macos/build_cli.sh
```

Without `SKILLHUB_CODESIGN_IDENTITY`, the archive is deliberately named `*-adhoc.zip`. Official
Developer ID signing, Apple's standalone-code notarization check, clean-Mac Gatekeeper acceptance,
SHA-256 evidence, the optional stapled DMG, and the protected manual GitHub Actions path are
documented in [`docs/macos-release.md`](docs/macos-release.md). The official local command is
fail-closed unless its exact reviewed commit, full ref, version, Developer ID identity, Team ID,
and notary credential are provided and the worktree is clean.

Only a suffix-free verified artifact is eligible for an official public download. The release
automation does not create a Git tag or GitHub Release. The platform-neutral Python wheel remains
available under `dist/` for development and manual installation.

## Deployment

The approved personal MVP target is Tencent Cloud Shanghai. See
`docs/deployment-tencent-shanghai-mvp.md` for COS buckets, security groups, HTTPS, backups, migration,
and Shenzhen/Guangzhou P95 probes. Production starts with `alembic upgrade head`; API processes never
mutate schema at startup.

GitHub governance and the rule that every public Skill/version needs its own authorization are
defined in `docs/github-publication-policy.md`. Source-code approval never authorizes a Skill
Release.

## Security

Never execute files from an uploaded Skill during scanning. Production is fail-closed for trusted
signatures, keeps quarantine separate from release storage, prevents published payload mutation in
both ORM and PostgreSQL triggers, and records append-only evidence. See `SECURITY.md`.
