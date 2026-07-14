"""Publish explicitly approved Skill versions to a public GitHub Release.

The private Skill Hub remains the system of record.  This module intentionally has no import,
pull, reconciliation, or overwrite path from GitHub.  A publication is a one-way derivation of
one immutable, approved Skill version.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib import error, parse, request

DEFAULT_GITHUB_OWNER = "tark5139"
DEFAULT_GITHUB_REPOSITORY = "skill-hub-public"
DEFAULT_GITHUB_APPROVER = "tark5139"
PUBLICATION_SCOPE = "github_public_release"

_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class PublicationError(RuntimeError):
    """Base class for safe publication failures."""


class AuthorizationError(PublicationError):
    """The per-version public publication authorization is absent or invalid."""


class IntegrityError(PublicationError):
    """The package, manifest, or authorization digests do not agree."""


class GitHubConflictError(PublicationError):
    """The tag or release already exists; publication must never overwrite it."""


class AssetVerificationError(PublicationError):
    """GitHub's uploaded asset inventory differs from the intended immutable set."""


class GitHubAPIError(PublicationError):
    """A GitHub API operation failed."""

    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(f"GitHub API returned {status}: {message}")
        self.status = status
        self.body = body


@dataclass(frozen=True, slots=True)
class VersionPublicationAuthorization:
    """An explicit approval bound to one version, artifact digest, and destination."""

    skill: str
    version: str
    artifact_sha256: str
    approved_by: str
    approval_id: str
    approved_at: datetime
    destination_owner: str = DEFAULT_GITHUB_OWNER
    destination_repository: str = DEFAULT_GITHUB_REPOSITORY
    decision: str = "approved"
    scope: str = PUBLICATION_SCOPE


@dataclass(frozen=True, slots=True)
class PublicSkillVersion:
    """Inputs needed to derive a public release without consulting GitHub for content."""

    skill: str
    version: str
    package_bytes: bytes
    manifest: Mapping[str, Any]
    signature: bytes
    license_text: str
    readme_text: str | None = None
    attestation_text: str | None = None


