from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from skillhub.config import Settings
from skillhub.main import create_app
from skillhub.models import SkillVersion
from skillhub.security import ed25519_signature_payload
from skillhub.worker import _extract_license_text, process_one_scan

from .conftest import ADMIN_TOKEN, build_skill_zip


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _license_manifest(files: dict[str, bytes]) -> dict[str, Any]:
    return {
        "files": [
            {
                "path": path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, content in files.items()
        ]
    }


def _license_archive(files: list[tuple[str, bytes]]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files:
            archive.writestr(path, content)
    return target.getvalue()


def _upload(client, archive: bytes, *, signature: dict[str, str] | None = None):
    created = client.post(
        "/api/v1/skills",
        headers=_headers(),
        json={
            "namespace": "personal",
            "name": "hello-skill",
            "description": "Worker integration test",
            "visibility": "public",
        },
    )
    digest = hashlib.sha256(archive).hexdigest()
    body = {
        "version": "1.0.0",
        "expected_sha256": digest,
        "license": "Apache-2.0",
        "compatibility": ["codex", "claude-code"],
        **(signature or {}),
    }
    upload = client.post(
        f"/api/v1/skills/{created.json()['id']}/uploads",
        headers=_headers(),
        json=body,
    )
    client.put(
        upload.json()["upload_url"],
        headers={**_headers(), "Content-Type": "application/zip"},
        content=archive,
    )
    finalized = client.post(f"/api/v1/uploads/{upload.json()['id']}:finalize", headers=_headers())
    return created.json()["id"], finalized.json()["id"], digest


def test_worker_scans_and_promotes_then_change_feed_tracks_deprecation(client, app) -> None:
    archive = build_skill_zip()
    skill_id, version_id, digest = _upload(client, archive)

    assert process_one_scan(app.state.session_factory, app.state.storage, app.state.settings)
    scanned = client.get(f"/api/v1/versions/{version_id}", headers=_headers())
    assert scanned.json()["status"] == "draft"
    assert scanned.json()["scan_status"] in {"passed", "passed_with_warnings"}
    assert scanned.json()["signature_status"] == "not_required"
    assert scanned.json()["manifest"]["compatibility"] == ["claude-code", "codex"]

    assert (
        client.post(f"/api/v1/versions/{version_id}:submit", headers=_headers()).status_code == 200
    )
    assert (
        client.post(
            f"/api/v1/versions/{version_id}:approve",
            headers=_headers(),
            json={"decision": "approved", "evidence": {"test": True}},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/versions/{version_id}:publish",
            headers=_headers(),
            json={"label": "stable"},
        ).status_code
        == 200
    )

    changes = client.get("/api/v1/sync/manifest?agent=codex&limit=20", headers=_headers())
    assert changes.status_code == 200
    events = [item["event"] for item in changes.json()["items"]]
    assert events == ["version.published", "label.moved"]
    high_watermark = changes.json()["high_watermark"]
    assert high_watermark >= 2

    deprecated = client.post(f"/api/v1/versions/{version_id}:deprecate", headers=_headers())
    assert deprecated.status_code == 200
    delta = client.get(
        f"/api/v1/sync/manifest?cursor={high_watermark}&limit=20", headers=_headers()
    )
    assert {item["event"] for item in delta.json()["items"]} == {
        "label.removed",
        "version.deprecated",
    }
    assert all(item["tombstone"] for item in delta.json()["items"])

    exact = client.get(
        "/api/v1/skills/personal/hello-skill/resolve?version=1.0.0", headers=_headers()
    )
    assert exact.status_code == 200
    assert exact.json()["deprecated"] is True
    assert exact.headers["x-skill-deprecated"] == "true"
    stable = client.get(
        "/api/v1/skills/personal/hello-skill/resolve?label=stable", headers=_headers()
    )
    assert stable.status_code == 404

    artifact = client.get(exact.json()["artifact_url"], headers=_headers())
    assert hashlib.sha256(artifact.content).hexdigest() == digest

    # The exact reviewed payload cannot be unfrozen or edited in a later transaction.
    with app.state.session_factory() as session:
        version = session.get(SkillVersion, version_id)
        version.immutable = False
        with pytest.raises(ValueError, match="immutable"):
            session.commit()


def test_worker_verifies_trusted_ed25519_signature(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    settings = Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path / 'signed.db'}",
        admin_token=ADMIN_TOKEN,
        admin_subject="tark5139",
        local_storage_path=tmp_path / "storage",
        require_signature=True,
        trusted_public_keys_json={
            "tark5139:release-1": "base64:" + base64.b64encode(public_bytes).decode()
        },
    )
    app = create_app(settings)
    from fastapi.testclient import TestClient

    archive = build_skill_zip()
    digest = hashlib.sha256(archive).hexdigest()
    signature = private_key.sign(ed25519_signature_payload(digest))
    with TestClient(app) as client:
        _, version_id, _ = _upload(
            client,
            archive,
            signature={
                "signature_key_id": "tark5139:release-1",
                "signature_base64": base64.b64encode(signature).decode(),
            },
        )
        assert process_one_scan(app.state.session_factory, app.state.storage, settings)
        scanned = client.get(f"/api/v1/versions/{version_id}", headers=_headers())
        assert scanned.json()["status"] == "draft"
        assert scanned.json()["signature_status"] == "verified"


@pytest.mark.parametrize("wrapper", ["", "demo/"])
def test_license_extraction_ignores_nested_license_before_exact_root(wrapper: str) -> None:
    skill = b"---\nname: demo\ndescription: demo\nlicense: Apache-2.0\n---\n"
    nested_license = b"unreviewed nested license\n"
    root_license = b"reviewed root license\n"
    normalized_files = {
        "SKILL.md": skill,
        "docs/LICENSE": nested_license,
        "LICENSE": root_license,
    }
    package = _license_archive(
        [
            (f"{wrapper}docs/LICENSE", nested_license),
            (f"{wrapper}SKILL.md", skill),
            (f"{wrapper}LICENSE", root_license),
        ]
    )

    assert _extract_license_text(package, _license_manifest(normalized_files)) == (
        root_license.decode()
    )


def test_license_extraction_rejects_manifest_digest_mismatch() -> None:
    skill = b"skill"
    license_text = b"reviewed root license\n"
    manifest = _license_manifest({"SKILL.md": skill, "LICENSE": license_text})
    manifest["files"][1]["sha256"] = "0" * 64
    package = _license_archive([("SKILL.md", skill), ("LICENSE", license_text)])

    with pytest.raises(ValueError, match="SHA-256 does not match manifest"):
        _extract_license_text(package, manifest)


def test_license_extraction_requires_one_root_candidate_and_strict_utf8() -> None:
    skill = b"skill"
    first_license = b"first\n"
    second_license = b"second\n"
    duplicate_manifest = _license_manifest(
        {"SKILL.md": skill, "LICENSE": first_license, "COPYING": second_license}
    )
    duplicate_package = _license_archive(
        [("SKILL.md", skill), ("LICENSE", first_license), ("COPYING", second_license)]
    )
    with pytest.raises(ValueError, match="exactly one root license"):
        _extract_license_text(duplicate_package, duplicate_manifest)

    invalid_utf8 = b"license\xff"
    manifest = _license_manifest({"SKILL.md": skill, "LICENSE": invalid_utf8})
    package = _license_archive([("SKILL.md", skill), ("LICENSE", invalid_utf8)])
    with pytest.raises(ValueError, match="strict UTF-8"):
        _extract_license_text(package, manifest)
