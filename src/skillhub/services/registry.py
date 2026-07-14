from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Any

from packaging.version import InvalidVersion, Version
from sqlalchemy import Text, and_, cast, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..auth import Principal
from ..errors import HubError
from ..models import (
    InstallationReport,
    Label,
    Namespace,
    RegistryChange,
    Review,
    Skill,
    SkillVersion,
    UploadSession,
)
from ..schemas import (
    InstallationReportIn,
    LabelSummary,
    SearchPage,
    SkillCreate,
    SkillDetail,
    SkillSummary,
    SyncManifestItem,
    SyncManifestResponse,
    UploadCreate,
    VersionOut,
    VersionSummary,
)
from .cursors import decode_cursor, encode_cursor


def _can_read(skill: Skill, principal: Principal) -> bool:
    return principal.is_admin or skill.visibility == "public" or skill.owner_id == principal.subject


def require_skill_read(skill: Skill | None, principal: Principal) -> Skill:
    if skill is None or not _can_read(skill, principal):
        # Deliberately collapse forbidden and missing to prevent private-name enumeration.
        raise HubError(404, "skill_not_found", "The Skill does not exist")
    return skill


def require_skill_owner(skill: Skill | None, principal: Principal) -> Skill:
    if skill is None:
        raise HubError(404, "skill_not_found", "The Skill does not exist")
    if not (principal.is_admin or skill.owner_id == principal.subject):
        raise HubError(404, "skill_not_found", "The Skill does not exist")
    return skill


def load_skill_by_id(session: Session, skill_id: str) -> Skill | None:
    return session.scalar(
        select(Skill)
        .options(
            selectinload(Skill.namespace),
            selectinload(Skill.versions),
            selectinload(Skill.labels).selectinload(Label.version),
        )
        .where(Skill.id == skill_id)
    )


def load_skill_by_slug(session: Session, namespace: str, name: str) -> Skill | None:
    return session.scalar(
        select(Skill)
        .join(Namespace)
        .options(
            selectinload(Skill.namespace),
            selectinload(Skill.versions),
            selectinload(Skill.labels).selectinload(Label.version),
        )
        .where(Namespace.slug == namespace, Skill.slug == name)
    )


def create_skill(session: Session, payload: SkillCreate, principal: Principal) -> Skill:
    namespace = session.scalar(select(Namespace).where(Namespace.slug == payload.namespace))
    if namespace is None:
        namespace = Namespace(slug=payload.namespace, owner_org=principal.subject)
        session.add(namespace)
        session.flush()
    skill = Skill(
        namespace_id=namespace.id,
        slug=payload.name,
        description=payload.description,
        visibility=payload.visibility,
        owner_id=principal.subject,
        tags_json=payload.tags,
    )
    session.add(skill)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HubError(
            409, "skill_already_exists", "A Skill with this name already exists"
        ) from exc
    return load_skill_by_id(session, skill.id) or skill


def _labels_map(skill: Skill) -> dict[str, str]:
    return {label.label: label.version.semver for label in skill.labels}


def _compatibility_index(values: list[str]) -> str:
    normalized = sorted({value.strip().lower() for value in values if value.strip()})
    return "" if not normalized else f"|{'|'.join(normalized)}|"


def skill_summary(skill: Skill) -> SkillSummary:
    return SkillSummary(
        id=skill.id,
        namespace=skill.namespace.slug,
        name=skill.slug,
        description=skill.description,
        visibility=skill.visibility,
        owner_id=skill.owner_id,
        tags=skill.tags_json or [],
        labels=_labels_map(skill),
        updated_at=skill.updated_at,
    )


def skill_detail(skill: Skill, principal: Principal) -> SkillDetail:
    summary = skill_summary(skill).model_dump()
    visible_versions = (
        skill.versions
        if principal.is_admin or skill.owner_id == principal.subject
        else [item for item in skill.versions if item.status in {"published", "deprecated"}]
    )
    versions = sorted(
        visible_versions,
        key=lambda item: Version(item.semver),
        reverse=True,
    )
    return SkillDetail(
        **summary,
        versions=[
            VersionSummary(
                id=item.id,
                version=item.semver,
                status=item.status,
                sha256=item.sha256,
                scan_status=item.scan_status,
                signature_status=item.signature_status,
                created_at=item.created_at,
                published_at=item.published_at,
            )
            for item in versions
        ],
        label_details=[
            LabelSummary(label=item.label, version=item.version.semver, etag=item.etag)
            for item in sorted(skill.labels, key=lambda row: row.label)
        ],
    )


