from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from skillhub_cli.archive import canonical_json_sha256, safe_extract_zip
from skillhub_cli.errors import InstallConflict, IntegrityError, StateError
from skillhub_cli.installer import SkillInstaller
from skillhub_cli.state import StateStore


@dataclass
class Release:
    version: str
    archive: bytes
    manifest: dict[str, Any]
    manifest_sha256: str


def make_release(version: str, body: str) -> Release:
    files = {
        "SKILL.md": (
            f"---\nname: demo\ndescription: demo version {version}\n---\n\n{body}\n".encode()
        ),
        "references/info.md": f"reference {version}\n".encode(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    artifact = buffer.getvalue()
    artifact_sha = hashlib.sha256(artifact).hexdigest()
    manifest = {
        "schema_version": "1.0",
        "namespace": "personal",
        "name": "demo",
        "version": version,
        "description": "demo",
        "compatibility": ["codex"],
        "files": [
            {
                "path": path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "media_type": "text/markdown",
            }
            for path, content in files.items()
        ],
        "artifact_sha256": artifact_sha,
        "license": "Apache-2.0",
        "signatures": [{"kind": "test"}],
        "scan_status": "passed",
        "created_at": "2026-07-14T00:00:00Z",
    }
    return Release(
        version=version,
        archive=artifact,
        manifest=manifest,
        manifest_sha256=canonical_json_sha256(manifest),
    )


class FakeRegistry:
    def __init__(self, releases: list[Release]) -> None:
        self.releases = {release.version: release for release in releases}
        self.stable = releases[-1].version

    def resolve(
        self,
        namespace: str,
        name: str,
        *,
        version: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        selected = self.releases[version or self.stable]
        return {
            "namespace": namespace,
            "name": name,
            "resolved_version": selected.version,
            "artifact_sha256": selected.manifest["artifact_sha256"],
            "manifest_sha256": selected.manifest_sha256,
            "artifact_url": f"artifact:{selected.version}",
            "manifest_url": f"manifest:{selected.version}",
            "signature_status": "verified",
        }

    def download(self, url: str, destination: Path) -> Path:
        version = url.split(":", 1)[1]
        destination.write_bytes(self.releases[version].archive)
        return destination

    def manifest(self, url: str) -> dict[str, Any]:
        version = url.split(":", 1)[1]
        return dict(self.releases[version].manifest)


def test_atomic_install_update_rollback_status_and_uninstall(tmp_path: Path) -> None:
    v1 = make_release("1.0.0", "first")
    v2 = make_release("2.0.0", "second")
    registry = FakeRegistry([v1, v2])
    registry.stable = "1.0.0"
    installer = SkillInstaller(registry, home=tmp_path)  # type: ignore[arg-type]

    installed = installer.install("personal", "demo", agent="codex")
    destination = tmp_path / ".codex/skills/demo"
    assert installed["action"] == "installed"
    assert destination.is_dir()
    assert installer.status()[0]["observation"]["status"] == "clean"
    assert installer.status(agent="claude") == []
    assert installer.status(agent="codex")[0]["agent"] == "codex"

    registry.stable = "2.0.0"
    updated = installer.update("personal", "demo", agent="codex")
    assert updated["action"] == "updated"
    assert "second" in (destination / "SKILL.md").read_text()
    record = StateStore(home=tmp_path).get("codex", "personal", "demo")
    assert record is not None and [backup.version for backup in record.backups] == ["1.0.0"]

    rolled_back = installer.rollback("personal", "demo", agent="codex")
    assert rolled_back["version"] == "1.0.0"
    assert "first" in (destination / "SKILL.md").read_text()
    assert installer.status()[0]["observation"]["status"] == "clean"

    removed = installer.uninstall("personal", "demo", agent="codex")
    assert removed["action"] == "uninstalled"
    assert not destination.exists()
    assert StateStore(home=tmp_path).list() == []
    assert Path(removed["removed_to"]).is_dir()


def test_update_refuses_local_changes_and_force_preserves_them_offline(tmp_path: Path) -> None:
    v1 = make_release("1.0.0", "first")
    v2 = make_release("2.0.0", "second")
    registry = FakeRegistry([v1, v2])
    registry.stable = "1.0.0"
    installer = SkillInstaller(registry, home=tmp_path)  # type: ignore[arg-type]
    installer.install("personal", "demo", agent="codex")
    destination = tmp_path / ".codex/skills/demo"
    (destination / "SKILL.md").write_text("local edit", encoding="utf-8")
    registry.stable = "2.0.0"

    with pytest.raises(InstallConflict, match="Local changes"):
        installer.update("personal", "demo", agent="codex")
    result = installer.update("personal", "demo", agent="codex", force=True)
    assert result["action"] == "updated"
    assert result["backup"] and Path(result["backup"]).is_dir()
    assert (Path(result["backup"]) / "SKILL.md").read_text() == "local edit"


def test_unmanaged_destination_is_not_overwritten_without_force(tmp_path: Path) -> None:
    registry = FakeRegistry([make_release("1.0.0", "first")])
    destination = tmp_path / ".codex/skills/demo"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("unmanaged", encoding="utf-8")
    installer = SkillInstaller(registry, home=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(InstallConflict, match="Unmanaged"):
        installer.install("personal", "demo", agent="codex")


def test_workbuddy_install_prepares_package_but_does_not_claim_install(tmp_path: Path) -> None:
    registry = FakeRegistry([make_release("1.0.0", "first")])
    installer = SkillInstaller(registry, home=tmp_path)  # type: ignore[arg-type]
    result = installer.install("personal", "demo", agent="workbuddy")
    assert result["action"] == "prepared"
    assert result["automatic_install"] is False
    assert Path(result["package_path"]).is_file()
    record = StateStore(home=tmp_path).get("workbuddy", "personal", "demo")
    assert record is not None and record.mode == "prepared"
    with pytest.raises(StateError, match="cannot be rolled back"):
        installer.rollback("personal", "demo", agent="workbuddy")


def test_artifact_sha_and_zip_slip_are_rejected(tmp_path: Path) -> None:
    release = make_release("1.0.0", "first")
    release.manifest["artifact_sha256"] = "0" * 64
    installer = SkillInstaller(FakeRegistry([release]), home=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError, match="SHA-256 mismatch"):
        installer.install("personal", "demo", agent="codex")

    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("../escape", "bad")
    with pytest.raises(IntegrityError, match="Unsafe"):
        safe_extract_zip(malicious, tmp_path / "extract")


def test_manifest_digest_is_checked_before_validation_or_extraction(
    monkeypatch: Any, tmp_path: Path
) -> None:
    release = make_release("1.0.0", "first")
    release.manifest["description"] = "tampered after digest was frozen"

    def must_not_validate(*_: Any, **__: Any) -> None:
        raise AssertionError("manifest validation ran before digest verification")

    monkeypatch.setattr("skillhub_cli.installer.validate_manifest", must_not_validate)
    installer = SkillInstaller(FakeRegistry([release]), home=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(IntegrityError, match="Manifest SHA-256 mismatch"):
        installer.install("personal", "demo", agent="codex")
    assert not (tmp_path / ".codex/skills/demo").exists()


def test_manifest_canonical_digest_excludes_storage_newline() -> None:
    manifest = {"z": "中文", "a": {"value": 1}}
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert canonical_json_sha256(manifest) == hashlib.sha256(canonical).hexdigest()
    assert canonical_json_sha256(manifest) != hashlib.sha256(canonical + b"\n").hexdigest()