@dataclass(frozen=True, slots=True)
class ImmutableAsset:
    name: str
    content: bytes
    media_type: str
    sha256: str

    @classmethod
    def build(cls, name: str, content: bytes, media_type: str) -> ImmutableAsset:
        return cls(
            name=name,
            content=content,
            media_type=media_type,
            sha256=hashlib.sha256(content).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class GitHubAsset:
    name: str
    size: int
    digest: str | None
    browser_download_url: str | None = None

    @classmethod
    def from_api(cls, value: Mapping[str, Any]) -> GitHubAsset:
        return cls(
            name=str(value["name"]),
            size=int(value["size"]),
            digest=str(value["digest"]) if value.get("digest") else None,
            browser_download_url=(
                str(value["browser_download_url"]) if value.get("browser_download_url") else None
            ),
        )


@dataclass(frozen=True, slots=True)
class GitHubRelease:
    id: int
    tag_name: str
    draft: bool
    upload_url: str
    html_url: str | None = None
    immutable: bool | None = None

    @classmethod
    def from_api(cls, value: Mapping[str, Any]) -> GitHubRelease:
        return cls(
            id=int(value["id"]),
            tag_name=str(value["tag_name"]),
            draft=bool(value["draft"]),
            upload_url=str(value.get("upload_url", "")),
            html_url=str(value["html_url"]) if value.get("html_url") else None,
            immutable=bool(value["immutable"]) if "immutable" in value else None,
        )


@dataclass(frozen=True, slots=True)
class PublishedGitHubRelease:
    release_id: int
    tag: str
    html_url: str
    immutable: bool | None
    assets: tuple[GitHubAsset, ...]
    approval_id: str


class GitHubReleaseClient(Protocol):
    """Injectable network boundary used by :class:`GitHubPublisher`."""

    def immutable_releases_enabled(self, owner: str, repository: str) -> bool: ...

    def tag_exists(self, owner: str, repository: str, tag: str) -> bool: ...

    def get_release_by_tag(self, owner: str, repository: str, tag: str) -> GitHubRelease | None: ...

    def create_draft_release(
        self,
        owner: str,
        repository: str,
        *,
        tag: str,
        name: str,
        body: str,
        target_commitish: str,
    ) -> GitHubRelease: ...

    def upload_asset(
        self,
        release: GitHubRelease,
        *,
        name: str,
        content: bytes,
        media_type: str,
    ) -> GitHubAsset: ...

    def list_assets(
        self, owner: str, repository: str, release_id: int
    ) -> Sequence[GitHubAsset]: ...

    def publish_release(self, owner: str, repository: str, release_id: int) -> GitHubRelease: ...


@dataclass(frozen=True, slots=True)
class GitHubPublisherConfig:
    owner: str = DEFAULT_GITHUB_OWNER
    repository: str = DEFAULT_GITHUB_REPOSITORY
    approver: str = DEFAULT_GITHUB_APPROVER
    target_commitish: str = "main"
    require_immutable_release: bool = True


class GitHubPublisher:
    """Create a verified, never-overwritten public mirror release."""

    def __init__(
        self,
        client: GitHubReleaseClient,
        config: GitHubPublisherConfig | None = None,
    ) -> None:
        self.client = client
        self.config = config or GitHubPublisherConfig()

    def publish(
        self,
        skill_version: PublicSkillVersion,
        authorization: VersionPublicationAuthorization,
    ) -> PublishedGitHubRelease:
        """Publish exactly one explicitly authorized version.

        Any failure after draft creation deliberately leaves the draft in GitHub for operator
        inspection.  It is never resumed, replaced, or deleted automatically.
        """

        package_sha256 = self._validate(skill_version, authorization)
        tag = f"{skill_version.skill}-v{skill_version.version}"

        # Publishing first and discovering that repository immutability was disabled would be
        # an irreversible public side effect.  Fail before tag checks or draft creation instead.
        if self.config.require_immutable_release and not self.client.immutable_releases_enabled(
            self.config.owner, self.config.repository
        ):
            raise PublicationError(
                "GitHub repository release immutability is not enabled; publication denied"
            )

        # Check both resources before creating anything.  A tag without a release is still a
        # conflict because silently reusing it could bind the package to an unexpected commit.
        tag_exists = self.client.tag_exists(self.config.owner, self.config.repository, tag)
        existing_release = self.client.get_release_by_tag(
            self.config.owner, self.config.repository, tag
        )
        if tag_exists or existing_release is not None:
            found = "tag and release" if tag_exists and existing_release else "tag or release"
            raise GitHubConflictError(f"GitHub {found} already exists for {tag}; overwrite denied")

        assets = self._build_assets(skill_version, tag, package_sha256)
        draft = self.client.create_draft_release(
            self.config.owner,
            self.config.repository,
            tag=tag,
            name=f"{skill_version.skill} {skill_version.version}",
            body=self._release_body(skill_version, authorization, package_sha256),
            target_commitish=self.config.target_commitish,
        )
        if not draft.draft or draft.tag_name != tag:
            raise PublicationError("GitHub did not return the requested draft release")

        for asset in assets:
            self.client.upload_asset(
                draft,
                name=asset.name,
                content=asset.content,
                media_type=asset.media_type,
            )

        uploaded = tuple(
            self.client.list_assets(self.config.owner, self.config.repository, draft.id)
        )
        self._verify_asset_inventory(assets, uploaded)

        published = self.client.publish_release(self.config.owner, self.config.repository, draft.id)
        if published.draft or published.tag_name != tag:
            raise PublicationError("GitHub did not publish the verified draft")
        if self.config.require_immutable_release and published.immutable is not True:
            raise PublicationError(
                "release was published but GitHub did not report it as immutable; "
                "enable release immutability on the repository"
            )
        if not published.html_url:
            raise PublicationError("published GitHub release has no public URL")

        # The draft inventory check above closes ordinary upload failures, but publication is a
        # separate GitHub operation.  Re-read the now-immutable Release after that state change so
        # an asset replaced between the preflight list and publication cannot be accepted.  The
        # returned snapshot must also be the post-publication inventory, not the stale draft view.
        published_assets = tuple(
            self.client.list_assets(
                self.config.owner,
                self.config.repository,
                published.id,
            )
        )
        self._verify_asset_inventory(assets, published_assets)

        return PublishedGitHubRelease(
            release_id=published.id,
            tag=tag,
            html_url=published.html_url,
            immutable=published.immutable,
            assets=published_assets,
            approval_id=authorization.approval_id,
        )

    def _validate(
        self,
        skill_version: PublicSkillVersion,
        authorization: VersionPublicationAuthorization,
    ) -> str:
        if not _SKILL_RE.fullmatch(skill_version.skill):
            raise IntegrityError("skill name is not safe for a Git tag and asset name")
        if not _SEMVER_RE.fullmatch(skill_version.version):
            raise IntegrityError("version must be an exact semantic version")
        if not skill_version.package_bytes:
            raise IntegrityError("public package is empty")
        if not skill_version.signature:
            raise IntegrityError("a detached Skill Hub signature is required")
        license_bytes = skill_version.license_text.encode("utf-8")
        if (
            len(license_bytes) < 200
            or len(license_bytes) > 1024 * 1024
            or "\x00" in skill_version.license_text
        ):
            raise IntegrityError(
                "public publication requires complete UTF-8 license text between 200 B and 1 MiB"
            )

        manifest = skill_version.manifest
        if manifest.get("name") != skill_version.skill:
            raise IntegrityError("manifest name does not match the publication request")
        if manifest.get("version") != skill_version.version:
            raise IntegrityError("manifest version does not match the publication request")
        if manifest.get("scan_status") not in {"passed", "passed_with_warnings"}:
            raise IntegrityError("only a scanned release can be published")
        if not str(manifest.get("license", "")).strip():
            raise IntegrityError("manifest does not declare a license")

        package_sha256 = hashlib.sha256(skill_version.package_bytes).hexdigest()
        manifest_sha256 = str(manifest.get("artifact_sha256", ""))
        if not _SHA256_RE.fullmatch(manifest_sha256):
            raise IntegrityError("manifest artifact_sha256 is missing or malformed")
        if manifest_sha256 != package_sha256:
            raise IntegrityError("package bytes do not match manifest artifact_sha256")

        if authorization.decision != "approved" or authorization.scope != PUBLICATION_SCOPE:
            raise AuthorizationError("authorization does not approve a public GitHub release")
        if not authorization.approval_id.strip():
            raise AuthorizationError("authorization requires an approval_id")
        if not isinstance(authorization.approved_at, datetime):
            raise AuthorizationError("approved_at must be a datetime")
        if authorization.approved_at.tzinfo is None:
            raise AuthorizationError("approved_at must include a timezone")
        if authorization.approved_by != self.config.approver:
            raise AuthorizationError("authorization was not issued by the configured approver")
        if (
            authorization.skill != skill_version.skill
            or authorization.version != skill_version.version
        ):
            raise AuthorizationError("authorization is for a different Skill version")
        if authorization.artifact_sha256 != package_sha256:
            raise AuthorizationError("authorization is bound to a different artifact digest")
        if (
            authorization.destination_owner != self.config.owner
            or authorization.destination_repository != self.config.repository
        ):
            raise AuthorizationError("authorization is for a different GitHub destination")
        return package_sha256

    def _build_assets(
        self,
        skill_version: PublicSkillVersion,
        tag: str,
        package_sha256: str,
    ) -> tuple[ImmutableAsset, ...]:
        package_name = f"{tag}.zip"
        manifest_bytes = (
            json.dumps(
                skill_version.manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        readme = skill_version.readme_text or self._default_readme(skill_version, tag, package_name)
        attestation = skill_version.attestation_text or self._default_attestation(tag, package_name)

        base_assets = (
            ImmutableAsset.build(package_name, skill_version.package_bytes, "application/zip"),
            ImmutableAsset.build("manifest.json", manifest_bytes, "application/json"),
            ImmutableAsset.build("signature.sig.json", skill_version.signature, "application/json"),
            ImmutableAsset.build(
                "LICENSE", skill_version.license_text.encode("utf-8"), "text/plain; charset=utf-8"
            ),
            ImmutableAsset.build(
                "README.md", readme.encode("utf-8"), "text/markdown; charset=utf-8"
            ),
            ImmutableAsset.build(
                "ATTESTATION.md",
                attestation.encode("utf-8"),
                "text/markdown; charset=utf-8",
            ),
        )
        if base_assets[0].sha256 != package_sha256:
            raise IntegrityError("internal package digest changed while constructing assets")
        checksums = "".join(f"{asset.sha256}  {asset.name}\n" for asset in base_assets).encode(
            "utf-8"
        )
        return base_assets + (
            ImmutableAsset.build("SHA256SUMS", checksums, "text/plain; charset=utf-8"),
        )

    @staticmethod
    def _verify_asset_inventory(
        intended: Sequence[ImmutableAsset], uploaded: Sequence[GitHubAsset]
    ) -> None:
        intended_by_name = {asset.name: asset for asset in intended}
        uploaded_by_name = {asset.name: asset for asset in uploaded}
        if len(uploaded_by_name) != len(uploaded):
            raise AssetVerificationError("GitHub returned duplicate asset names")
        if set(intended_by_name) != set(uploaded_by_name):
            missing = sorted(set(intended_by_name) - set(uploaded_by_name))
            unexpected = sorted(set(uploaded_by_name) - set(intended_by_name))
            raise AssetVerificationError(
                f"asset inventory mismatch; missing={missing}, unexpected={unexpected}"
            )
        for name, intended_asset in intended_by_name.items():
            actual = uploaded_by_name[name]
            if actual.size != len(intended_asset.content):
                raise AssetVerificationError(f"asset size mismatch for {name}")
            if actual.digest != f"sha256:{intended_asset.sha256}":
                raise AssetVerificationError(f"asset digest mismatch for {name}")

    def _default_readme(
        self, skill_version: PublicSkillVersion, tag: str, package_name: str
    ) -> str:
        encoded_tag = parse.quote(tag, safe="-._~")
        encoded_asset = parse.quote(package_name, safe="-._~")
        download_url = (
            f"https://github.com/{self.config.owner}/{self.config.repository}/releases/"
            f"download/{encoded_tag}/{encoded_asset}"
        )
        return f"""# {skill_version.skill} {skill_version.version}

This is an explicitly approved public mirror of one immutable Skill Hub version. GitHub is not
the system of record.

## Download on macOS

```sh
curl -fL '{download_url}' -o '{package_name}'
curl -fL '{download_url.rsplit("/", 1)[0]}/SHA256SUMS' -o SHA256SUMS
grep -F '  {package_name}' SHA256SUMS | shasum -a 256 -c -
unzip -q '{package_name}' -d '{skill_version.skill}'
```

Download `signature.sig.json`, `manifest.json`, and `ATTESTATION.md` from the same Release before
performing signature or provenance verification. Install only into an Agent directory documented
by that Agent; do not execute unreviewed scripts from the archive.
"""

    def _default_attestation(self, tag: str, package_name: str) -> str:
        return f"""# Integrity and provenance

- `signature.sig.json` is the detached Ed25519 signature document produced and approved by the
  private Skill Hub; it binds the algorithm, trusted key ID, artifact digest, and Base64 signature.
- `SHA256SUMS` binds the package and every explanatory asset uploaded with this Release.
- GitHub Release attestation, when enabled by repository release immutability, proves the tag,
  commit, and Release assets as served by GitHub. It does not replace the Skill Hub signature.

```sh
gh release verify '{tag}' -R '{self.config.owner}/{self.config.repository}'
gh release verify-asset '{tag}' '{package_name}' -R '{self.config.owner}/{self.config.repository}'
```

Verify the detached signature with the trusted Skill Hub public key distributed outside this
Release. Do not treat this GitHub repository as an authority for private metadata or approvals.
"""

    @staticmethod
    def _release_body(
        skill_version: PublicSkillVersion,
        authorization: VersionPublicationAuthorization,
        package_sha256: str,
    ) -> str:
        return (
            f"Public distribution mirror for `{skill_version.skill}` "
            f"version `{skill_version.version}`.\n\n"
            f"Artifact SHA-256: `{package_sha256}`\n\n"
            f"Publication approval: `{authorization.approval_id}` by "
            f"`{authorization.approved_by}`.\n\n"
            "The private Skill Hub is the system of record. This Release is derived, immutable, "
            "and never imported back automatically."
        )


class UrllibGitHubReleaseClient:
    """Small GitHub REST client with an injectable URL opener for deterministic tests."""

    def __init__(
        self,
        token: str,
        *,
        api_base_url: str = "https://api.github.com",
        api_version: str = "2026-03-10",
        timeout_seconds: float = 30.0,
        opener: Callable[..., Any] = request.urlopen,
    ) -> None:
        if not token.strip():
            raise ValueError("GitHub token is required")
        self.token = token
        self.api_base_url = api_base_url.rstrip("/")
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds
        self.opener = opener

    def immutable_releases_enabled(self, owner: str, repository: str) -> bool:
        value = self._json_request(
            "GET",
            f"/repos/{owner}/{repository}/immutable-releases",
            allow_not_found=True,
        )
        return isinstance(value, Mapping) and value.get("enabled") is True

    def tag_exists(self, owner: str, repository: str, tag: str) -> bool:
        path = f"/repos/{owner}/{repository}/git/ref/tags/{parse.quote(tag, safe='')}"
        value = self._json_request("GET", path, allow_not_found=True)
        return value is not None

    def get_release_by_tag(self, owner: str, repository: str, tag: str) -> GitHubRelease | None:
        path = f"/repos/{owner}/{repository}/releases/tags/{parse.quote(tag, safe='')}"
        value = self._json_request("GET", path, allow_not_found=True)
        return GitHubRelease.from_api(value) if value is not None else None

    def create_draft_release(
        self,
        owner: str,
        repository: str,
        *,
        tag: str,
        name: str,
        body: str,
        target_commitish: str,
    ) -> GitHubRelease:
        value = self._json_request(
            "POST",
            f"/repos/{owner}/{repository}/releases",
            json_body={
                "tag_name": tag,
                "target_commitish": target_commitish,
                "name": name,
                "body": body,
                "draft": True,
                "prerelease": False,
                "generate_release_notes": False,
            },
        )
        return GitHubRelease.from_api(value)

    def upload_asset(
        self,
        release: GitHubRelease,
        *,
        name: str,
        content: bytes,
        media_type: str,
    ) -> GitHubAsset:
        if not release.upload_url:
            raise GitHubAPIError(500, "draft response omitted upload_url")
        upload_url = release.upload_url.split("{", 1)[0]
        separator = "&" if "?" in upload_url else "?"
        upload_url = f"{upload_url}{separator}{parse.urlencode({'name': name})}"
        value = self._json_request("POST", upload_url, raw_body=content, content_type=media_type)
        return GitHubAsset.from_api(value)

    def list_assets(self, owner: str, repository: str, release_id: int) -> Sequence[GitHubAsset]:
        value = self._json_request(
            "GET", f"/repos/{owner}/{repository}/releases/{release_id}/assets?per_page=100"
        )
        if not isinstance(value, list):
            raise GitHubAPIError(500, "asset list response is not an array")
        return tuple(GitHubAsset.from_api(item) for item in value)

    def publish_release(self, owner: str, repository: str, release_id: int) -> GitHubRelease:
        value = self._json_request(
            "PATCH",
            f"/repos/{owner}/{repository}/releases/{release_id}",
            json_body={"draft": False},
        )
        return GitHubRelease.from_api(value)

    def _json_request(
        self,
        method: str,
        path_or_url: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        url = (
            path_or_url
            if path_or_url.startswith(("https://", "http://"))
            else f"{self.api_base_url}{path_or_url}"
        )
        body = raw_body
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            content_type = "application/json"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "personal-skill-hub/0.1",
            "X-GitHub-Api-Version": self.api_version,
        }
        if content_type:
            headers["Content-Type"] = content_type
        req = request.Request(url=url, data=body, headers=headers, method=method)
        try:
            with self.opener(req, timeout=self.timeout_seconds) as response:
                payload = response.read()
        except error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            if allow_not_found and exc.code == 404:
                return None
            message = exc.reason or "request failed"
            if exc.code in {409, 422}:
                raise GitHubConflictError(
                    f"GitHub rejected a non-overwriting publication: {payload or message}"
                ) from exc
            raise GitHubAPIError(exc.code, str(message), payload) from exc
        except error.URLError as exc:
            raise GitHubAPIError(0, f"network error: {exc.reason}") from exc

        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise GitHubAPIError(500, "GitHub response was not valid JSON") from exc