def search_skills(
    session: Session,
    *,
    principal: Principal,
    query: str | None,
    tags: list[str],
    compatibility: str | None,
    owner: str | None,
    cursor: str | None,
    limit: int,
) -> SearchPage:
    statement = (
        select(Skill)
        .join(Namespace)
        .options(
            selectinload(Skill.namespace),
            selectinload(Skill.labels).selectinload(Label.version),
        )
        .order_by(Skill.updated_at.desc(), Skill.id.desc())
        .limit(limit + 1)
    )
    if not principal.is_admin:
        statement = statement.where(
            or_(Skill.visibility == "public", Skill.owner_id == principal.subject)
        )
    if query:
        pattern = f"%{query.strip()}%"
        statement = statement.where(
            or_(
                Skill.slug.ilike(pattern),
                Skill.description.ilike(pattern),
                Namespace.slug.ilike(pattern),
            )
        )
    if owner:
        statement = statement.where(Skill.owner_id == owner)
    if compatibility:
        compatibility_pattern = f'%"{compatibility.strip().lower()}"%'
        statement = statement.where(
            Skill.versions.any(
                and_(
                    SkillVersion.status == "published",
                    cast(SkillVersion.manifest_json, Text).ilike(compatibility_pattern),
                )
            )
        )
    for tag in sorted({tag.lower() for tag in tags}):
        # JSON containment varies between SQLite and PostgreSQL. This predicate is portable for the
        # MVP and remains authorization-filtered; production can replace it with a normalized table.
        statement = statement.where(Skill.tags_json.cast(Text).ilike(f'%"{tag}"%'))
    if cursor:
        updated_at, row_id = decode_cursor(cursor)
        statement = statement.where(
            or_(
                Skill.updated_at < updated_at,
                and_(Skill.updated_at == updated_at, Skill.id < row_id),
            )
        )
    rows = list(session.scalars(statement).unique())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].updated_at, rows[-1].id) if has_more and rows else None
    return SearchPage(items=[skill_summary(item) for item in rows], next_cursor=next_cursor)


def create_upload(
    session: Session,
    *,
    skill: Skill,
    payload: UploadCreate,
) -> UploadSession:
    try:
        Version(payload.version)
    except InvalidVersion as exc:
        raise HubError(
            422, "invalid_version", "The version is not valid semantic versioning"
        ) from exc
    existing = session.scalar(
        select(SkillVersion).where(
            SkillVersion.skill_id == skill.id, SkillVersion.semver == payload.version
        )
    )
    if existing:
        raise HubError(409, "version_already_exists", "This Skill version already exists")
    active = session.scalar(
        select(UploadSession).where(
            UploadSession.skill_id == skill.id,
            UploadSession.version == payload.version,
        )
    )
    if active:
        raise HubError(409, "upload_already_exists", "An upload already exists for this version")
    upload = UploadSession(
        skill_id=skill.id,
        version=payload.version,
        quarantine_key=f"{skill.namespace.slug}/{secrets.token_hex(16)}/source.zip",
        expected_sha256=payload.expected_sha256,
        declared_license=payload.license,
        signature_key_id=payload.signature_key_id,
        signature_base64=payload.signature_base64,
        compatibility_json=payload.compatibility,
    )
    session.add(upload)
    session.flush()
    return upload


def attach_upload_content(
    session: Session,
    *,
    upload: UploadSession,
    content: bytes,
    max_bytes: int,
    storage: Any,
) -> UploadSession:
    if upload.status not in {"created", "uploaded"}:
        raise HubError(409, "upload_not_writable", "The upload session is no longer writable")
    if len(content) == 0:
        raise HubError(422, "empty_archive", "The uploaded archive is empty")
    if len(content) > max_bytes:
        raise HubError(413, "archive_too_large", f"Archive exceeds {max_bytes} bytes")
    digest = hashlib.sha256(content).hexdigest()
    if upload.status == "uploaded" and upload.actual_sha256 != digest:
        raise HubError(409, "upload_already_stored", "Upload content is immutable once stored")
    if upload.expected_sha256 and upload.expected_sha256 != digest:
        raise HubError(422, "digest_mismatch", "Archive SHA-256 does not match expected_sha256")
    storage.put_quarantine(upload.quarantine_key, content)
    upload.actual_sha256 = digest
    upload.size_bytes = len(content)
    upload.status = "uploaded"
    session.flush()
    return upload


