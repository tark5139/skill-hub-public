from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Namespace(Base, TimestampMixin):
    __tablename__ = "namespaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    owner_org: Mapped[str] = mapped_column(String(255))

    skills: Mapped[list[Skill]] = relationship(back_populates="namespace")


class Skill(Base, TimestampMixin):
    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("namespace_id", "slug", name="uq_skills_namespace_slug"),
        Index("ix_skills_visibility_owner", "visibility", "owner_id"),
        CheckConstraint("visibility IN ('private', 'public')", name="ck_skills_visibility"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    namespace_id: Mapped[str] = mapped_column(ForeignKey("namespaces.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(63), index=True)
    description: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[str] = mapped_column(String(255), index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="private", index=True)
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list)

    namespace: Mapped[Namespace] = relationship(back_populates="skills")
    versions: Mapped[list[SkillVersion]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )
    labels: Mapped[list[Label]] = relationship(back_populates="skill", cascade="all, delete-orphan")
    uploads: Mapped[list[UploadSession]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )


class UploadSession(Base, TimestampMixin):
    __tablename__ = "upload_sessions"
    __table_args__ = (
        UniqueConstraint("skill_id", "version", name="uq_upload_sessions_skill_version"),
        CheckConstraint(
            "status IN ('created', 'uploaded', 'finalized', 'scanned', 'rejected')",
            name="ck_upload_sessions_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"), index=True)
    version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    quarantine_key: Mapped[str] = mapped_column(String(1024), unique=True)
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    declared_license: Mapped[str] = mapped_column(String(128), default="NOASSERTION")
    signature_key_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signature_base64: Mapped[str | None] = mapped_column(Text, nullable=True)
    compatibility_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    error_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    skill: Mapped[Skill] = relationship(back_populates="uploads")
    version_record: Mapped[SkillVersion | None] = relationship(
        back_populates="upload", uselist=False
    )


class SkillVersion(Base, TimestampMixin):
    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint("skill_id", "semver", name="uq_skill_versions_skill_semver"),
        UniqueConstraint("id", "skill_id", name="uq_skill_versions_id_skill"),
        Index("ix_skill_versions_skill_status", "skill_id", "status"),
        CheckConstraint(
            "status IN ('pending_scan', 'scanning', 'scan_failed', 'draft', 'submitted', "
            "'approved', 'rejected', 'published', 'deprecated')",
            name="ck_skill_versions_status",
        ),
        CheckConstraint(
            "scan_status IN ('pending', 'failed', 'passed', 'passed_with_warnings')",
            name="ck_skill_versions_scan_status",
        ),
        CheckConstraint(
            "signature_status IN ('not_checked', 'pending', 'verified', 'not_required', "
            "'required_missing')",
            name="ck_skill_versions_signature_status",
        ),
        CheckConstraint("sha256 IS NULL OR length(sha256) = 64", name="ck_skill_versions_sha256"),
        CheckConstraint(
            "manifest_sha256 IS NULL OR length(manifest_sha256) = 64",
            name="ck_skill_versions_manifest_sha256",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"), index=True)
    upload_id: Mapped[str | None] = mapped_column(
        ForeignKey("upload_sessions.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    semver: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="pending_scan", index=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    artifact_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    manifest_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    scan_status: Mapped[str] = mapped_column(String(32), default="pending")
    signature_status: Mapped[str] = mapped_column(String(32), default="not_checked")
    scan_attempts: Mapped[int] = mapped_column(Integer, default=0)
    scan_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    immutable: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    skill: Mapped[Skill] = relationship(back_populates="versions")
    upload: Mapped[UploadSession | None] = relationship(back_populates="version_record")
    files: Mapped[list[SkillFile]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )
    reviews: Mapped[list[Review]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )
    publications: Mapped[list[ExternalPublication]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class SkillFile(Base):
    __tablename__ = "skill_files"
    __table_args__ = (UniqueConstraint("version_id", "path", name="uq_skill_files_version_path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    version: Mapped[SkillVersion] = relationship(back_populates="files")


class Label(Base, TimestampMixin):
    __tablename__ = "labels"
    __table_args__ = (
        UniqueConstraint("skill_id", "label", name="uq_labels_skill_label"),
        ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], ondelete="CASCADE", name="fk_labels_skill"
        ),
        ForeignKeyConstraint(
            ["version_id", "skill_id"],
            ["skill_versions.id", "skill_versions.skill_id"],
            ondelete="RESTRICT",
            name="fk_labels_version_same_skill",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    skill_id: Mapped[str] = mapped_column(String(36), index=True)
    label: Mapped[str] = mapped_column(String(63))
    version_id: Mapped[str] = mapped_column(String(36))
    etag: Mapped[str] = mapped_column(String(128))

    skill: Mapped[Skill] = relationship(back_populates="labels")
    version: Mapped[SkillVersion] = relationship(viewonly=True)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("decision IN ('approved', 'rejected')", name="ck_reviews_decision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="CASCADE"), index=True
    )
    reviewer: Mapped[str] = mapped_column(String(255))
    decision: Mapped[str] = mapped_column(String(32))
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    signature_status: Mapped[str] = mapped_column(String(32))
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    version: Mapped[SkillVersion] = relationship(back_populates="reviews")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_target_created", "target_type", "target_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(255), index=True)
    action: Mapped[str] = mapped_column(String(255), index=True)
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(255))
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InstallationReport(Base):
    __tablename__ = "installation_reports"
    __table_args__ = (
        CheckConstraint(
            "state IN ('installed', 'local_changes', 'conflict', 'rolled_back', 'removed')",
            name="ck_installation_reports_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(String(255), index=True)
    client_id: Mapped[str] = mapped_column(String(255), index=True)
    agent: Mapped[str] = mapped_column(String(64))
    namespace: Mapped[str] = mapped_column(String(63))
    skill_name: Mapped[str] = mapped_column(String(63))
    version: Mapped[str] = mapped_column(String(128))
    remote_sha256: Mapped[str] = mapped_column(String(64))
    local_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32))
    conflict_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RegistryChange(Base):
    """Monotonic, authorization-filtered client change feed."""

    __tablename__ = "registry_changes"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('version.published', 'version.deprecated', 'label.moved', "
            "'label.removed')",
            name="ck_registry_changes_event_type",
        ),
    )

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"), index=True)
    version_id: Mapped[str | None] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    compatibility_index: Mapped[str] = mapped_column(Text, default="", index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    skill: Mapped[Skill] = relationship()
    version: Mapped[SkillVersion | None] = relationship()


class ExternalPublication(Base, TimestampMixin):
    __tablename__ = "external_publications"
    __table_args__ = (
        UniqueConstraint("version_id", "provider", name="uq_publication_version_provider"),
        CheckConstraint(
            "status IN ('requested', 'authorized', 'publishing', 'published', 'failed_policy', "
            "'failed_manual_review')",
            name="ck_external_publications_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), default="github")
    status: Mapped[str] = mapped_column(String(32), default="requested")
    authorized_by: Mapped[str] = mapped_column(String(255))
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    destination_owner: Mapped[str] = mapped_column(String(255))
    destination_repository: Mapped[str] = mapped_column(String(255))
    tag_name: Mapped[str] = mapped_column(String(255))
    policy_version: Mapped[str] = mapped_column(String(32), default="github-public-v1")
    published_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_release_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    version: Mapped[SkillVersion] = relationship(back_populates="publications")


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("principal_id", "idempotency_key", name="uq_idempotency_principal_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(String(255), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(1024))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
