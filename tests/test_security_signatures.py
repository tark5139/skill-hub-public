from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from skillhub.security import (
    ScanPolicy,
    default_signature_path,
    ed25519_signature_payload,
    scan_skill_archive,
)


def _archive(tmp_path: Path) -> Path:
    archive = tmp_path / "signed.zip"
    skill_md = (
        "---\nname: demo\ndescription: Signed demo.\nlicense: Apache-2.0\n---\n# Signed demo\n"
    )
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("SKILL.md", skill_md)
    return archive


def _write_signature(
    archive: Path,
    private_key: Ed25519PrivateKey,
    *,
    key_id: str = "publisher:test",
) -> str:
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    signature = private_key.sign(ed25519_signature_payload(digest))
    default_signature_path(archive).write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "algorithm": "ed25519",
                "key_id": key_id,
                "artifact_sha256": digest,
                "signature": base64.b64encode(signature).decode(),
            }
        ),
        encoding="utf-8",
    )
    return digest


def _raw_public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _scan(path: Path, policy: ScanPolicy):
    return scan_skill_archive(
        path,
        namespace="personal",
        name="demo",
        version="1.0.0",
        policy=policy,
    )


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_required_trusted_ed25519_signature_is_verified(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    digest = _write_signature(archive, private_key)

    result = _scan(
        archive,
        ScanPolicy(
            require_signature=True,
            trusted_ed25519_keys={"publisher:test": _raw_public_key(private_key)},
        ),
    )

    assert result.passed is True
    assert result.signature_key_id == "publisher:test"
    assert result.manifest is not None
    assert result.manifest["signatures"][0]["artifact_sha256"] == digest
    assert result.manifest["signatures"][0]["verified"] is True


def test_key_id_resolver_is_supported(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    _write_signature(archive, private_key)
    looked_up: list[str] = []

    def resolver(key_id: str) -> bytes | None:
        looked_up.append(key_id)
        return _raw_public_key(private_key) if key_id == "publisher:test" else None

    result = _scan(
        archive,
        ScanPolicy(require_signature=True, trusted_key_resolver=resolver),
    )

    assert result.passed is True
    assert looked_up == ["publisher:test"]


def test_missing_required_signature_fails_closed(tmp_path: Path) -> None:
    result = _scan(_archive(tmp_path), ScanPolicy(require_signature=True))

    assert result.passed is False
    assert "SIGNATURE_REQUIRED" in _codes(result)


def test_unknown_key_id_is_not_trusted(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    _write_signature(archive, private_key, key_id="unknown")

    result = _scan(
        archive,
        ScanPolicy(
            require_signature=True,
            trusted_ed25519_keys={"other": _raw_public_key(private_key)},
        ),
    )

    assert result.passed is False
    assert "SIGNATURE_KEY_UNTRUSTED" in _codes(result)


def test_archive_tampering_invalidates_detached_signature(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    _write_signature(archive, private_key)
    archive.write_bytes(archive.read_bytes() + b"tamper")

    result = _scan(
        archive,
        ScanPolicy(
            require_signature=True,
            trusted_ed25519_keys={"publisher:test": _raw_public_key(private_key)},
        ),
    )

    assert result.passed is False
    assert "SIGNATURE_DIGEST_MISMATCH" in _codes(result)


def test_wrong_cryptographic_signature_fails_even_when_digest_matches(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    trusted_private_key = Ed25519PrivateKey.generate()
    _write_signature(archive, trusted_private_key)
    sidecar_path = default_signature_path(archive)
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    digest = sidecar["artifact_sha256"]
    sidecar["signature"] = base64.b64encode(
        Ed25519PrivateKey.generate().sign(ed25519_signature_payload(digest))
    ).decode()
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    result = _scan(
        archive,
        ScanPolicy(
            require_signature=True,
            trusted_ed25519_keys={"publisher:test": _raw_public_key(trusted_private_key)},
        ),
    )

    assert result.passed is False
    assert "SIGNATURE_VERIFICATION_FAILED" in _codes(result)


def test_malformed_sidecar_is_rejected_even_when_signature_is_optional(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    default_signature_path(archive).write_text("not-json", encoding="utf-8")

    result = _scan(archive, ScanPolicy(require_signature=False))

    assert result.passed is False
    assert "SIGNATURE_INVALID" in _codes(result)


def test_trust_lookup_failure_fails_closed_without_leaking_exception(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    _write_signature(archive, private_key)

    def failing_resolver(_: str):
        raise RuntimeError("trust backend detail must not escape")

    result = _scan(
        archive,
        ScanPolicy(require_signature=True, trusted_key_resolver=failing_resolver),
    )

    assert result.passed is False
    assert "SIGNATURE_TRUST_LOOKUP_FAILED" in _codes(result)
    assert all("backend detail" not in issue.message for issue in result.issues)
