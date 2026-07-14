from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from ..audit import append_audit
from ..auth import Principal, require_scope
from ..db import get_session
from ..errors import HubError
from ..models import Skill, SkillVersion, UploadSession
from ..schemas import (
    GitHubAuthorizationRequest,
    InstallationReportIn,
    LabelUpdate,
    MeResponse,
    PublishRequest,
    ResolveResponse,
    ReviewRequest,
    SearchPage,
    SkillCreate,
    SkillDetail,
    SkillSummary,
    SyncManifestResponse,
    SystemCapabilitiesResponse,
    UploadContentOut,
    UploadCreate,
    UploadSessionOut,
    VersionOut,
)
from ..services import registry
from ..services.idempotency import lock_key, remember_response, replay_if_present, request_digest

router = APIRouter(prefix="/api/v1")
SessionDep = Annotated[Session, Depends(get_session)]


def _request_id(request: Request) -> str:
    return request.state.request_id


def _commit(session: Session) -> None:
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise


@router.get("/me", response_model=MeResponse, tags=["identity"])
def me(principal: Principal = Depends(require_scope("skill.read"))) -> MeResponse:
    return MeResponse(
        subject=principal.subject,
        scopes=sorted(principal.scopes),
        is_admin=principal.is_admin,
    )


@router.get(
    "/system/capabilities",
    response_model=SystemCapabilitiesResponse,
    tags=["identity"],
)
def system_capabilities(
    request: Request,
    principal: Principal = Depends(require_scope("skill.read")),
) -> SystemCapabilitiesResponse:
    del principal
    settings = request.app.state.settings
    return SystemCapabilitiesResponse(
        environment=settings.env,
        require_signature=settings.require_signature,
        storage_backend=settings.storage_backend,
        deployment_region=settings.s3_region,
        official_client_os="macOS 13+",
        official_client_arch="Apple Silicon (arm64)",
        github_owner=settings.github_owner,
        github_repository=settings.github_repository,
        github_approver=settings.github_approver,
        agents=[
            {"name": "Codex", "mode": "formal"},
            {"name": "Claude Code", "mode": "formal"},
            {"name": "TRAE CN", "mode": "formal"},
            {"name": "OpenClaw", "mode": "formal"},
            {"name": "Hermes Agent", "mode": "formal"},
            {"name": "WorkBuddy", "mode": "preview"},
            {"name": "Feishu Aily", "mode": "cloud_connector"},
        ],
    )


@router.get("/skills", response_model=SearchPage, tags=["registry"])
def list_skills(
    session: SessionDep,
    q: str | None = Query(default=None, max_length=256),
    tag: list[str] = Query(default=[]),
    compatibility: str | None = Query(default=None, max_length=64),
    owner: str | None = Query(default=None, max_length=255),
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require_scope("skill.read")),
) -> SearchPage:
    return registry.search_skills(
        session,
        principal=principal,
        query=q,
        tags=tag,
        compatibility=compatibility,
        owner=owner,
        cursor=cursor,
        limit=limit,
    )


@router.post("/skills", response_model=SkillSummary, status_code=201, tags=["registry"])
def create_skill(
    payload: SkillCreate,
    request: Request,
    session: SessionDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    principal: Principal = Depends(require_scope("skill.write")),
) -> SkillSummary | JSONResponse:
    digest = request_digest(payload.model_dump(mode="json"))
    lock_key(session, principal_id=principal.subject, key=idempotency_key)
    replay = replay_if_present(
        session,
        principal_id=principal.subject,
        key=idempotency_key,
        method="POST",
        path="/api/v1/skills",
        request_hash=digest,
    )
    if replay:
        status, body = replay
        return JSONResponse(
            status_code=status, content=body, headers={"Idempotent-Replayed": "true"}
        )
    skill = registry.create_skill(session, payload, principal)
    result = registry.skill_summary(skill)
    body = result.model_dump(mode="json")
    append_audit(
        session,
        actor=principal.subject,
        action="skill.created",
        target_type="skill",
        target_id=skill.id,
        request_id=_request_id(request),
        after=body,
    )
    remember_response(
        session,
        principal_id=principal.subject,
        key=idempotency_key,
        method="POST",
        path="/api/v1/skills",
        request_hash=digest,
        status_code=201,
        response=body,
    )
    _commit(session)
    return result


