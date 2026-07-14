"""Public data models and policy controls for skill archive scanning."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

type IssueSeverity = Literal["error", "warning"]
type ScanStatus = Literal["passed", "passed_with_warnings", "failed"]
type Ed25519KeyMaterial = Ed25519PublicKey | bytes | str
type TrustedKeyResolver = Callable[[str], Ed25519KeyMaterial | None]


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    """Fail-closed limits and trust inputs for a single archive scan.

    Detached signatures use ``<archive filename>.sig.json`` unless
    ``detached_signature_path`` is supplied. ``secret_exemptions`` contains exact
    finding fingerprints returned by an earlier scan; exemptions never use broad
    path or rule wildcards.
    """

    max_archive_size: int = 50 * 1024 * 1024
    max_entries: int = 512
    max_file_size: int = 10 * 1024 * 1024
    max_total_uncompressed_size: int = 50 * 1024 * 1024
    max_frontmatter_size: int = 64 * 1024
    max_compression_ratio: float = 100.0
    max_secret_findings: int = 100
    require_license: bool = True
    allowed_licenses: frozenset[str] | None = None
    warn_on_scripts: bool = True
    scripts_are_errors: bool = False
    secret_exemptions: frozenset[str] = field(default_factory=frozenset)
    require_signature: bool = False
    detached_signature_path: Path | None = None
    trusted_ed25519_keys: Mapping[str, Ed25519KeyMaterial] = field(default_factory=dict)
    trusted_key_resolver: TrustedKeyResolver | None = None

    def __post_init__(self) -> None:
        integer_limits = {
            "max_archive_size": self.max_archive_size,
            "max_entries": self.max_entries,
            "max_file_size": self.max_file_size,
            "max_total_uncompressed_size": self.max_total_uncompressed_size,
            "max_frontmatter_size": self.max_frontmatter_size,
            "max_secret_findings": self.max_secret_findings,
        }
        for label, value in integer_limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if (
            isinstance(self.max_compression_ratio, bool)
            or not isinstance(self.max_compression_ratio, (int, float))
            or not math.isfinite(self.max_compression_ratio)
            or self.max_compression_ratio <= 0
        ):
            raise ValueError("max_compression_ratio must be a finite positive number")
        if self.allowed_licenses is not None:
            allowed_licenses = frozenset(self.allowed_licenses)
            if any(not isinstance(value, str) or not value for value in allowed_licenses):
                raise ValueError("allowed_licenses must contain non-empty strings")
            object.__setattr__(self, "allowed_licenses", allowed_licenses)
        if any(not isinstance(value, str) or not value for value in self.secret_exemptions):
            raise ValueError("secret_exemptions must contain non-empty strings")
        object.__setattr__(self, "secret_exemptions", frozenset(self.secret_exemptions))
        if self.detached_signature_path is not None:
            object.__setattr__(
                self,
                "detached_signature_path",
                Path(self.detached_signature_path),
            )
        if self.trusted_key_resolver is not None and not callable(self.trusted_key_resolver):
            raise ValueError("trusted_key_resolver must be callable")
        object.__setattr__(
            self,
            "trusted_ed25519_keys",
            MappingProxyType(dict(self.trusted_ed25519_keys)),
        )


@dataclass(frozen=True, slots=True)
class ScanIssue:
    """A stable, non-secret-bearing scanner finding."""

    code: str
    severity: IssueSeverity
    message: str
    path: str | None = None
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """Immutable metadata calculated from a package file."""

    path: str
    size: int
    sha256: str
    media_type: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Complete scanner outcome, including a publishable manifest on success."""

    passed: bool
    scan_status: ScanStatus
    namespace: str
    name: str
    version: str
    artifact_sha256: str
    files: tuple[ScannedFile, ...]
    issues: tuple[ScanIssue, ...]
    manifest: dict[str, object] | None
    description: str | None = None
    license: str | None = None
    skill_root: str | None = None
    signature_key_id: str | None = None
