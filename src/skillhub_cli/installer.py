"""Verified, conflict-aware and atomic Skill installation lifecycle."""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skillhub.adapters import AdapterError, WorkBuddyAdapter, get_adapter

from .archive import (
    canonical_json_sha256,
    safe_extract_zip,
    sha256_file,
    validate_manifest,
    verify_extracted_files,
)
from .config import state_dir
from .errors import InstallConflict, IntegrityError, SkillHubError, StateError
from .registry import RegistryClient
from .state import BackupRecord, InstallationRecord, StateStore, observe_record


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class SkillInstaller:
    def __init__(
        self,
        registry: RegistryClient,
        *,
        home: Path | str | None = None,
        state: StateStore | None = None,
        require_verified_signature: bool = True,
    ) -> None:
        from skillhub.adapters import resolve_home

        self.registry = registry
        self.home = resolve_home(home)
        self.state = state or StateStore(home=self.home)
        self.require_verified_signature = require_verified_signature

    def _verified_release(
        self,
        namespace: str,
        name: str,
        *,
        version: str | None,
        label: str | None,
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        Path,
        dict[str, str],
        tempfile.TemporaryDirectory[str],
    ]:
        resolved = self.registry.resolve(namespace, name, version=version, label=label)
        if self.require_verified_signature and resolved.get("signature_status") != "verified":
            raise IntegrityError("Registry has not attested this release signature as verified")

        work_root = state_dir(self.home) / "tmp"
        work_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="release-", dir=work_root)
        temp = Path(temporary.name)
        try:
            archive_path = self.registry.download(resolved["artifact_url"], temp / "artifact.zip")
            observed_artifact = sha256_file(archive_path)
            if observed_artifact != resolved["artifact_sha256"]:
                raise IntegrityError(
                    "Artifact SHA-256 mismatch: "
                    f"expected {resolved['artifact_sha256']}, observed {observed_artifact}"
                )

            manifest = self.registry.manifest(resolved["manifest_url"])
            observed_manifest = canonical_json_sha256(manifest)
            if not secrets.compare_digest(observed_manifest, resolved["manifest_sha256"]):
                raise IntegrityError(
                    "Manifest SHA-256 mismatch: "
                    f"expected {resolved['manifest_sha256']}, observed {observed_manifest}"
                )
            validate_manifest(
                manifest,
                namespace=namespace,
                name=name,
                version=resolved["resolved_version"],
                artifact_sha256=observed_artifact,
            )
            extracted = safe_extract_zip(archive_path, temp / "extracted")
            files = verify_extracted_files(extracted, manifest)
        except Exception:
            temporary.cleanup()
            raise
        return resolved, manifest, extracted, files, temporary

    def install(
        self,
        namespace: str,
        name: str,
        *,
        agent: str,
        version: str | None = None,
        label: str | None = None,
        force: bool = False,
        root_override: Path | None = None,
    ) -> dict[str, Any]:
        adapter = get_adapter(agent, home=self.home, root_override=root_override)
        resolved, manifest, extracted, files, temporary = self._verified_release(
            namespace, name, version=version, label=label
        )
        try:
            if isinstance(adapter, WorkBuddyAdapter):
                return self._prepare_workbuddy(
                    adapter, namespace, name, resolved, manifest, extracted
                )
            return self._activate_local(
                adapter.adapter_id,
                adapter,
                namespace,
                name,
                resolved,
                manifest,
                extracted,
                files,
                force=force,
            )
        finally:
            temporary.cleanup()

    def _prepare_workbuddy(
        self,
        adapter: WorkBuddyAdapter,
        namespace: str,
        name: str,
        resolved: dict[str, Any],
        manifest: dict[str, Any],
        extracted: Path,
    ) -> dict[str, Any]:
        output_dir = state_dir(self.home) / "workbuddy-imports"
        result = adapter.prepare_import(
            extracted,
            namespace=namespace,
            name=name,
            version=resolved["resolved_version"],
            output_dir=output_dir,
        )
        package_digest = sha256_file(result.package_path)
        record = InstallationRecord(
            agent=adapter.adapter_id,
            namespace=namespace,
            name=name,
            version=resolved["resolved_version"],
            destination=str(result.package_path),
            artifact_sha256=package_digest,
            manifest_sha256=canonical_json_sha256(manifest),
            files={},
            installed_at=utc_now(),
            mode="prepared",
            guide_path=str(result.guide_path),
        )
        try:
            self.state.put(record)
        except Exception:
            result.package_path.unlink(missing_ok=True)
            result.guide_path.unlink(missing_ok=True)
            raise
        return {
            "action": "prepared",
            "support_level": "preview",
            "agent": adapter.adapter_id,
            "namespace": namespace,
            "name": name,
            "version": record.version,
            **result.as_dict(),
            "automatic_install": False,
        }

    def _activate_local(
        self,
        agent: str,
        adapter: Any,
        namespace: str,
        name: str,
        resolved: dict[str, Any],
        manifest: dict[str, Any],
        extracted: Path,
        files: dict[str, str],
        *,
        force: bool,
    ) -> dict[str, Any]:
        destination = adapter.destination(name)
        current = self.state.get(agent, namespace, name)
        owner = self.state.find_destination(destination)
        current_observation: dict[str, Any] | None = None
        if owner and owner.key != f"{agent}:{namespace}/{name}":
            raise InstallConflict(
                f"Destination {destination} is already managed by {owner.namespace}/{owner.name}"
            )

        if current:
            if Path(current.destination).resolve() != destination.resolve():
                raise InstallConflict(
                    "This installation is already managed at "
                    f"{current.destination}; changing its root requires uninstall/reinstall"
                )
            current_observation = observe_record(current)
            if current_observation["status"] != "clean" and not force:
                raise InstallConflict(
                    f"Local changes detected at {destination}; use --force only after review"
                )
            if (
                current.version == resolved["resolved_version"]
                and current.artifact_sha256 == resolved["artifact_sha256"]
                and current_observation["status"] == "clean"
            ):
                return {
                    "action": "unchanged",
                    "agent": agent,
                    "namespace": namespace,
                    "name": name,
                    "version": current.version,
                    "destination": str(destination),
                }
        elif destination.exists() and not force:
            raise InstallConflict(
                f"Unmanaged destination already exists: {destination}; import it or use --force"
            )

        root = adapter.root
        if root is None:
            raise AdapterError(f"Adapter {agent} has no local root")
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{name}.skillhub-", dir=root))
        shutil.rmtree(staging)
        shutil.copytree(extracted, staging, copy_function=shutil.copy2)

        backup_path: Path | None = None
        previous_backup: BackupRecord | None = None
        if destination.exists():
            previous_version = current.version if current else "unmanaged"
            backup_path = self._new_backup_path(adapter, name, previous_version)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, backup_path)
            if current and current_observation and current_observation["status"] == "clean":
                previous_backup = BackupRecord(
                    version=current.version,
                    path=str(backup_path),
                    artifact_sha256=current.artifact_sha256,
                    manifest_sha256=current.manifest_sha256,
                    files=current.files,
                    created_at=utc_now(),
                )
        try:
            os.replace(staging, destination)
        except Exception:
            if backup_path and backup_path.exists() and not destination.exists():
                os.replace(backup_path, destination)
            shutil.rmtree(staging, ignore_errors=True)
            raise

        backups = list(current.backups) if current else []
        if previous_backup:
            backups.append(previous_backup)
        record = InstallationRecord(
            agent=agent,
            namespace=namespace,
            name=name,
            version=resolved["resolved_version"],
            destination=str(destination),
            artifact_sha256=resolved["artifact_sha256"],
            manifest_sha256=canonical_json_sha256(manifest),
            files=files,
            installed_at=utc_now(),
            backups=backups[-5:],
        )
        try:
            self.state.put(record)
        except Exception:
            # Keep the installation tree and state file on the same side of the
            # transaction if an atomic state write fails.
            shutil.rmtree(destination, ignore_errors=True)
            if backup_path and backup_path.exists():
                os.replace(backup_path, destination)
            raise
        return {
            "action": "updated" if current else "installed",
            "agent": agent,
            "namespace": namespace,
            "name": name,
            "version": record.version,
            "destination": str(destination),
            "backup": str(backup_path) if backup_path else None,
        }

    @staticmethod
    def _new_backup_path(adapter: Any, name: str, version: str) -> Path:
        suffix = uuid.uuid4().hex[:12]
        return adapter.backup_root(name) / f"{version}-{suffix}"

    def update(
        self,
        namespace: str,
        name: str,
        *,
        agent: str,
        version: str | None = None,
        label: str | None = "stable",
        force: bool = False,
        root_override: Path | None = None,
    ) -> dict[str, Any]:
        canonical_agent = get_adapter(agent, home=self.home).adapter_id
        current = self.state.get(canonical_agent, namespace, name)
        if current is None:
            raise StateError(f"{namespace}/{name} is not installed for {canonical_agent}")
        if current.mode == "prepared":
            label = None if version else label
        else:
            recorded_root = Path(current.destination).parent.resolve()
            if root_override and root_override.resolve() != recorded_root:
                raise StateError(
                    f"--root must match the recorded installation root: {recorded_root}"
                )
            root_override = recorded_root
        return self.install(
            namespace,
            name,
            agent=canonical_agent,
            version=version,
            label=None if version else label,
            force=force,
            root_override=root_override,
        )

    def status(
        self,
        *,
        agent: str | None = None,
        namespace: str | None = None,
        name: str | None = None,
    ) -> list[dict[str, Any]]:
        canonical_agent = get_adapter(agent, home=self.home).adapter_id if agent else None
        records = self.state.list()
        result: list[dict[str, Any]] = []
        for record in records:
            if canonical_agent and record.agent != canonical_agent:
                continue
            if namespace and record.namespace != namespace:
                continue
            if name and record.name != name:
                continue
            result.append({**record.as_dict(), "observation": observe_record(record)})
        return result

    def rollback(
        self,
        namespace: str,
        name: str,
        *,
        agent: str,
        version: str | None = None,
        force: bool = False,
        root_override: Path | None = None,
    ) -> dict[str, Any]:
        canonical_agent = get_adapter(agent, home=self.home).adapter_id
        record = self.state.get(canonical_agent, namespace, name)
        if record is None:
            raise StateError(f"{namespace}/{name} is not installed for {canonical_agent}")
        if record.mode != "installed":
            raise StateError("Guided WorkBuddy imports cannot be rolled back automatically")
        observation = observe_record(record)
        if observation["status"] != "clean" and not force:
            raise InstallConflict("Local changes detected; rollback refused without --force")

        candidates = [backup for backup in record.backups if version in {None, backup.version}]
        if not candidates:
            selector = version or "previous"
            raise StateError(f"No {selector} backup is available")
        selected = candidates[-1]
        selected_path = Path(selected.path)
        if not selected_path.is_dir():
            raise StateError(f"Rollback backup is missing: {selected_path}")

        destination = Path(record.destination)
        recorded_root = destination.parent.resolve()
        if root_override and root_override.resolve() != recorded_root:
            raise StateError(f"--root must match the recorded installation root: {recorded_root}")
        adapter = get_adapter(canonical_agent, home=self.home, root_override=recorded_root)
        current_backup_path = self._new_backup_path(adapter, name, record.version)
        current_backup_path.parent.mkdir(parents=True, exist_ok=True)
        had_destination = destination.exists()
        if had_destination:
            os.replace(destination, current_backup_path)
        try:
            os.replace(selected_path, destination)
        except Exception:
            if current_backup_path.exists() and not destination.exists():
                os.replace(current_backup_path, destination)
            raise

        remaining = [backup for backup in record.backups if backup is not selected]
        if had_destination:
            remaining.append(
                BackupRecord(
                    version=record.version,
                    path=str(current_backup_path),
                    artifact_sha256=record.artifact_sha256,
                    manifest_sha256=record.manifest_sha256,
                    files=record.files,
                    created_at=utc_now(),
                )
            )
        rolled_back = InstallationRecord(
            agent=record.agent,
            namespace=record.namespace,
            name=record.name,
            version=selected.version,
            destination=record.destination,
            artifact_sha256=selected.artifact_sha256,
            manifest_sha256=selected.manifest_sha256,
            files=selected.files,
            installed_at=utc_now(),
            backups=remaining[-5:],
        )
        try:
            self.state.put(rolled_back)
        except Exception:
            # Restore both trees to their original locations so the unchanged
            # state file remains truthful.
            os.replace(destination, selected_path)
            if current_backup_path.exists():
                os.replace(current_backup_path, destination)
            raise
        return {
            "action": "rolled_back",
            "agent": canonical_agent,
            "namespace": namespace,
            "name": name,
            "version": selected.version,
            "destination": str(destination),
        }

    def uninstall(
        self,
        namespace: str,
        name: str,
        *,
        agent: str,
        force: bool = False,
        root_override: Path | None = None,
    ) -> dict[str, Any]:
        canonical_agent = get_adapter(agent, home=self.home).adapter_id
        record = self.state.get(canonical_agent, namespace, name)
        if record is None:
            raise StateError(f"{namespace}/{name} is not installed for {canonical_agent}")
        observation = observe_record(record)
        if observation["status"] != "clean" and not force:
            raise InstallConflict("Local changes detected; uninstall refused without --force")

        destination = Path(record.destination)
        removed_to: Path | None = None
        prepared_moves: list[tuple[Path, Path]] = []
        if record.mode == "prepared":
            trash = state_dir(self.home) / "tmp" / f"uninstall-{uuid.uuid4().hex}"
            for original in (destination, Path(record.guide_path) if record.guide_path else None):
                if original and original.exists():
                    trash.mkdir(parents=True, exist_ok=True)
                    temporary_path = trash / f"{len(prepared_moves)}-{original.name}"
                    os.replace(original, temporary_path)
                    prepared_moves.append((original, temporary_path))
        elif destination.exists():
            recorded_root = destination.parent.resolve()
            if root_override and root_override.resolve() != recorded_root:
                raise StateError(
                    f"--root must match the recorded installation root: {recorded_root}"
                )
            adapter = get_adapter(canonical_agent, home=self.home, root_override=recorded_root)
            removed_to = self._new_backup_path(adapter, name, f"removed-{record.version}")
            removed_to.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, removed_to)
        try:
            self.state.delete(canonical_agent, namespace, name)
        except Exception:
            if removed_to and removed_to.exists() and not destination.exists():
                os.replace(removed_to, destination)
            for original, temporary_path in reversed(prepared_moves):
                if temporary_path.exists():
                    os.replace(temporary_path, original)
            raise
        if prepared_moves:
            shutil.rmtree(prepared_moves[0][1].parent, ignore_errors=True)
        return {
            "action": "uninstalled",
            "agent": canonical_agent,
            "namespace": namespace,
            "name": name,
            "removed_to": str(removed_to) if removed_to else None,
        }

    def doctor(self) -> dict[str, Any]:
        adapters: list[dict[str, Any]] = []
        for adapter_id in ("codex", "claude-code", "trae-cn", "openclaw", "hermes", "workbuddy"):
            adapters.append(get_adapter(adapter_id, home=self.home).doctor().as_dict())
        state_ok = True
        state_error = None
        try:
            self.state.list()
        except SkillHubError as exc:
            state_ok = False
            state_error = str(exc)
        return {
            "home": str(self.home),
            "state_ok": state_ok,
            "state_error": state_error,
            "adapters": adapters,
        }