@router.get("/skills/{namespace}/{name}", response_model=SkillDetail, tags=["registry"])
def get_skill(
    namespace: str,
    name: str,
    session: SessionDep,
    principal: Principal = Depends(require_scope("skill.read")),
) -> SkillDetail:
    skill = registry.require_skill_read(
        registry.load_skill_by_slug(session, namespace, name), principal
    )
    return registry.skill_detail(skill, principal)


@router.post(
    "/skills/{skill_id}/uploads",
    response_model=UploadSessionOut,
    status_code=201,
    tags=["publishing"],
)
def create_upload(
    skill_id: str,
    payload: UploadCreate,
    request: Request,
    session: SessionDep,
    principal: Principal = Depends(require_scope("skill.write")),
) -> UploadSessionOut:
    skill = registry.require_skill_owner(registry.load_skill_by_id(session, skill_id), principal)
    upload = registry.create_upload(session, skill=skill, payload=payload)
    append_audit(
        session,
        actor=principal.subject,
        action="upload.created",
        target_type="upload",
        target_id=upload.id,
        request_id=_request_id(request),
        after={"skill_id": skill.id, "version": upload.version},
    )
    _commit(session)
    return UploadSessionOut(
        id=upload.id,
        skill_id=upload.skill_id,
        version=upload.version,
        status=upload.status,
        upload_url=f"/api/v1/uploads/{upload.id}/content",
        max_archive_bytes=request.app.state.settings.max_archive_bytes,
    )


@router.put(
    "/uploads/{upload_id}/content",
    response_model=UploadContentOut,
    tags=["publishing"],
)
async def upload_content(
    upload_id: str,
    request: Request,
    session: SessionDep,
    content_type: str | None = Header(default=None, alias="Content-Type"),
    principal: Principal = Depends(require_scope("skill.write")),
) -> UploadContentOut:
    if content_type and content_type.split(";", 1)[0].strip() not in {
        "application/zip",
        "application/octet-stream",
    }:
        raise HubError(415, "unsupported_media_type", "Upload content must be a ZIP archive")
    upload = session.scalar(
        select(UploadSession)
        .options(selectinload(UploadSession.skill))
        .where(UploadSession.id == upload_id)
    )
    if upload is None:
        raise HubError(404, "upload_not_found", "The upload session does not exist")
    registry.require_skill_owner(upload.skill, principal)
    declared_length = request.headers.get("Content-Length")
    maximum = request.app.state.settings.max_archive_bytes
    if declared_length:
        try:
            if int(declared_length) > maximum:
                raise HubError(413, "archive_too_large", f"Archive exceeds {maximum} bytes")
        except ValueError as exc:
            raise HubError(400, "invalid_content_length", "Content-Length is invalid") from exc
    buffered = bytearray()
    async for chunk in request.stream():
        buffered.extend(chunk)
        if len(buffered) > maximum:
            raise HubError(413, "archive_too_large", f"Archive exceeds {maximum} bytes")
    content = bytes(buffered)
    if upload.status not in {"created", "uploaded"}:
        raise HubError(409, "upload_not_writable", "The upload session is no longer writable")
    if not content:
        raise HubError(422, "empty_archive", "The uploaded archive is empty")
    digest = hashlib.sha256(content).hexdigest()
    if upload.expected_sha256 and upload.expected_sha256 != digest:
        raise HubError(422, "digest_mismatch", "Archive SHA-256 does not match expected_sha256")
    if upload.status == "uploaded" and upload.actual_sha256 != digest:
        raise HubError(409, "upload_already_stored", "Upload content is immutable once stored")
    try:
        await run_in_threadpool(
            request.app.state.storage.put_quarantine,
            upload.quarantine_key,
            content,
        )
    except FileExistsError as exc:
        raise HubError(
            409, "upload_already_stored", "A different archive is already stored"
        ) from exc
    upload.actual_sha256 = digest
    upload.size_bytes = len(content)
    upload.status = "uploaded"
    session.flush()
    append_audit(
        session,
        actor=principal.subject,
        action="upload.content_stored",
        target_type="upload",
        target_id=upload.id,
        request_id=_request_id(request),
        after={"sha256": upload.actual_sha256, "size_bytes": upload.size_bytes},
    )
    _commit(session)
    return UploadContentOut(
        id=upload.id,
        status=upload.status,
        size_bytes=upload.size_bytes or 0,
        sha256=upload.actual_sha256 or "",
    )


