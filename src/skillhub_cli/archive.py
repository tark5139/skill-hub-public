"""Artifact and manifest verification before any Agent directory is touched."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import IntegrityError

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(payload: dict[str, Any]) -> str:
    """Hash server-canonical UTF-8 JSON: sorted compact keys and no trailing newline."""

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_relative_path(raw: str) -> PurePosixPath:
    if not raw or "\x00" in raw or "\\" in raw:
        raise IntegrityError(f"Unsafe archive/manifest path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise IntegrityError(f"Unsafe archive/manifest path: {raw!r}")
    return path


def safe_extract_zip(
    archive_path: Path,
    destination: Path,
    *,
    max_files: int = 10_000,
    max_total_bytes: int = 512 * 1024 * 1024,
) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    total = 0
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise IntegrityError(f"Artifact is not a valid ZIP: {exc}") from exc

    with archive:
        members = archive.infolist()
        if len(members) > max_files:
            raise IntegrityError("Artifact contains too many entries")
        for member in members:
            relative = _safe_relative_path(member.filename.rstrip("/"))
            normalized = relative.as_posix()
            if normalized in seen:
                raise IntegrityError(f"Duplicate archive entry: {normalized}")
            seen.add(normalized)
            unix_mode = member.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise IntegrityError(f"Symlinks are not allowed in Skill artifacts: {normalized}")
            if member.flag_bits & 0x1:
                raise IntegrityError(f"Encrypted ZIP entries are not allowed: {normalized}")
            total += member.file_size
            if total > max_total_bytes:
                raise IntegrityError("Artifact uncompressed size exceeds configured limit")

            target = destination.joinpath(*relative.parts)
            resolved = target.resolve()
            if not resolved.is_relative_to(destination.resolve()):
                raise IntegrityError(f"Archive path escapes destination: {normalized}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            safe_mode = unix_mode & 0o777
            if safe_mode:
                os.chmod(target, safe_mode & ~0o6000)
    return destination


def validate_manifest(
    manifest: dict[str, Any],
    *,
    namespace: str,
    name: str,
    version: str,
    artifact_sha256: str,
) -> None:
    expected = {
        "schema_version": "1.0",
        "namespace": namespace,
        "name": name,
        "version": version,
        "artifact_sha256": artifact_sha256,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise IntegrityError(
                f"Manifest {key} mismatch: expected {value!r}, got {manifest.get(key)!r}"
            )
    if manifest.get("scan_status") not in {"passed", "passed_with_warnings"}:
        raise IntegrityError("Manifest scan_status is not installable")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise IntegrityError("Manifest files must be a non-empty list")
    paths: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise IntegrityError("Manifest file entry must be an object")
        path = _safe_relative_path(str(entry.get("path", ""))).as_posix()
        if path in paths:
            raise IntegrityError(f"Duplicate manifest file: {path}")
        paths.add(path)
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise IntegrityError(f"Invalid manifest SHA-256 for {path}")
        if not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise IntegrityError(f"Invalid manifest size for {path}")
    if "SKILL.md" not in paths:
        raise IntegrityError("Skill artifact must contain SKILL.md at its root")


def verify_extracted_files(root: Path, manifest: dict[str, Any]) -> dict[str, str]:
    expected = {entry["path"]: entry for entry in manifest["files"]}
    actual = {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()}
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise IntegrityError(f"Artifact file set mismatch; missing={missing}, extra={extra}")
    result: dict[str, str] = {}
    for relative, path in actual.items():
        entry = expected[relative]
        if path.is_symlink():
            raise IntegrityError(f"Extracted symlink is not allowed: {relative}")
        size = path.stat().st_size
        if size != entry["size"]:
            raise IntegrityError(f"File size mismatch for {relative}")
        digest = sha256_file(path)
        if digest != entry["sha256"]:
            raise IntegrityError(f"File SHA-256 mismatch for {relative}")
        result[relative] = digest
    return result