def finalize_upload(session: Session, upload: UploadSession, storage: Any) -> SkillVersion:
    if upload.status == "finalized" and upload.version_record:
        return upload.version_record
    if upload.status != "uploaded" or not upload.actual_sha256:
        raise HubError(409, "upload_not_ready", "Upload content must be stored before finalization")
    try:
        stored = storage.get_quarantine(upload.quarantine_key)
    except (FileNotFoundError, KeyError) as exc:
        raise HubError(409, "upload_object_missing", "Quarantine object is unavailable") from exc
    stored_digest = hashlib.sha256(stored).hexdigest()
    if stored_digest != upload.actual_sha256 or len(stored) != upload.size_bytes:
        raise HubError(
            409,
            "upload_object_changed",
            "Quarantine object no longer matches the recorded digest and size",
        )
    version = SkillVersion(
        skill_id=upload.skill_id,
        upload_id=upload.id,
        semver=upload.version,
        status="pending_scan",
        sha256=upload.actual_sha256,
        scan_status="pending",
        signature_status="pending",
    )
    session.add(version)
    upload.status = "finalized"
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HubError(409, "version_already_exists", "This Skill version already exists") from exc
    return version


def version_out(version: SkillVersion) -> VersionOut:
    return VersionOut(
        id=version.id,
        skill_id=version.skill_id,
        version=version.semver,
        status=version.status,
        sha256=version.sha256,
        scan_status=version.scan_status,
        signature_status=version.signature_status,
        manifest=version.manifest_json,
    )


def require_version(session: Session, version_id: str) -> SkillVersion:
    version = session.scalar(
        select(SkillVersion)
        .options(selectinload(SkillVersion.skill).selectinload(Skill.namespace))
        .where(SkillVersion.id == version_id)
    )
    if version is None:
        raise HubError(404, "version_not_found", "The Skill version does not exist")
    return version


def submit_version(version: SkillVersion) -> None:
    if version.status != "draft":
        raise HubError(409, "invalid_lifecycle_transition", "Only a scanned draft can be submitted")
    if version.scan_status not in {"passed", "passed_with_warnings"}:
        raise HubError(409, "scan_not_passed", "The version has not passed security scanning")
    if not version.sha256 or not version.manifest_json or not version.artifact_key:
        raise HubError(409, "artifact_not_ready", "The validated artifact is unavailable")
    version.status = "submitted"
    version.manifest_sha256 = _manifest_digest(version.manifest_json)
    # Freeze the reviewed payload at submission, not at publication. Lifecycle state may continue
    # through the explicitly allowed review/publish transitions, but package fields cannot change.
    version.immutable = True


def _manifest_digest(manifest: dict[str, Any]) -> str:
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def review_version(
    session: Session,
    *,
    version: SkillVersion,
    reviewer: str,
    decision: str,
    evidence: dict[str, Any],
) -> Review:
    if version.status != "submitted":
        raise HubError(
            409, "invalid_lifecycle_transition", "Only a submitted version can be reviewed"
        )
    review = Review(
        version_id=version.id,
        reviewer=reviewer,
        decision=decision,
        artifact_sha256=version.sha256 or "",
        manifest_sha256=_manifest_digest(version.manifest_json or {}),
        signature_status=version.signature_status,
        evidence_json=evidence,
    )
    session.add(review)
    version.status = "approved" if decision == "approved" else "rejected"
    return review


def move_label(
    session: Session,
    *,
    skill: Skill,
    label_name: str,
    target: SkillVersion,
    if_match: str | None,
) -> Label:
    if target.skill_id != skill.id or target.status != "published":
        raise HubError(409, "invalid_label_target", "Labels can target only a published version")
    if session.bind and session.bind.dialect.name == "postgresql":
        session.execute(select(Skill.id).where(Skill.id == skill.id).with_for_update())
    label_statement = select(Label).where(Label.skill_id == skill.id, Label.label == label_name)
    if session.bind and session.bind.dialect.name == "postgresql":
        label_statement = label_statement.with_for_update()
    label = session.scalar(label_statement)
    new_etag = f'"label:{secrets.token_hex(16)}"'
    if label is None:
        if if_match not in {None, "*"}:
            raise HubError(412, "etag_mismatch", "The label does not exist")
        label = Label(skill_id=skill.id, label=label_name, version_id=target.id, etag=new_etag)
        session.add(label)
    else:
        if if_match is None:
            raise HubError(428, "precondition_required", "If-Match is required to move a label")
        if if_match != label.etag:
            raise HubError(412, "etag_mismatch", "The label changed since it was read")
        label.version_id = target.id
        label.etag = new_etag
    session.flush()
    session.add(
        RegistryChange(
            skill_id=skill.id,
            version_id=target.id,
            event_type="label.moved",
            compatibility_index=_compatibility_index(
                list((target.manifest_json or {}).get("compatibility", []))
            ),
            payload_json={
                "label": label_name,
                "version": target.semver,
                "sha256": target.sha256,
                "compatibility": list((target.manifest_json or {}).get("compatibility", [])),
            },
        )
    )
    return label