@router.post(
    "/uploads/{upload_id}:finalize",
    response_model=VersionOut,
    status_code=202,
    tags=["publishing"],
)
def finalize_upload(
    upload_id: str,
    request: Request,
    session: SessionDep,
    principal: Principal = Depends(require_scope("skill.write")),
) -> VersionOut:
    statement = (
        select(UploadSession)
        .options(
            selectinload(UploadSession.skill),
            selectinload(UploadSession.version_record),
        )
        .where(UploadSession.id == upload_id)
    )
    if session.bind and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    upload = session.scalar(statement)
    if upload is None:
        raise HubError(404, "upload_not_found", "The upload session does not exist")
    registry.require_skill_owner(upload.skill, principal)
    version = registry.finalize_upload(session, upload, request.app.state.storage)
    append_audit(
        session,
        actor=principal.subject,
        action="version.scan_queued",
        target_type="version",
        target_id=version.id,
        request_id=_request_id(request),
        after={"version": version.semver, "sha256": version.sha256},
    )
    _commit(session)
    return registry.version_out(version)


@router.get("/versions/{version_id}", response_model=VersionOut, tags=["publishing"])
def get_version(
    version_id: str,
    session: SessionDep,
    principal: Principal = Depends(require_scope("skill.read")),
) -> VersionOut:
    version = registry.require_version(session, version_id)
    registry.require_skill_read(version.skill, principal)
    if (
        not principal.is_admin
        and version.skill.owner_id != principal.subject
        and version.status not in {"published", "deprecated"}
    ):
        raise HubError(404, "version_not_found", "The Skill version does not exist")
    return registry.version_out(version)


@router.post("/versions/{version_id}:submit", response_model=VersionOut, tags=["publishing"])
def submit_version(
    version_id: str,
    request: Request,
    session: SessionDep,
    principal: Principal = Depends(require_scope("skill.write")),
) -> VersionOut:
    version = registry.require_version(session, version_id)
    registry.require_skill_owner(version.skill, principal)
    before = {"status": version.status}
    registry.submit_version(version)
    append_audit(
        session,
        actor=principal.subject,
        action="version.submitted",
        target_type="version",
        target_id=version.id,
        request_id=_request_id(request),
        before=before,
        after={"status": version.status},
    )
    _commit(session)
    return registry.version_out(version)


@router.post("/versions/{version_id}:approve", response_model=VersionOut, tags=["publishing"])
def approve_version(
    version_id: str,
    payload: ReviewRequest,
    request: Request,
    session: SessionDep,
    principal: Principal = Depends(require_scope("skill.review")),
) -> VersionOut:
    version = registry.require_version(session, version_id)
    registry.require_skill_owner(version.skill, principal)
    before = {"status": version.status}
    registry.review_version(
        session,
        version=version,
        reviewer=principal.subject,
        decision=payload.decision,
        evidence=payload.evidence,
    )
    append_audit(
        session,
        actor=principal.subject,
        action=f"version.{payload.decision}",
        target_type="version",
        target_id=version.id,
        request_id=_request_id(request),
        before=before,
        after={"status": version.status, "evidence": payload.evidence},
    )
    _commit(session)
    return registry.version_out(version)


