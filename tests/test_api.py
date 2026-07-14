from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select

from skillhub.models import Label, SkillVersion

from .conftest import build_skill_zip


def test_authentication_is_required_and_problem_details_are_used(client) -> None:
    response = client.get("/api/v1/skills")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"].endswith("authentication_required")
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_management_console_is_packaged_with_strict_browser_headers(client) -> None:
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/console/"
    console = client.get("/console/")
    assert console.status_code == 200
    assert "Personal Skill Hub" in console.text
    assert "unsafe-inline" not in console.headers["content-security-policy"]
    assert console.headers["x-content-type-options"] == "nosniff"


def test_capabilities_are_authenticated_and_do_not_expose_secrets(client, auth_headers) -> None:
    unauthenticated = client.get("/api/v1/system/capabilities")
    assert unauthenticated.status_code == 401

    response = client.get("/api/v1/system/capabilities", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["official_client_os"] == "macOS 13+"
    assert body["official_client_arch"] == "Apple Silicon (arm64)"
    assert body["github_repository"] == "skill-hub-public"
    assert {item["name"]: item["mode"] for item in body["agents"]}["WorkBuddy"] == "preview"
    assert {item["name"]: item["mode"] for item in body["agents"]}["Feishu Aily"] == (
        "cloud_connector"
    )
    assert "token" not in response.text.lower()


def test_create_search_detail_and_idempotency(client, auth_headers) -> None:
    body = {
        "namespace": "personal",
        "name": "hello-skill",
        "description": "Searchable private Skill",
        "visibility": "private",
        "tags": ["Demo", "demo", "safe"],
    }
    headers = {**auth_headers, "Idempotency-Key": "create-hello"}
    created = client.post("/api/v1/skills", headers=headers, json=body)
    assert created.status_code == 201
    assert created.json()["tags"] == ["demo", "safe"]
    assert created.json()["updated_at"].endswith("Z")

    replay = client.post("/api/v1/skills", headers=headers, json=body)
    assert replay.status_code == 201
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json()["id"] == created.json()["id"]

    changed = {**body, "description": "different"}
    conflict = client.post("/api/v1/skills", headers=headers, json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["type"].endswith("idempotency_key_reused")

    search = client.get("/api/v1/skills?q=searchable&tag=safe", headers=auth_headers)
    assert search.status_code == 200
    assert [item["name"] for item in search.json()["items"]] == ["hello-skill"]

    detail = client.get("/api/v1/skills/personal/hello-skill", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["visibility"] == "private"


def test_full_publish_resolve_and_download_contract(client, app, auth_headers) -> None:
    archive = build_skill_zip()
    digest = hashlib.sha256(archive).hexdigest()
    created = client.post(
        "/api/v1/skills",
        headers=auth_headers,
        json={
            "namespace": "personal",
            "name": "hello-skill",
            "description": "End-to-end test",
            "visibility": "public",
        },
    )
    skill_id = created.json()["id"]
    upload = client.post(
        f"/api/v1/skills/{skill_id}/uploads",
        headers=auth_headers,
        json={
            "version": "1.0.0",
            "expected_sha256": digest,
            "license": "Apache-2.0",
            "compatibility": ["codex", "claude-code"],
        },
    )
    assert upload.status_code == 201
    upload_id = upload.json()["id"]
    stored = client.put(
        f"/api/v1/uploads/{upload_id}/content",
        headers={**auth_headers, "Content-Type": "application/zip"},
        content=archive,
    )
    assert stored.status_code == 200
    assert stored.json()["sha256"] == digest
    finalized = client.post(f"/api/v1/uploads/{upload_id}:finalize", headers=auth_headers)
    assert finalized.status_code == 202
    version_id = finalized.json()["id"]
    assert finalized.json()["status"] == "pending_scan"

    # Worker behavior is covered separately; seed its successful output to exercise API lifecycle.
    artifact_key = f"artifacts/sha256/{digest[:2]}/{digest}/artifact.zip"
    app.state.storage.put_release(artifact_key, archive, content_type="application/zip")
    manifest = {
        "schema_version": "1.0",
        "namespace": "personal",
        "name": "hello-skill",
        "version": "1.0.0",
        "description": "A safe test skill.",
        "compatibility": ["claude-code", "codex"],
        "files": [],
        "artifact_sha256": digest,
        "license": "Apache-2.0",
        "signatures": [],
        "scan_status": "passed",
        "created_at": datetime.now(UTC).isoformat(),
    }
    with app.state.session_factory() as session:
        version = session.get(SkillVersion, version_id)
        version.status = "draft"
        version.scan_status = "passed"
        version.signature_status = "not_required"
        version.artifact_key = artifact_key
        version.manifest_key = "manifests/personal/hello-skill/1.0.0.json"
        version.manifest_json = manifest
        session.commit()

    submitted = client.post(f"/api/v1/versions/{version_id}:submit", headers=auth_headers)
    assert submitted.json()["status"] == "submitted"
    approved = client.post(
        f"/api/v1/versions/{version_id}:approve",
        headers=auth_headers,
        json={"decision": "approved", "evidence": {"reviewer_note": "safe"}},
    )
    assert approved.json()["status"] == "approved"
    published = client.post(
        f"/api/v1/versions/{version_id}:publish",
        headers=auth_headers,
        json={"label": "stable"},
    )
    assert published.json()["status"] == "published"

    resolved = client.get(
        "/api/v1/skills/personal/hello-skill/resolve?label=stable", headers=auth_headers
    )
    assert resolved.status_code == 200
    assert resolved.json()["artifact_sha256"] == digest
    assert resolved.headers["etag"] == f'"sha256:{digest}"'
    cached = client.get(
        "/api/v1/skills/personal/hello-skill/resolve?label=stable",
        headers={**auth_headers, "If-None-Match": resolved.headers["etag"]},
    )
    assert cached.status_code == 304

    artifact = client.get(resolved.json()["artifact_url"], headers=auth_headers)
    assert artifact.status_code == 200
    assert artifact.content == archive
    assert artifact.headers["x-skill-resolved-version"] == "1.0.0"
    assert artifact.headers["digest"].startswith("sha-256=")

    manifest_response = client.get(resolved.json()["manifest_url"], headers=auth_headers)
    assert manifest_response.status_code == 200
    assert manifest_response.json()["artifact_sha256"] == digest

    ambiguous = client.get(
        "/api/v1/skills/personal/hello-skill/resolve?version=1.0.0&label=stable",
        headers=auth_headers,
    )
    assert ambiguous.status_code == 400

    with app.state.session_factory() as session:
        stable = session.scalar(
            select(Label).where(Label.skill_id == skill_id, Label.label == "stable")
        )
        stable_etag = stable.etag
    missing_precondition = client.put(
        f"/api/v1/skills/{skill_id}/labels/stable",
        headers=auth_headers,
        json={"version": "1.0.0"},
    )
    assert missing_precondition.status_code == 428
    moved = client.put(
        f"/api/v1/skills/{skill_id}/labels/stable",
        headers={**auth_headers, "If-Match": stable_etag},
        json={"version": "1.0.0"},
    )
    assert moved.status_code == 200
    assert moved.headers["etag"] != stable_etag

    authorized = client.post(
        f"/api/v1/versions/{version_id}/github-authorization",
        headers=auth_headers,
        json={
            "confirmation": "PUBLISH_PUBLICLY",
            "license_confirmed": True,
            "sensitive_content_reviewed": True,
        },
    )
    assert authorized.status_code == 202
    assert authorized.json()["status"] == "authorized"


def test_upload_digest_mismatch_fails_closed(client, auth_headers) -> None:
    created = client.post(
        "/api/v1/skills",
        headers=auth_headers,
        json={
            "namespace": "personal",
            "name": "bad-digest",
            "description": "Digest test",
        },
    )
    upload = client.post(
        f"/api/v1/skills/{created.json()['id']}/uploads",
        headers=auth_headers,
        json={"version": "1.0.0", "expected_sha256": "0" * 64},
    )
    response = client.put(
        upload.json()["upload_url"],
        headers={**auth_headers, "Content-Type": "application/zip"},
        content=build_skill_zip("bad-digest"),
    )
    assert response.status_code == 422
    assert response.json()["type"].endswith("digest_mismatch")