def publish_version(
    session: Session,
    *,
    version: SkillVersion,
    label_name: str | None,
    require_verified_signature: bool,
) -> Label | None:
    if version.status != "approved":
        raise HubError(
            409, "invalid_lifecycle_transition", "Only an approved version can be published"
        )
    if not version.artifact_key or not version.manifest_json or not version.sha256:
        raise HubError(409, "artifact_not_ready", "The validated artifact is unavailable")
    if version.scan_status not in {"passed", "passed_with_warnings"}:
        raise HubError(409, "scan_not_passed", "The version has not passed security scanning")
    if require_verified_signature and version.signature_status != "verified":
        raise HubError(
            409,
            "signature_not_verified",
            "Policy requires a verified trusted signature before publication",
        )
    approved_review = session.scalar(
        select(Review)
        .where(Review.version_id == version.id, Review.decision == "approved")
        .order_by(Review.created_at.desc())
    )
    if approved_review is None:
        raise HubError(409, "approval_missing", "An append-only approval record is required")
    if (
        approved_review.artifact_sha256 != version.sha256
        or approved_review.manifest_sha256 != _manifest_digest(version.manifest_json)
        or version.manifest_sha256 != approved_review.manifest_sha256
        or approved_review.signature_status != version.signature_status
    ):
        raise HubError(
            409, "approval_binding_mismatch", "Approved evidence does not match this payload"
        )
    version.status = "published"
    version.published_at = datetime.now(UTC)
    session.flush()
    session.add(
        RegistryChange(
            skill_id=version.skill_id,
            version_id=version.id,
            event_type="version.published",
            compatibility_index=_compatibility_index(
                list((version.manifest_json or {}).get("compatibility", []))
            ),
            payload_json={
                "version": version.semver,
                "sha256": version.sha256,
                "compatibility": list((version.manifest_json or {}).get("compatibility", [])),
            },
        )
    )
    if label_name:
        skill = version.skill
        existing = session.scalar(
            select(Label).where(Label.skill_id == skill.id, Label.label == label_name)
        )
        return move_label(
            session,
            skill=skill,
            label_name=label_name,
            target=version,
            if_match=existing.etag if existing else None,
        )
    return None


def deprecate_version(session: Session, version: SkillVersion) -> None:
    if version.status != "published":
        raise HubError(
            409, "invalid_lifecycle_transition", "Only a published version can be deprecated"
        )
    version.status = "deprecated"
    version.deprecated_at = datetime.now(UTC)
    labels = list(session.scalars(select(Label).where(Label.version_id == version.id)))
    for label in labels:
        session.add(
            RegistryChange(
                skill_id=version.skill_id,
                version_id=version.id,
                event_type="label.removed",
                compatibility_index=_compatibility_index(
                    list((version.manifest_json or {}).get("compatibility", []))
                ),
                payload_json={
                    "label": label.label,
                    "version": version.semver,
                    "sha256": version.sha256,
                    "compatibility": list((version.manifest_json or {}).get("compatibility", [])),
                },
            )
        )
        session.delete(label)
    session.add(
        RegistryChange(
            skill_id=version.skill_id,
            version_id=version.id,
            event_type="version.deprecated",
            compatibility_index=_compatibility_index(
                list((version.manifest_json or {}).get("compatibility", []))
            ),
            payload_json={
                "version": version.semver,
                "sha256": version.sha256,
                "compatibility": list((version.manifest_json or {}).get("compatibility", [])),
            },
        )
    )