@router.post("/versions/{version_id}:publish", response_model=VersionOut, tags=["publishing"])
def publish_version(
    version_id: str,
    payload: PublishRequest,
    request: Request,
    session: SessionDep,
    principal: Principal = Depends(require_scope("skill.publish")),
) -> VersionOut:
    version = registry.require_version(session, version_id)
    registry.require_skill_owner(version.skill, principal)
    before = {"status": version.status}
    label = registry.publish_version(
        session,
        version=version,
        label_name=payload.label,
        require_verified_signature=request.app.state.settings.require_signature,
    )
    append_audit(
        session,
        actor=principal.subject,
        action="version.published",
        target_type="version",
        target_id=version.id,
        request_id=_request_id(request),
        before=before,
        after={
            "status": version.status,
            "sha256": version.sha256,
            "label": label.label if label else None,
        },
    )
    _commit(session)
    return registry.version_out(version)


@router.post("/versions/{version_id}:deprecate", response_model=VersionOut, tags=["publishing"])
def deprecate_version(
    version_id: str,
    request: Request,
    session: SessionDep,
    principal: Principal = Depends(require_scope("skill.publish")),
) -> VersionOut:
    version = registry.require_version(session, version_id)
    registry.require_skill_owner(version.skill, principal)
    registry.deprecate_version(session, version)
    append_audit(
        session,
        actor=principal.subject,
        action="version.deprecated",
        target_type="version",
        target_id=version.id,
        request_id=_request_id(request),
        after={"status": version.status},
    )
    _commit(session)
    return registry.version_out(version)


@router.put("/skills/{skill_id}/labels/{label_name}", tags=["publishing"])
def set_label(
    skill_id: str,
    label_name: str,
    payload: LabelUpdate,
    request: Request,
    session: SessionDep,
    if_match: str | None = Header(default=None, alias="If-Match"),
    principal: Principal = Depends(require_scope("skill.publish")),
) -> Response:
    skill = registry.require_skill_owner(registry.load_skill_by_id(session, skill_id), principal)
    version = session.scalar(
        select(SkillVersion).where(
            SkillVersion.skill_id == skill.id, SkillVersion.semver == payload.version
        )
    )
    if version is None:
        raise HubError(404, "version_not_found", "The Skill version does not exist")
    label = registry.move_label(
        session,
        skill=skill,
        label_name=label_name,
        target=version,
        if_match=if_match,
    )
    append_audit(
        session,
        actor=principal.subject,
        action="label.moved",
        target_type="label",
        target_id=label.id,
        request_id=_request_id(request),
        after={"label": label.label, "version": version.semver, "etag": label.etag},
    )
    _commit(session)
    return JSONResponse(
        content={"label": label.label, "version": version.semver, "etag": label.etag},
        headers={"ETag": label.etag},
    )


@router.get(
    "/skills/{namespace}/{name}/resolve",
    response_model=ResolveResponse,
    tags=["distribution"],
)
def resolve_skill(
    namespace: str,
    name: str,
    request: Request,
    response: Response,
    session: SessionDep,
    version: str | None = Query(default=None),
    label: str | None = Query(default=None),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    principal: Principal = Depends(require_scope("skill.read")),
) -> ResolveResponse | Response:
    skill = registry.require_skill_read(
        registry.load_skill_by_slug(session, namespace, name), principal
    )
    resolved, requested = registry.resolve_version(
        session,
        skill=skill,
        version_selector=version,
        label_selector=label,
    )
    assert resolved.sha256 is not None
    if not resolved.manifest_sha256:
        raise HubError(503, "manifest_unavailable", "The server manifest is unavailable")
    etag = f'"sha256:{resolved.sha256}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, no-cache"
    if resolved.status == "deprecated":
        response.headers["Warning"] = '299 Skill-Hub "Resolved version is deprecated"'
        response.headers["X-Skill-Deprecated"] = "true"
    return ResolveResponse(
        namespace=namespace,
        name=name,
        requested=requested,
        resolved_version=resolved.semver,
        artifact_sha256=resolved.sha256,
        manifest_sha256=resolved.manifest_sha256,
        artifact_url=f"/api/v1/artifacts/{resolved.sha256}?version_id={resolved.id}",
        manifest_url=f"/api/v1/manifests/{resolved.manifest_sha256}",
        signature_status=resolved.signature_status,
        deprecated=resolved.status == "deprecated",
        etag=etag,
    )


