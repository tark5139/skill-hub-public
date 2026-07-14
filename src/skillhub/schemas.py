from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def _utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_serializer("*", when_used="json", check_fields=False)
    def serialize_datetime_fields(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return _utc_isoformat(value)
        return value


class SkillCreate(StrictModel):
    namespace: str
    name: str
    description: str = Field(min_length=1, max_length=4096)
    visibility: Literal["private", "public"] = "private"
    tags: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("namespace", "name")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if not SLUG_PATTERN.fullmatch(value):
            raise ValueError("must be a lowercase slug of at most 63 characters")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        normalized = sorted({item.strip().lower() for item in values if item.strip()})
        if any(len(item) > 63 for item in normalized):
            raise ValueError("tags must not exceed 63 characters")
        return normalized


class VersionSummary(StrictModel):
    id: str
    version: str
    status: str
    sha256: str | None
    scan_status: str
    signature_status: str
    created_at: datetime
    published_at: datetime | None


class LabelSummary(StrictModel):
    label: str
    version: str
    etag: str


class SkillSummary(StrictModel):
    id: str
    namespace: str
    name: str
    description: str
    visibility: str
    owner_id: str
    tags: list[str]
    labels: dict[str, str] = Field(default_factory=dict)
    updated_at: datetime


class SkillDetail(SkillSummary):
    versions: list[VersionSummary]
    label_details: list[LabelSummary]


class SearchPage(StrictModel):
    items: list[SkillSummary]
    next_cursor: str | None = None


class UploadCreate(StrictModel):
    version: str
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    license: str = Field(default="NOASSERTION", min_length=1, max_length=128)
    compatibility: list[str] = Field(default_factory=list, max_length=32)
    signature_key_id: str | None = Field(default=None, max_length=255)
    signature_base64: str | None = None

    @field_validator("version")
    @classmethod
    def validate_semver(cls, value: str) -> str:
        if not SEMVER_PATTERN.fullmatch(value):
            raise ValueError("version must be semantic versioning, for example 1.2.3")
        return value

    @field_validator("compatibility")
    @classmethod
    def normalize_compatibility(cls, values: list[str]) -> list[str]:
        return sorted({item.strip().lower() for item in values if item.strip()})


class UploadSessionOut(StrictModel):
    id: str
    skill_id: str
    version: str
    status: str
    upload_url: str
    max_archive_bytes: int
    expires_at: datetime | None = None


class UploadContentOut(StrictModel):
    id: str
    status: str
    size_bytes: int
    sha256: str


class VersionOut(StrictModel):
    id: str
    skill_id: str
    version: str
    status: str
    sha256: str | None
    scan_status: str
    signature_status: str
    manifest: dict[str, Any] | None


class ReviewRequest(StrictModel):
    decision: Literal["approved", "rejected"]
    evidence: dict[str, Any] = Field(default_factory=dict)


class PublishRequest(StrictModel):
    label: str | None = "stable"

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str | None) -> str | None:
        if value is not None and not LABEL_PATTERN.fullmatch(value):
            raise ValueError("invalid label")
        return value


class LabelUpdate(StrictModel):
    version: str

    @field_validator("version")
    @classmethod
    def validate_semver(cls, value: str) -> str:
        if not SEMVER_PATTERN.fullmatch(value):
            raise ValueError("version must be semantic versioning")
        return value


class ResolveResponse(StrictModel):
    namespace: str
    name: str
    requested: dict[str, str]
    resolved_version: str
    artifact_sha256: str
    manifest_sha256: str
    artifact_url: str
    manifest_url: str
    signature_status: str
    deprecated: bool = False
    etag: str


class SyncManifestItem(StrictModel):
    sequence: int
    event: Literal["version.published", "version.deprecated", "label.moved", "label.removed"]
    namespace: str
    name: str
    version: str
    sha256: str
    labels: list[str]
    compatibility: list[str]
    tombstone: bool = False
    updated_at: datetime


class SyncManifestResponse(StrictModel):
    items: list[SyncManifestItem]
    server_time: datetime
    next_cursor: str | None = None
    high_watermark: int


class InstallationReportIn(StrictModel):
    client_id: str = Field(min_length=1, max_length=255)
    agent: str = Field(min_length=1, max_length=64)
    namespace: str
    name: str
    version: str
    remote_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    local_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    state: Literal["installed", "local_changes", "conflict", "rolled_back", "removed"]
    conflict_reason: str | None = Field(default=None, max_length=4096)


class MeResponse(StrictModel):
    subject: str
    scopes: list[str]
    is_admin: bool


class AgentCapability(StrictModel):
    name: str
    mode: Literal["formal", "preview", "cloud_connector"]


class SystemCapabilitiesResponse(StrictModel):
    environment: Literal["development", "test", "production"]
    require_signature: bool
    storage_backend: Literal["local", "s3"]
    deployment_region: str
    official_client_os: str
    official_client_arch: str
    github_owner: str
    github_repository: str
    github_approver: str
    agents: list[AgentCapability]


class GitHubAuthorizationRequest(StrictModel):
    confirmation: Literal["PUBLISH_PUBLICLY"]
    license_confirmed: bool
    sensitive_content_reviewed: bool