def resolve_version(
    session: Session,
    *,
    skill: Skill,
    version_selector: str | None,
    label_selector: str | None,
) -> tuple[SkillVersion, dict[str, str]]:
    if version_selector and label_selector:
        raise HubError(400, "ambiguous_selector", "version and label are mutually exclusive")
    if version_selector:
        version = session.scalar(
            select(SkillVersion).where(
                SkillVersion.skill_id == skill.id,
                SkillVersion.semver == version_selector,
                SkillVersion.status.in_(["published", "deprecated"]),
            )
        )
        requested = {"version": version_selector}
    else:
        label_name = label_selector or "stable"
        label = session.scalar(
            select(Label)
            .options(selectinload(Label.version))
            .where(Label.skill_id == skill.id, Label.label == label_name)
        )
        version = label.version if label and label.version.status == "published" else None
        requested = {"label": label_name}
    if version is None or not version.sha256 or not version.artifact_key:
        raise HubError(404, "version_not_resolved", "No published version matches the selector")
    return version, requested


def sync_manifest(
    session: Session,
    *,
    principal: Principal,
    since: datetime | None,
    cursor: int | None,
    agent: str | None,
    limit: int,
) -> SyncManifestResponse:
    statement = (
        select(RegistryChange)
        .join(Skill, RegistryChange.skill_id == Skill.id)
        .options(
            selectinload(RegistryChange.skill).selectinload(Skill.namespace),
            selectinload(RegistryChange.version),
        )
        .order_by(RegistryChange.sequence.asc())
        .limit(limit + 1)
    )
    if not principal.is_admin:
        statement = statement.where(
            or_(Skill.visibility == "public", Skill.owner_id == principal.subject)
        )
    if cursor is not None:
        statement = statement.where(RegistryChange.sequence > cursor)
    elif since:
        statement = statement.where(RegistryChange.created_at > since)
    if agent:
        pattern = f"%|{agent.strip().lower()}|%"
        statement = statement.where(
            or_(
                RegistryChange.event_type != "version.published",
                RegistryChange.compatibility_index == "",
                RegistryChange.compatibility_index.ilike(pattern),
            )
        )
    changes = list(session.scalars(statement).unique())
    has_more = len(changes) > limit
    changes = changes[:limit]
    items: list[SyncManifestItem] = []
    for change in changes:
        payload = change.payload_json or {}
        version = change.version
        semver = str(payload.get("version") or (version.semver if version else ""))
        sha256 = str(payload.get("sha256") or (version.sha256 if version else ""))
        compatibility = list(payload.get("compatibility", []))
        labels = (
            [str(payload["label"])] if change.event_type in {"label.moved", "label.removed"} else []
        )
        items.append(
            SyncManifestItem(
                sequence=change.sequence,
                event=change.event_type,
                namespace=change.skill.namespace.slug,
                name=change.skill.slug,
                version=semver,
                sha256=sha256,
                labels=labels,
                compatibility=compatibility,
                tombstone=change.event_type in {"version.deprecated", "label.removed"},
                updated_at=change.created_at,
            )
        )
    max_sequence = session.scalar(
        select(RegistryChange.sequence).order_by(RegistryChange.sequence.desc())
    )
    next_cursor = str(changes[-1].sequence) if has_more and changes else None
    return SyncManifestResponse(
        items=items,
        server_time=datetime.now(UTC),
        next_cursor=next_cursor,
        high_watermark=max_sequence or 0,
    )


def record_installation(
    session: Session,
    *,
    principal: Principal,
    payload: InstallationReportIn,
) -> InstallationReport:
    skill = require_skill_read(
        load_skill_by_slug(session, payload.namespace, payload.name), principal
    )
    version = session.scalar(
        select(SkillVersion).where(
            SkillVersion.skill_id == skill.id,
            SkillVersion.semver == payload.version,
            SkillVersion.status.in_(["published", "deprecated"]),
        )
    )
    if version is None or version.sha256 != payload.remote_sha256:
        raise HubError(
            422,
            "installation_reference_invalid",
            "Reported version and digest do not identify an accessible release",
        )
    report = InstallationReport(
        principal_id=principal.subject,
        client_id=payload.client_id,
        agent=payload.agent,
        namespace=payload.namespace,
        skill_name=payload.name,
        version=payload.version,
        remote_sha256=payload.remote_sha256,
        local_sha256=payload.local_sha256,
        state=payload.state,
        conflict_reason=payload.conflict_reason,
    )
    session.add(report)
    session.flush()
    return report