def _version_for_digest(
    session: Session,
    digest: str,
    principal: Principal,
    *,
    version_id: str | None = None,
) -> SkillVersion:
    statement = (
        select(SkillVersion)
        .join(Skill)
        .options(selectinload(SkillVersion.skill).selectinload(Skill.namespace))
        .where(
            SkillVersion.sha256 == digest,
            SkillVersion.status.in_(["published", "deprecated"]),
        )
        .order_by(SkillVersion.published_at.desc())
    )
    if version_id:
        statement = statement.where(SkillVersion.id == version_id)
    if not principal.is_admin:
        statement = statement.where(
            (Skill.visibility == "public") | (Skill.owner_id == principal.subject)
        )
    version = session.scalar(statement)
    if version is None:
        raise HubError(404, "artifact_not_found", "The artifact does not exist")
    registry.require_skill_read(version.skill, principal)
    return version


@router.get("/artifacts/{digest}", tags=["distribution"])
def download_artifact(
    digest: str,
    request: Request,
    session: SessionDep,
    version_id: str | None = Query(default=None),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    principal: Principal = Depends(require_scope("skill.read")),
) -> Response:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise HubError(404, "artifact_not_found", "The artifact does not exist")
    version = _version_for_digest(session, digest, principal, version_id=version_id)
    assert version.artifact_key is not None
    etag = f'"sha256:{digest}"'
    headers = {
        "ETag": etag,
        "Digest": f"sha-256={base64.b64encode(bytes.fromhex(digest)).decode()}",
        "X-Skill-Resolved-Version": version.semver,
        "Cache-Control": "private, max-age=0, must-revalidate",
        "Content-Disposition": f'attachment; filename="{version.skill.slug}-{version.semver}.zip"',
    }
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    signed_url = request.app.state.storage.presign_release(version.artifact_key)
    if signed_url:
        return RedirectResponse(signed_url, status_code=307, headers=headers)
    try:
        content = request.app.state.storage.get_release(version.artifact_key)
    except FileNotFoundError as exc:
        raise HubError(
            503, "artifact_unavailable", "Artifact metadata exists but storage is unavailable"
        ) from exc
    if not secrets_compare_digest(hashlib.sha256(content).hexdigest(), digest):
        raise HubError(
            503, "artifact_integrity_failure", "Stored artifact failed integrity verification"
        )
    return Response(content=content, media_type="application/zip", headers=headers)


def secrets_compare_digest(left: str, right: str) -> bool:
    # Isolated helper keeps the response path straightforward and timing-safe.
    import secrets

    return secrets.compare_digest(left, right)


@router.get("/manifests/{digest}", tags=["distribution"])
def get_manifest(
    digest: str,
    session: SessionDep,
    principal: Principal = Depends(require_scope("skill.read")),
) -> Response:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise HubError(404, "manifest_not_found", "The manifest does not exist")
    statement = (
        select(SkillVersion)
        .join(Skill)
        .options(selectinload(SkillVersion.skill).selectinload(Skill.namespace))
        .where(
            SkillVersion.manifest_sha256 == digest,
            SkillVersion.status.in_(["published", "deprecated"]),
        )
    )
    if not principal.is_admin:
        statement = statement.where(
            (Skill.visibility == "public") | (Skill.owner_id == principal.subject)
        )
    version = session.scalar(statement)
    if version is None:
        raise HubError(404, "manifest_not_found", "The manifest does not exist")
    if not version.manifest_json:
        raise HubError(503, "manifest_unavailable", "The server manifest is unavailable")
    etag = f'"manifest:{digest}"'
    return JSONResponse(
        content=version.manifest_json,
        headers={"ETag": etag, "Cache-Control": "private, no-cache"},
    )


