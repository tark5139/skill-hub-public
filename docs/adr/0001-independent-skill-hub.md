# ADR-0001: Independent Skill Hub is the system of record

- Status: accepted
- Date: 2026-07-14
- Decision owner: tark5139

## Context

The system must distribute immutable Skills to several local and cloud Agent products while
preserving private discovery, explicit release governance, integrity verification, and a stable
contract. Nacos is useful as a migration source but must not become a second writer.

## Decision

Use PostgreSQL as the authority for metadata, permissions, lifecycle state, labels, reviews, and
audit events. Store quarantined uploads and immutable, content-addressed release artifacts in an
S3-compatible object store (Tencent COS in production). Expose a versioned Registry REST API.
A worker validates archives, builds the server manifest, checks signatures and policy, and promotes
artifacts. A macOS CLI verifies and atomically materializes Skills into Agent-specific locations.

Nacos is one-way only. GitHub is an explicitly authorized public distribution mirror and is never
the source of truth.

## First-release boundaries

- Official client: macOS 13 or newer on Apple Silicon.
- Local adapters: Codex, Claude Code, TRAE CN IDE, OpenClaw, and Hermes Agent.
- Preview adapter: WorkBuddy guided import.
- Cloud connector: Feishu Aily discovery/invocation and drift reporting; its public API does not
  currently provide complete Skill-definition CRUD.
- Governance: authorization-aware private search, SHA-256 integrity, signature verification,
  append-only review/audit evidence, and per-version GitHub publication authorization.
- Deployment: personal MVP in Tencent Cloud Shanghai, with P95 probes from Shenzhen and Guangzhou.

## Consequences

The core API and package format remain cloud- and Agent-neutral. SQLite and local filesystem storage
may be used only for local tests; production configuration must use PostgreSQL and COS/S3. Public
GitHub releases are derived artifacts and cannot mutate an already published Skill version.
