"""Detached Ed25519 signature verification for immutable skill archives."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .models import Ed25519KeyMaterial, ScanIssue, ScanPolicy

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_MAX_SIDECAR_SIZE = 16 * 1024


@dataclass(frozen=True, slots=True)
class SignatureVerification:
    key_id: str | None
    record: dict[str, object] | None
    issues: tuple[ScanIssue, ...]


def ed25519_signature_payload(artifact_sha256: str) -> bytes:
    """Return the domain-separated bytes publishers must sign.

    Signing the digest rather than loading an arbitrarily large archive into memory
    still binds the signature to the exact archive bytes.
    """

    digest = artifact_sha256.lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("artifact_sha256 must be 64 lowercase hexadecimal characters")
    return b"skillhub-artifact-v1\x00" + bytes.fromhex(digest)


def default_signature_path(archive_path: Path) -> Path:
    """Return the unambiguous detached sidecar path for an archive."""

    return archive_path.with_name(f"{archive_path.name}.sig.json")


def verify_detached_signature(
    archive_path: Path,
    artifact_sha256: str,
    policy: ScanPolicy,
) -> SignatureVerification:
    sidecar = policy.detached_signature_path or default_signature_path(archive_path)
    if sidecar.is_symlink():
        return _signature_error(
            "SIGNATURE_SIDECAR_TYPE", "Signature sidecar must be a regular file."
        )
    if not sidecar.exists():
        if policy.require_signature:
            return SignatureVerification(
                None,
                None,
                (
                    ScanIssue(
                        "SIGNATURE_REQUIRED",
                        "error",
                        "A trusted detached Ed25519 signature is required.",
                    ),
                ),
            )
        return SignatureVerification(None, None, ())

    if not sidecar.is_file():
        return _signature_error(
            "SIGNATURE_SIDECAR_TYPE", "Signature sidecar must be a regular file."
        )

    try:
        if sidecar.stat().st_size > _MAX_SIDECAR_SIZE:
            return _signature_error(
                "SIGNATURE_SIDECAR_TOO_LARGE",
                f"Signature sidecar exceeds {_MAX_SIDECAR_SIZE} bytes.",
            )
        raw = sidecar.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _signature_error("SIGNATURE_INVALID", "Signature sidecar is not valid JSON.")

    required_keys = {"schema_version", "algorithm", "key_id", "artifact_sha256", "signature"}
    if not isinstance(document, dict) or set(document) != required_keys:
        return _signature_error(
            "SIGNATURE_SCHEMA_INVALID",
            "Signature sidecar fields do not match the version 1 schema.",
        )
    if document["schema_version"] != "1.0" or document["algorithm"] != "ed25519":
        return _signature_error(
            "SIGNATURE_ALGORITHM_UNSUPPORTED",
            "Only schema 1.0 with Ed25519 is accepted.",
        )

    key_id = document["key_id"]
    signed_digest = document["artifact_sha256"]
    encoded_signature = document["signature"]
    if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
        return _signature_error("SIGNATURE_KEY_ID_INVALID", "Signature key_id is invalid.")
    if not isinstance(signed_digest, str) or not _SHA256_RE.fullmatch(signed_digest):
        return _signature_error("SIGNATURE_DIGEST_INVALID", "Signed artifact digest is invalid.")
    if not hmac.compare_digest(signed_digest, artifact_sha256):
        return _signature_error(
            "SIGNATURE_DIGEST_MISMATCH",
            "Signature sidecar does not describe this archive.",
            key_id,
        )
    if not isinstance(encoded_signature, str):
        return _signature_error(
            "SIGNATURE_ENCODING_INVALID", "Signature must be base64 text.", key_id
        )
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
    except (binascii.Error, ValueError):
        return _signature_error(
            "SIGNATURE_ENCODING_INVALID", "Signature is not valid base64.", key_id
        )
    if len(signature) != 64:
        return _signature_error(
            "SIGNATURE_LENGTH_INVALID",
            "An Ed25519 signature must contain 64 bytes.",
            key_id,
        )

    key_material = policy.trusted_ed25519_keys.get(key_id)
    if key_material is None and policy.trusted_key_resolver is not None:
        try:
            key_material = policy.trusted_key_resolver(key_id)
        except Exception:  # A trust service failure must never become an implicit trust decision.
            return _signature_error(
                "SIGNATURE_TRUST_LOOKUP_FAILED",
                "Trusted-key lookup failed closed.",
                key_id,
            )
    if key_material is None:
        return _signature_error(
            "SIGNATURE_KEY_UNTRUSTED",
            "Signature key_id is not in the configured trust store.",
            key_id,
        )
    try:
        public_key = _load_public_key(key_material)
        public_key.verify(signature, ed25519_signature_payload(artifact_sha256))
    except (TypeError, ValueError, InvalidSignature):
        return _signature_error(
            "SIGNATURE_VERIFICATION_FAILED",
            "Detached signature verification failed.",
            key_id,
        )

    sidecar_sha256 = hashlib.sha256(raw).hexdigest()
    return SignatureVerification(
        key_id,
        {
            "algorithm": "ed25519",
            "key_id": key_id,
            "artifact_sha256": artifact_sha256,
            "sidecar_sha256": sidecar_sha256,
            "verified": True,
        },
        (),
    )


def _load_public_key(material: Ed25519KeyMaterial) -> Ed25519PublicKey:
    if isinstance(material, Ed25519PublicKey):
        return material
    if isinstance(material, str):
        if material.startswith("base64:"):
            try:
                material = base64.b64decode(material.removeprefix("base64:"), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("invalid base64 public key") from exc
        else:
            material = material.encode("ascii")
    if not isinstance(material, bytes):
        raise TypeError("unsupported Ed25519 public key material")
    if len(material) == 32:
        return Ed25519PublicKey.from_public_bytes(material)
    loaded = serialization.load_pem_public_key(material)
    if not isinstance(loaded, Ed25519PublicKey):
        raise ValueError("trusted key is not Ed25519")
    return loaded


def _signature_error(code: str, message: str, key_id: str | None = None) -> SignatureVerification:
    return SignatureVerification(key_id, None, (ScanIssue(code, "error", message),))
