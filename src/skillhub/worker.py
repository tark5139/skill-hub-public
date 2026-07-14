from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import tempfile
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .audit import append_audit
from .config import Settings, get_settings
from .db import create_db_engine, create_session_factory, initialize_database
from .models import (
    ExternalPublication,
    Skill,
    SkillFile,
    SkillVersion,
    UploadSession,
    WorkerHeartbeat,
)
from .publishers.github import (
    GitHubPublisher,
    GitHubPublisherConfig,
    PublicSkillVersion,
    UrllibGitHubReleaseClient,
    VersionPublicationAuthorization,
)
from .security import ScanPolicy, scan_skill_archive
from .storage import ObjectStorage, build_storage

logger = logging.getLogger("skillhub.worker")

_ROOT_LICENSE_NAMES = frozenset(
    {"license", "license.md", "license.txt", "copying", "copying.txt"}
)


def record_heartbeat(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        heartbeat = session.get(WorkerHeartbeat, "default")
        if heartbeat is None:
            heartbeat = WorkerHeartbeat(worker_id="default", last_seen_at=datetime.now(UTC))
            session.add(heartbeat)
        heartbeat.last_seen_at = datetime.now(UTC)
        heartbeat.details_json = {"component": "registry-worker", "version": "0.1.0"}
        session.commit()


def _claim_scan(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> str | None:
    now = now or datetime.now(UTC)
    statement = (
        select(SkillVersion)
        .where(
            or_(
                SkillVersion.status == "pending_scan",
                (SkillVersion.status == "scanning") & (SkillVersion.scan_lease_until < now),
            )
        )
        .order_by(SkillVersion.created_at.asc())
        .limit(1)
    )
    if session.bind and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    version = session.scalar(statement)
    if version is None:
        return None
    if version.scan_attempts >= settings.worker_max_scan_attempts:
        version.status = "scan_failed"
        version.scan_status = "failed"
        version.scan_lease_until = None
        append_audit(
            session,
            actor="system:worker",
            action="version.scan_exhausted",
            target_type="version",
            target_id=version.id,
            request_id=f"worker-{version.id}",
            after={"attempts": version.scan_attempts},
        )
        session.commit()
        return None
    version.status = "scanning"
    version.scan_attempts += 1
    version.scan_lease_until = now + timedelta(seconds=settings.worker_scan_lease_seconds)
    version_id = version.id
    session.commit()
    return version_id


def _signature_sidecar(upload: UploadSession, archive_path: Path) -> Path | None:
    if not upload.signature_key_id and not upload.signature_base64:
        return None
    if not upload.signature_key_id or not upload.signature_base64 or not upload.actual_sha256:
        return None
    sidecar = archive_path.with_name(f"{archive_path.name}.sig.json")
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "algorithm": "ed25519",
                "key_id": upload.signature_key_id,
                "artifact_sha256": upload.actual_sha256,
                "signature": upload.signature_base64,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return sidecar


def _scan_policy(settings: Settings, signature_path: Path | None) -> ScanPolicy:
    return ScanPolicy(
        max_archive_size=settings.max_archive_bytes,
        max_entries=settings.max_files,
        max_file_size=min(settings.max_uncompressed_bytes, 50 * 1024 * 1024),
        max_total_uncompressed_size=settings.max_uncompressed_bytes,
        max_compression_ratio=settings.max_compression_ratio,
        require_signature=settings.require_signature,
        detached_signature_path=signature_path,
        trusted_ed25519_keys=settings.trusted_public_keys_json,
    )


def process_scan(
    session_factory: sessionmaker[Session],
    storage: ObjectStorage,
    settings: Settings,
    version_id: str,
) -> bool:
    with session_factory() as session:
        version = session.scalar(
            select(SkillVersion)
            .options(
                selectinload(SkillVersion.upload),
                selectinload(SkillVersion.skill).selectinload(Skill.namespace),
            )
            .where(SkillVersion.id == version_id)
        )
        if version is None or version.status != "scanning" or version.upload is None:
            return False
        upload = version.upload
        namespace = version.skill.namespace.slug
        name = version.skill.slug
        semver = version.semver
        quarantine_key = upload.quarantine_key
        expected_digest = version.sha256

    try:
        archive_bytes = storage.get_quarantine(quarantine_key)
        with tempfile.TemporaryDirectory(prefix="skillhub-scan-") as temporary:
            archive_path = Path(temporary) / "source.zip"
            archive_path.write_bytes(archive_bytes)
            sidecar_path = _signature_sidecar(upload, archive_path)
            result = scan_skill_archive(
                archive_path,
                namespace=namespace,
                name=name,
                version=semver,
                artifact_sha256=expected_digest,
                policy=_scan_policy(settings, sidecar_path),
            )
    except Exception as exc:
        logger.exception("scan infrastructure failed for %s", version_id)
        with session_factory() as session:
            current = session.get(SkillVersion, version_id)
            if current and current.status == "scanning":
                current.status = "pending_scan"
                current.scan_lease_until = None
                if current.upload:
                    current.upload.error_json = [
                        {
                            "code": "SCAN_INFRASTRUCTURE_FAILURE",
                            "severity": "error",
                            "message": type(exc).__name__,
                        }
                    ]
                session.commit()
        return False

    issues = [
        {
            "code": item.code,
            "severity": item.severity,
            "message": item.message,
            "path": item.path,
            "fingerprint": item.fingerprint,
        }
        for item in result.issues
    ]
    with session_factory() as session:
        version = session.scalar(
            select(SkillVersion)
            .options(
                selectinload(SkillVersion.upload),
                selectinload(SkillVersion.skill).selectinload(Skill.namespace),
            )
            .where(SkillVersion.id == version_id)
        )
        if version is None or version.status != "scanning" or version.upload is None:
            return False
        upload = version.upload
        upload.error_json = issues
        version.scan_lease_until = None
        version.scan_status = result.scan_status
        version.signature_status = (
            "verified"
            if result.signature_key_id
            else "required_missing"
            if settings.require_signature
            else "not_required"
        )
        if not result.passed or result.manifest is None:
            version.status = "scan_failed"
            upload.status = "rejected"
            append_audit(
                session,
                actor="system:worker",
                action="version.scan_failed",
                target_type="version",
                target_id=version.id,
                request_id=f"worker-{version.id}",
                after={"issues": issues, "attempts": version.scan_attempts},
            )
            session.commit()
            return False

        manifest = dict(result.manifest)
        if upload.compatibility_json:
            manifest["compatibility"] = sorted(set(upload.compatibility_json))
        artifact_digest = result.artifact_sha256
        artifact_key = f"artifacts/sha256/{artifact_digest[:2]}/{artifact_digest}/artifact.zip"
        manifest_key = f"manifests/{namespace}/{name}/{semver}.json"
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        storage.put_release(artifact_key, archive_bytes, content_type="application/zip")
        storage.put_release(manifest_key, manifest_bytes, content_type="application/json")

        version.files.clear()
        for item in result.files:
            version.files.append(
                SkillFile(
                    path=item.path,
                    size=item.size,
                    media_type=item.media_type,
                    sha256=item.sha256,
                )
            )
        version.status = "draft"
        version.sha256 = artifact_digest
        version.artifact_key = artifact_key
        version.manifest_key = manifest_key
        version.manifest_sha256 = hashlib.sha256(manifest_bytes.rstrip(b"\n")).hexdigest()
        version.manifest_json = manifest
        upload.status = "scanned"
        append_audit(
            session,
            actor="system:worker",
            action="version.scan_passed",
            target_type="version",
            target_id=version.id,
            request_id=f"worker-{version.id}",
            after={
                "scan_status": result.scan_status,
                "signature_status": version.signature_status,
                "sha256": artifact_digest,
            },
        )
        session.commit()
    storage.delete_quarantine(quarantine_key)
    return True


def process_one_scan(
    session_factory: sessionmaker[Session], storage: ObjectStorage, settings: Settings
) -> bool:
    with session_factory() as session:
        version_id = _claim_scan(session, settings)
    if not version_id:
        return False
    process_scan(session_factory, storage, settings, version_id)
    return True


def _extract_license_text(package: bytes, manifest: dict[str, Any]) -> str:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("validated package manifest has no file inventory")

    candidates: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path = PurePosixPath(item["path"])
        if (
            len(path.parts) == 1
            and path.as_posix() == item["path"]
            and path.name.casefold() in _ROOT_LICENSE_NAMES
        ):
            candidates.append(item)
    if len(candidates) != 1:
        raise ValueError("validated package must contain exactly one root license file")

    candidate = candidates[0]
    license_path = candidate["path"]
    expected_sha256 = candidate.get("sha256")
    expected_size = candidate.get("size")
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise ValueError("validated package license manifest SHA-256 is invalid")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
        raise ValueError("validated package license manifest size is invalid")

    with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024) as buffer:
        buffer.write(package)
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            file_infos = [info for info in archive.infolist() if not info.is_dir()]
            file_names = [info.filename for info in file_infos]
            if "SKILL.md" in file_names:
                archive_license_path = license_path
            else:
                roots = {
                    PurePosixPath(name).parts[0]
                    for name in file_names
                    if PurePosixPath(name).parts
                }
                if len(roots) != 1:
                    raise ValueError(
                        "validated package must use a root or single-wrapper ZIP layout"
                    )
                root = next(iter(roots))
                if f"{root}/SKILL.md" not in file_names:
                    raise ValueError("validated package wrapper has no root SKILL.md")
                archive_license_path = f"{root}/{license_path}"

            matches = [info for info in file_infos if info.filename == archive_license_path]
            if len(matches) != 1:
                raise ValueError(
                    "validated package root license ZIP member is unavailable or duplicate"
                )
            content = archive.read(matches[0])

    if len(content) != expected_size:
        raise ValueError("validated package license size does not match manifest")
    observed_sha256 = hashlib.sha256(content).hexdigest()
    if observed_sha256 != expected_sha256:
        raise ValueError("validated package license SHA-256 does not match manifest")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("validated package license is not strict UTF-8") from exc


def process_one_github_publication(
    session_factory: sessionmaker[Session], storage: ObjectStorage, settings: Settings
) -> bool:
    if not settings.github_token:
        return False
    with session_factory() as session:
        statement = (
            select(ExternalPublication)
            .options(
                selectinload(ExternalPublication.version)
                .selectinload(SkillVersion.skill)
                .selectinload(Skill.namespace),
                selectinload(ExternalPublication.version).selectinload(SkillVersion.upload),
            )
            .where(
                ExternalPublication.provider == "github",
                ExternalPublication.status == "authorized",
            )
            .order_by(ExternalPublication.authorized_at.asc())
            .limit(1)
        )
        if session.bind and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        publication = session.scalar(statement)
        if publication is None:
            return False
        publication.status = "publishing"
        publication_id = publication.id
        session.commit()

    with session_factory() as session:
        publication = session.scalar(
            select(ExternalPublication)
            .options(
                selectinload(ExternalPublication.version)
                .selectinload(SkillVersion.skill)
                .selectinload(Skill.namespace),
                selectinload(ExternalPublication.version).selectinload(SkillVersion.upload),
            )
            .where(ExternalPublication.id == publication_id)
        )
        assert publication is not None
        version = publication.version
        if (
            version.status != "published"
            or not version.immutable
            or not version.artifact_key
            or not version.manifest_json
            or not version.sha256
            or version.skill.visibility != "public"
            or version.signature_status != "verified"
            or publication.artifact_sha256 != version.sha256
            or publication.manifest_sha256 != version.manifest_sha256
            or publication.destination_owner != settings.github_owner
            or publication.destination_repository != settings.github_repository
            or publication.tag_name != f"{version.skill.slug}-v{version.semver}"
            or publication.policy_version != "github-public-v1"
            or not version.upload
            or not version.upload.signature_base64
            or not version.upload.signature_key_id
        ):
            publication.status = "failed_policy"
            publication.evidence_json = {
                **publication.evidence_json,
                "failure": "publication invariants no longer hold",
            }
            session.commit()
            return True
        package = storage.get_release(version.artifact_key)
        signature_document = json.dumps(
            {
                "schema_version": "1.0",
                "algorithm": "ed25519",
                "key_id": version.upload.signature_key_id,
                "artifact_sha256": version.sha256,
                "signature": version.upload.signature_base64,
            },
            separators=(",", ":"),
        ).encode()
        try:
            signature_raw = base64.b64decode(version.upload.signature_base64, validate=True)
            if len(signature_raw) != 64:
                raise ValueError("invalid Ed25519 signature length")
            license_text = _extract_license_text(package, version.manifest_json)
            public_version = PublicSkillVersion(
                skill=version.skill.slug,
                version=version.semver,
                package_bytes=package,
                manifest=version.manifest_json,
                signature=signature_document,
                license_text=license_text,
            )
            authorization = VersionPublicationAuthorization(
                skill=version.skill.slug,
                version=version.semver,
                artifact_sha256=version.sha256,
                approved_by=publication.authorized_by,
                approval_id=publication.id,
                approved_at=publication.authorized_at,
                destination_owner=settings.github_owner,
                destination_repository=settings.github_repository,
            )
            publisher = GitHubPublisher(
                UrllibGitHubReleaseClient(settings.github_token),
                GitHubPublisherConfig(
                    owner=settings.github_owner,
                    repository=settings.github_repository,
                    approver=settings.github_approver,
                ),
            )
            published = publisher.publish(public_version, authorization)
        except Exception as exc:
            logger.exception("GitHub publication failed for %s", publication.id)
            publication.status = "failed_manual_review"
            publication.evidence_json = {
                **publication.evidence_json,
                "failure_type": type(exc).__name__,
                "failure": str(exc)[:1000],
            }
            session.commit()
            return True

        publication.status = "published"
        publication.published_url = published.html_url
        publication.external_release_id = str(published.release_id)
        publication.evidence_json = {
            **publication.evidence_json,
            "tag": published.tag,
            "approval_id": published.approval_id,
            "assets": [item.name for item in published.assets],
            "immutable": published.immutable,
        }
        append_audit(
            session,
            actor="system:worker",
            action="github.release_published",
            target_type="external_publication",
            target_id=publication.id,
            request_id=f"worker-{publication.id}",
            after={"url": published.html_url, "tag": published.tag},
        )
        session.commit()
    return True


def run_worker(*, once: bool = False, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    engine = create_db_engine(settings)
    if settings.env != "production":
        initialize_database(engine)
    session_factory = create_session_factory(engine)
    storage = build_storage(settings)
    processed = 0
    try:
        while True:
            record_heartbeat(session_factory)
            did_work = process_one_scan(session_factory, storage, settings)
            did_work = (
                process_one_github_publication(session_factory, storage, settings) or did_work
            )
            if did_work:
                processed += 1
            if once:
                return processed
            if not did_work:
                time.sleep(settings.worker_poll_seconds)
    finally:
        engine.dispose()


def run() -> None:
    parser = argparse.ArgumentParser(description="Personal Skill Hub background worker")
    parser.add_argument("--once", action="store_true", help="Process at most one item and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(0 if run_worker(once=args.once) >= 0 else 1)


if __name__ == "__main__":
    run()