@router.get("/sync/manifest", response_model=SyncManifestResponse, tags=["sync"])
def get_sync_manifest(
    session: SessionDep,
    since: datetime | None = Query(default=None),
    cursor: int | None = Query(default=None, ge=0),
    agent: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=500, ge=1, le=2_000),
    principal: Principal = Depends(require_scope("agent.sync")),
) -> SyncManifestResponse:
    return registry.sync_manifest(
        session,
        principal=principal,
        since=since,
        cursor=cursor,
        agent=agent,
        limit=limit,
    )


@router.post("/sync/reports", status_code=202, tags=["sync"])
def submit_sync_report(
    payload: InstallationReportIn,
    request: Request,
    session: SessionDep,
    principal: Principal = Depends(require_scope("agent.sync")),
) -> dict[str, str]:
    report = registry.record_installation(session, principal=principal, payload=payload)
    append_audit(
        session,
        actor=principal.subject,
        action="installation.reported",
        target_type="installation_report",
        target_id=report.id,
        request_id=_request_id(request),
        after={"agent": payload.agent, "state": payload.state},
    )
    _commit(session)
    return {"id": report.id, "status": "accepted"}


@router.post("/versions/{version_id}/github-authorization", status_code=202, tags=["publishing"])
def authorize_github_publication(
    version_id: str,
    payload: GitHubAuthorizationRequest,
    request: Request,
    session: SessionDep,
    principal: Principal = Depends(require_scope("skill.publish")),
) -> dict[str, Any]:
    # The publisher module performs the external operation. This endpoint records the exact,
    # immutable version authorization and intentionally does not make a network call.
    from ..models import ExternalPublication

    version = registry.require_version(session, version_id)
    registry.require_skill_owner(version.skill, principal)
    if principal.subject != request.app.state.settings.github_approver:
        raise HubError(
            403,
            "approver_required",
            "Only the configured public-release approver may authorize export",
        )
    if version.status != "published" or not version.immutable:
        raise HubError(
            409, "version_not_publishable", "Only an immutable published version can be exported"
        )
    if version.skill.visibility != "public":
        raise HubError(
            409, "skill_not_public", "The Skill visibility must be public before GitHub export"
        )
    if not version.sha256 or not version.manifest_sha256:
        raise HubError(
            409, "release_digest_missing", "Artifact and manifest digests must be frozen"
        )
    if not payload.license_confirmed or not payload.sensitive_content_reviewed:
        raise HubError(
            422,
            "publication_checks_incomplete",
            "License and sensitive-content checks must be confirmed",
        )
    existing = session.scalar(
        select(ExternalPublication).where(
            ExternalPublication.version_id == version.id,
            ExternalPublication.provider == "github",
        )
    )
    if existing and existing.status in {"authorized", "publishing", "published"}:
        publication = existing
    else:
        publication = existing or ExternalPublication(
            version_id=version.id,
            provider="github",
            authorized_by=principal.subject,
            artifact_sha256=version.sha256,
            manifest_sha256=version.manifest_sha256,
            destination_owner=request.app.state.settings.github_owner,
            destination_repository=request.app.state.settings.github_repository,
            tag_name=f"{version.skill.slug}-v{version.semver}",
        )
        publication.status = "authorized"
        publication.authorized_by = principal.subject
        publication.artifact_sha256 = version.sha256
        publication.manifest_sha256 = version.manifest_sha256
        publication.destination_owner = request.app.state.settings.github_owner
        publication.destination_repository = request.app.state.settings.github_repository
        publication.tag_name = f"{version.skill.slug}-v{version.semver}"
        publication.policy_version = "github-public-v1"
        publication.evidence_json = payload.model_dump(mode="json")
        session.add(publication)
    append_audit(
        session,
        actor=principal.subject,
        action="github.publication_authorized",
        target_type="version",
        target_id=version.id,
        request_id=_request_id(request),
        after={"publication_id": publication.id, "sha256": version.sha256},
    )
    _commit(session)
    return {"id": publication.id, "status": publication.status, "version_id": version.id}
