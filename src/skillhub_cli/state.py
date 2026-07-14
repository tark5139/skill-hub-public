"""Atomic local installation state and integrity observations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .archive import sha256_file
from .config import _atomic_json_write, local_state_path
from .errors import StateError


@dataclass
class BackupRecord:
    version: str
    path: str
    artifact_sha256: str
    manifest_sha256: str
    files: dict[str, str]
    created_at: str


@dataclass
class InstallationRecord:
    agent: str
    namespace: str
    name: str
    version: str
    destination: str
    artifact_sha256: str
    manifest_sha256: str
    files: dict[str, str]
    installed_at: str
    mode: str = "installed"
    guide_path: str | None = None
    backups: list[BackupRecord] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.agent}:{self.namespace}/{self.name}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstallationRecord:
        backups = [BackupRecord(**item) for item in data.pop("backups", [])]
        return cls(**data, backups=backups)


class StateStore:
    def __init__(self, *, home: Path | str | None = None, path: Path | None = None) -> None:
        self.path = path or local_state_path(home)

    def _load_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "installations": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"Cannot read local state {self.path}: {exc}") from exc
        if payload.get("schema_version") != 1 or not isinstance(payload.get("installations"), dict):
            raise StateError(f"Unsupported or corrupt local state: {self.path}")
        return payload

    def list(self) -> list[InstallationRecord]:
        payload = self._load_payload()
        records: list[InstallationRecord] = []
        for raw in payload["installations"].values():
            try:
                records.append(InstallationRecord.from_dict(dict(raw)))
            except (TypeError, KeyError) as exc:
                raise StateError(f"Invalid installation state entry: {exc}") from exc
        return records

    def get(self, agent: str, namespace: str, name: str) -> InstallationRecord | None:
        key = f"{agent}:{namespace}/{name}"
        payload = self._load_payload()
        raw = payload["installations"].get(key)
        return InstallationRecord.from_dict(dict(raw)) if raw else None

    def find_destination(self, destination: Path) -> InstallationRecord | None:
        resolved = destination.resolve()
        for record in self.list():
            if Path(record.destination).resolve() == resolved:
                return record
        return None

    def put(self, record: InstallationRecord) -> None:
        payload = self._load_payload()
        payload["installations"][record.key] = record.as_dict()
        _atomic_json_write(self.path, payload)

    def delete(self, agent: str, namespace: str, name: str) -> None:
        payload = self._load_payload()
        payload["installations"].pop(f"{agent}:{namespace}/{name}", None)
        _atomic_json_write(self.path, payload)


def observe_record(record: InstallationRecord) -> dict[str, Any]:
    destination = Path(record.destination)
    if not destination.exists():
        return {"status": "missing", "missing": sorted(record.files), "modified": [], "extra": []}
    if destination.is_symlink():
        return {
            "status": "modified",
            "missing": [],
            "modified": ["<destination-symlink>"],
            "extra": [],
        }
    if record.mode == "prepared":
        digest = sha256_file(destination) if destination.is_file() else None
        status = "clean" if digest == record.artifact_sha256 else "modified"
        return {"status": status, "missing": [], "modified": [], "extra": []}

    actual_paths = {
        path.relative_to(destination).as_posix(): path
        for path in destination.rglob("*")
        if path.is_file()
    }
    expected_paths = set(record.files)
    missing = sorted(expected_paths - set(actual_paths))
    extra = sorted(set(actual_paths) - expected_paths)
    modified = sorted(
        relative
        for relative in expected_paths & set(actual_paths)
        if actual_paths[relative].is_symlink()
        or sha256_file(actual_paths[relative]) != record.files[relative]
    )
    status = "clean" if not missing and not extra and not modified else "modified"
    return {"status": status, "missing": missing, "modified": modified, "extra": extra}
